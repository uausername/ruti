"""Answer safe Codex approval prompts in tmux panes, once, with option 1.

One `scan()` looks at every tmux pane and approves a prompt only when *all* hold:

* the pane's cwd is a Codex worker worktree (`.../harness-local-N/worktrees/codex-*`);
* the pane's foreground program is Codex (not a shell, not Claude);
* `harness ps --json` lists a Codex harness in that folder and a live master
  (Claude Code) harness -- with no master the service approves nothing;
* the prompt is a "Would you like to run the following command?" dialog with option
  1 "Yes, proceed" selected, the command is shown in full (a truncated one is read
  from Codex's own full-command pager), and `policy.decide` allows it;
* right before the key press the pane still has the same PID and the same prompt.

It only ever sends `y` (option 1, approve once) -- never "don't ask again". Every
decision goes to a JSONL log; refusals are also appended to `needs_admin.jsonl`, the
queue a person or the Administrator works through. Any failure approves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config
from . import policy

PROMPT_MARK = "Would you like to run the following command"
CONFIRM_MARK = "Press enter to confirm or esc"
YES_SELECTED = re.compile(r"›\s*1\.\s*Yes, proceed")
PAGER_PERCENT = re.compile(r"─\s(\d{1,3})%\s─")
HOOKS_MARK = "Hooks need review"
UPDATE_MARK = "Update available"
UPDATE_SKIP = "Skip until next version"

Runner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout


@dataclass
class Pane:
    pane_id: str
    pid: str
    command: str
    path: str


@dataclass
class Approver:
    layout: policy.Layout = field(default_factory=policy.Layout)
    run: Runner = _run
    sleep: Callable[[float], None] = time.sleep
    log_dir: Path | None = None
    require_master: bool = True
    codex_commands: tuple[str, ...] = ("codex", "node")
    seen: set[str] = field(default_factory=set)

    # -- logging -----------------------------------------------------------------
    def _dir(self) -> Path:
        d = self.log_dir or (config.STATE_ROOT / "operator")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log(self, event: str, **fields) -> dict:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(self._dir() / "approver.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line)
        if event == "needs_admin":
            with open(self._dir() / "needs_admin.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line)
        return row

    # -- tmux / harness ----------------------------------------------------------
    def panes(self) -> list[Pane]:
        out = self.run(["tmux", "list-panes", "-a", "-F",
                        "#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_current_path}"])
        panes = []
        for row in out.splitlines():
            parts = row.split("|", 3)
            if len(parts) == 4:
                panes.append(Pane(*parts))
        return panes

    def capture(self, pane_id: str) -> str:
        return self.run(["tmux", "capture-pane", "-p", "-J", "-t", pane_id])

    def harness_folders(self) -> set[str] | None:
        """Folders of Codex harnesses, or None when no master is alive / ps failed."""
        try:
            data = json.loads(self.run(["harness", "ps", "--json"]) or "{}")
        except (ValueError, subprocess.SubprocessError, OSError):
            return None
        rows = data.get("harnesses") or []
        alive = [r for r in rows if r.get("state") not in ("stopped", "closed", None)]
        master = any("claude" in str(r.get("agent", "")).lower() for r in alive)
        if self.require_master and not master:
            return None
        return {str(r.get("folder", "")).rstrip("/") for r in alive
                if "codex" in str(r.get("agent", "")).lower()}

    def worktree_of(self, path: str) -> str | None:
        m = re.fullmatch(self.layout.worktrees_re + r"(codex-[a-z0-9-]+)(?:/app)?/?", path)
        return m.group(1) if m else None

    # -- prompt parsing ----------------------------------------------------------
    @staticmethod
    def block(screen: str) -> list[str] | None:
        """The raw lines of the command in a run-command prompt, or None."""
        if PROMPT_MARK not in screen or CONFIRM_MARK not in screen:
            return None
        lines = screen.split("\n")
        mark = max(i for i, l in enumerate(lines) if PROMPT_MARK in l)
        start = next((i for i in range(mark + 1, len(lines)) if lines[i].strip().startswith("$ ")), None)
        if start is None:
            return None
        end = next((j for j in range(start, len(lines)) if YES_SELECTED.search(lines[j])), None)
        if end is None:
            return None
        return [l.rstrip() for l in lines[start:end]]

    @classmethod
    def extract(cls, screen: str) -> str | None:
        """The command shown in a run-command prompt, 'TRUNCATED', or None."""
        block = cls.block(screen)
        if block is None:
            return None
        # Codex 0.16x: "… +3 lines (ctrl + a view all)"; 0.166: "[… 36 lines] ^a view all"
        if any("view all" in b or "[…" in b for b in block):
            return "TRUNCATED"
        block[0] = block[0].strip()[2:]
        return "\n".join(block).strip()

    def _pager(self, pane_id: str) -> tuple[list[str], int | None] | None:
        """Body lines and scroll percentage of Codex's full-command pager, or None."""
        lines = [l.rstrip() for l in self.capture(pane_id).split("\n")]
        if not any("q close" in l for l in lines):
            return None
        try:
            head = next(i for i, l in enumerate(lines) if l.lstrip().startswith("/ E X E C"))
            tail = next(i for i, l in enumerate(lines) if i > head and l.lstrip().startswith("─"))
        except StopIteration:
            return None
        m = PAGER_PERCENT.search(lines[tail])
        return lines[head + 1:tail], (int(m.group(1)) if m else None)

    def read_full(self, pane: Pane, max_lines: int = 400) -> str | None:
        """Open Codex's full-command pager (Ctrl+A), read all of it, close it (q).

        A command longer than the pane is read from the top one line at a time
        (Down) until the pager says 100%; every step must shift the view by
        exactly one line, otherwise nothing is returned.
        """
        keys = lambda *k: self.run(["tmux", "send-keys", "-t", pane.pane_id, *k])  # noqa: E731
        keys("C-a")
        self.sleep(1.5)
        try:
            page = self._pager(pane.pane_id)
            if page is None:
                return None
            body, pct = page
            if any(l.strip() == "~" for l in body):  # filler: the command fits
                return "\n".join(l for l in body if l.strip() != "~").strip()
            if pct is None:
                return None
            keys("Home")
            self.sleep(0.5)
            page = self._pager(pane.pane_id)
            if page is None:
                return None
            body, pct = page
            out = list(body)
            for _ in range(max_lines):
                if pct == 100:
                    return "\n".join(out).strip()
                keys("Down")
                self.sleep(0.2)
                page = self._pager(pane.pane_id)
                if page is None or page[1] is None or page[1] < pct:
                    return None
                nbody, pct = page
                if len(nbody) != len(body) or nbody[:-1] != body[1:]:
                    return None
                out.append(nbody[-1])
                body = nbody
            return None
        finally:
            keys("q")
            self.sleep(1.5)

    # -- one pass ----------------------------------------------------------------
    def scan(self) -> list[dict]:
        results: list[dict] = []
        try:
            panes = self.panes()
        except (subprocess.SubprocessError, OSError) as exc:
            return [self.log("error", error=repr(exc))]
        candidates = []
        for pane in panes:
            worktree = self.worktree_of(pane.path)
            if not worktree:
                continue
            screen = self.capture(pane.pane_id)
            if UPDATE_MARK in screen and UPDATE_SKIP in screen:
                # Codex's "Update available" menu blocks the session (seen after a WSL restart,
                # 2026-11-23). Skipping is the conservative answer: no download, no new
                # version mid-task. Updating Codex stays a person's decision.
                self.run(["tmux", "send-keys", "-t", pane.pane_id, "Escape"])
                results.append(self.log("skipped_update", pane=pane.pane_id, pid=pane.pid, worktree=worktree))
                continue
            if HOOKS_MARK in screen:
                # never answered here: trusting hooks is a person's decision (Codex /hooks)
                results.append(self._refuse(pane, worktree, "Codex asks to review hooks "
                                            "(new or changed definitions); a person trusts them in /hooks"))
                continue
            if self.extract(screen) is not None:
                candidates.append((pane, worktree, screen))
        if not candidates:
            return results
        folders = self.harness_folders()
        for pane, worktree, screen in candidates:
            results.append(self._handle(pane, worktree, screen, folders))
        return [r for r in results if r]

    def _refuse(self, pane: Pane, worktree: str, reason: str, command: str = "") -> dict | None:
        key = hashlib.sha256(f"{pane.pane_id}|{pane.pid}|{command}|{reason}".encode()).hexdigest()
        if key in self.seen:
            return None
        self.seen.add(key)
        return self.log("needs_admin", pane=pane.pane_id, pid=pane.pid, worktree=worktree,
                        reason=reason, command=command[-400:])

    def _handle(self, pane: Pane, worktree: str, screen: str, folders: set[str] | None) -> dict | None:
        if folders is None:
            return self._refuse(pane, worktree, "no live master harness or `harness ps` failed")
        root = re.sub(r"/app/?$", "", pane.path.rstrip("/"))
        if root not in folders:
            return self._refuse(pane, worktree, "pane folder is not a Codex harness in `harness ps`")
        if pane.command not in self.codex_commands:
            return self._refuse(pane, worktree, f"foreground program is {pane.command!r}, not Codex")
        command = self.extract(screen)
        truncated = command == "TRUNCATED"
        if truncated:
            key = hashlib.sha256(f"{pane.pane_id}|{screen}".encode()).hexdigest()
            if key in self.seen:
                return None
            self.seen.add(key)
            command = self.read_full(pane)
            if command is None:
                return self._refuse(pane, worktree, "truncated command could not be read in full")
        decision = policy.decide(command, worktree, self.layout)
        if not decision.allowed:
            return self._refuse(pane, worktree, decision.reason, command)
        # last look: same process, same prompt, option 1 still selected
        current = {p.pane_id: p for p in self.panes()}.get(pane.pane_id)
        again = self.capture(pane.pane_id)
        same = self.extract(again)
        if current is None or current.pid != pane.pid or not YES_SELECTED.search(again) or (
                same != command and not (truncated and same == "TRUNCATED"
                                         and self.block(again) == self.block(screen))):
            return self._refuse(pane, worktree, "prompt changed before the key press", command)
        self.run(["tmux", "send-keys", "-t", pane.pane_id, "y"])
        return self.log("approved", pane=pane.pane_id, pid=pane.pid, worktree=worktree,
                        command=command[-400:], full_read=truncated)

    def loop(self, interval: float = 5.0) -> None:
        self.log("started", pid_self=str(__import__("os").getpid()))
        while True:
            try:
                for row in self.scan():
                    if row.get("event") == "approved":
                        self.sleep(2)
            except Exception as exc:  # noqa: BLE001 -- the service must keep running
                self.log("error", error=repr(exc))
            self.sleep(interval)

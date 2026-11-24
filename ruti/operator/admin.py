"""Small tools for the Administrator session: talk to the master, collect side-agent cards.

`ruti operator tell-master "<text>"` pastes a message into the master's Claude pane --
the one live Claude harness whose folder is a ``haccp-master-*`` worktree (or ``--folder``).
With ``--when-idle`` it waits until the master is idle, which is what slash commands such
as ``/model sonnet`` need; a message sent while the master works is queued by Claude.

`ruti operator ysk` lists the "You should know" cards that Claude Code's side agent showed
in WSL sessions. Nobody reads the master's pane, so the waker collects new cards every
minute into ``$STATE/operator/ysk.jsonl``; this command prints the ones not yet seen.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60).stdout


def masters(run: Runner = _run, folder: str | None = None) -> list[dict]:
    try:
        rows = json.loads(run(["harness", "ps", "--json"]) or "{}").get("harnesses") or []
    except ValueError:
        return []
    out = []
    for r in rows:
        f = (r.get("folder") or "").rstrip("/")
        if r.get("state") in ("closed", "stopped") or "claude" not in (r.get("agent") or "").lower():
            continue
        if (folder and f == folder.rstrip("/")) or (not folder and "/haccp-master-" in f):
            out.append(r)
    return out


def pane_for(folder: str, run: Runner = _run) -> str | None:
    out = run(["tmux", "list-panes", "-a", "-F", "#{pane_id}|#{pane_current_command}|#{pane_current_path}"])
    hits = [p for p, cmd, path in (l.split("|", 2) for l in out.splitlines() if l.count("|") >= 2)
            if path.rstrip("/") == folder.rstrip("/") and cmd in ("claude", "node")]
    return hits[0] if len(hits) == 1 else None


class TellError(RuntimeError):
    pass


def tell_master(text: str, *, folder: str | None = None, when_idle: bool = False, wait_s: float = 1800,
                run: Runner = _run, sleep: Callable[[float], None] = time.sleep) -> dict:
    ms = masters(run, folder)
    if len(ms) != 1:
        raise TellError(f"expected exactly one live master, found {len(ms)}: "
                        + ", ".join(f"{m.get('name')} @ {m.get('folder')}" for m in ms))
    m = ms[0]
    waited = 0.0
    while when_idle and m.get("state") != "idle":
        if waited >= wait_s:
            raise TellError(f"master still {m.get('state')} after {int(waited)}s")
        sleep(15)
        waited += 15
        ms = masters(run, m["folder"])
        if not ms:
            raise TellError("master disappeared while waiting")
        m = ms[0]
    pane = pane_for(m["folder"], run)
    if not pane:
        raise TellError(f"no single Claude pane in {m['folder']}")
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        run(["tmux", "load-buffer", "-b", "ruti-admin", tmp])
        run(["tmux", "paste-buffer", "-p", "-d", "-b", "ruti-admin", "-t", pane])
    finally:
        os.unlink(tmp)
    sleep(1)
    run(["tmux", "send-keys", "-t", pane, "Enter"])
    return {"ok": True, "pane": pane, "harness_id": m.get("id"), "state_before": m.get("state"),
            "waited_s": int(waited)}


# -- "You should know" cards ---------------------------------------------------------
def ysk_stores(home: Path | None = None) -> list[Path]:
    return sorted(((home or Path.home()) / ".claude" / "plugins" / "store").glob(
        "cc-plugin-you-should-know_*.json"))


def collect_ysk(feed: Path, stores: list[Path] | None = None, now: Callable[[], float] = time.time) -> list[dict]:
    """Append cards not yet in `feed`; returns the new ones."""
    seen = set()
    if feed.exists():
        for line in feed.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            seen.add((d.get("session"), d.get("shown_at")))
    new = []
    for store in stores if stores is not None else ysk_stores():
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in data.get("sessions") or []:
            v = s.get("view") or {}
            if not v.get("line"):
                continue
            key = (s.get("id"), v.get("shownAt"))
            if key in seen:
                continue
            seen.add(key)
            new.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now())),
                        "session": s.get("id"), "shown_at": v.get("shownAt"), "tag": s.get("tag"),
                        "kind": v.get("kind"), "line": v.get("line"), "explanation": v.get("explanation")})
    if new:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as fh:
            for row in new:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return new


def unread_ysk(feed: Path, mark: Path) -> list[dict]:
    """Cards after the last `ruti operator ysk --mark-read`."""
    if not feed.exists():
        return []
    rows = [json.loads(l) for l in feed.read_text(encoding="utf-8").splitlines() if l.strip()]
    try:
        upto = int(mark.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        upto = 0
    return rows[upto:]


def mark_read(feed: Path, mark: Path) -> int:
    n = len([l for l in feed.read_text(encoding="utf-8").splitlines() if l.strip()]) if feed.exists() else 0
    mark.write_text(str(n), encoding="utf-8")
    return n

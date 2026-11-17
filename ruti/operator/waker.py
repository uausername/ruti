"""Wake the master Claude after a pause -- deterministically, without an LLM on watch.

The master records *when* and *from what* it should resume (`ruti operator resume-at`),
then ends its turn. The `ruti operator wake` service checks that request every minute:

* not due yet, ``manual``, or already handled -> nothing;
* due, but the Claude quota reading still says the window is spent -> postponed to its reset;
* the master harness is working already -> request closed, nothing sent;
* the master is idle, and its pause is shorter than ``fresh_after_s`` (the prompt cache
  is still warm) -> a fixed message is pasted into its tmux pane and the harness must
  turn ``working`` within a minute;
* otherwise (pane gone, or a long pause where resuming would re-read the whole context
  uncached) -> a *new* master is started in the same folder with
  ``harness new claude`` and told to read the checkpoint; once it is *working*, the old
  idle master is closed (its conversation is kept; ``--now`` if a stray background loop
  would otherwise wake it later). A new master that never starts working -- e.g. stuck
  on Claude's "trust this folder" screen -- fails the request at once (a retry would
  start a second session) and the old master is left as it was.

Every step is a JSON line in ``$STATE_ROOT/operator/waker.jsonl``. After
``max_attempts`` failures the request turns ``failed`` and goes to ``needs_admin.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config
from . import trust as trust_mod

Runner = Callable[[list[str]], str]

QUOTA_LIMITS = {"five_hour": 95, "seven_day": 97}
STALE_LIMITS = {"five_hour": 80, "seven_day": 90}
FRESH_S = 1800
# Claude Code's first-run screen in a folder it has not been told to trust.
TRUST_MARK = "trust this folder"


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout


def state_dir() -> Path:
    d = config.STATE_ROOT / "operator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_after(value: str, now: float, quota: dict | None = None) -> float:
    """`+90m`, `+2h`, `+30s`, an ISO time (naive = UTC), an epoch, or `quota`."""
    value = value.strip()
    m = re.fullmatch(r"\+(\d+)([smh])", value)
    if m:
        return now + int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    if value == "quota":
        reset = quota_block(quota or {}, now)
        return reset if reset else now
    if re.fullmatch(r"\d{9,11}(\.\d+)?", value):
        return float(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def quota_block(quota: dict, now: float) -> float | None:
    """The latest reset among windows at/over their limit that have not reset yet.

    A reading older than ``FRESH_S`` may have grown since -- other Claude sessions
    (Windows, other projects) spend the same subscription -- so the stricter
    ``STALE_LIMITS`` apply to it.
    """
    captured = quota.get("captured_at_epoch")
    try:
        stale = captured is None or now - float(captured) > FRESH_S
    except (TypeError, ValueError):
        stale = True
    limits = STALE_LIMITS if stale else QUOTA_LIMITS
    blocked = []
    for key, limit in limits.items():
        w = quota.get(key) or {}
        used, reset = w.get("used_percentage"), w.get("resets_at")
        try:
            used, reset = float(used), float(reset)
        except (TypeError, ValueError):
            continue
        if used >= limit and reset > now:
            blocked.append(reset)
    return max(blocked) if blocked else None


def quota_sources() -> list[Path]:
    """This ruti's quota.json, plus the Windows one when running in WSL.

    The status line writes quota.json only where a Claude session runs, and the
    Windows sessions spend the same subscription as the WSL master.
    """
    paths = [config.STATE_ROOT / "quota.json"]
    extra = os.environ.get("RUTI_QUOTA_SOURCES")
    if extra:
        paths += [Path(p) for p in extra.split(os.pathsep) if p]
    else:
        paths += sorted(Path("/mnt/c/Users").glob("*/AppData/Local/ruti/quota.json"))
    return paths


def load_quota(paths: list[Path] | None = None) -> dict:
    """The freshest quota reading among the sources (by ``captured_at_epoch``)."""
    best: dict = {}
    for path in paths if paths is not None else quota_sources():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if float(data.get("captured_at_epoch") or 0) > float(best.get("captured_at_epoch") or 0):
            best = {**data, "_source": str(path)}
    return best


@dataclass
class Waker:
    run: Runner = _run
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    quota: Callable[[], dict] = load_quota
    root: Path | None = None
    fresh_after_s: float = 3000.0      # Claude Code's prompt cache lives an hour
    max_attempts: int = 3
    confirm_s: float = 60.0
    # Pre-accept Claude's folder-trust screen; None = ~/.claude.json (WSL and Windows).
    trust: Callable[[list[str]], object] | None = None

    # -- files ------------------------------------------------------------------
    def _dir(self) -> Path:
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            return self.root
        return state_dir()

    @property
    def request_path(self) -> Path:
        return self._dir() / "resume.json"

    def load(self) -> dict | None:
        try:
            return json.loads(self.request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, req: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(req, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.request_path)

    def log(self, event: str, **fields) -> dict:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(self._dir() / "waker.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line)
        if event in ("needs_admin", "failed"):
            with open(self._dir() / "needs_admin.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**row, "source": "waker"}, ensure_ascii=False) + "\n")
        return row

    # -- harness / tmux -----------------------------------------------------------
    def harnesses(self) -> list[dict] | None:
        try:
            return json.loads(self.run(["harness", "ps", "--json"]) or "{}").get("harnesses") or []
        except (ValueError, subprocess.SubprocessError, OSError):
            return None

    def find(self, rows: list[dict], harness_id: str) -> dict | None:
        return next((r for r in rows if r.get("id") == harness_id), None)

    def master_pane(self, folder: str) -> str | None:
        out = self.run(["tmux", "list-panes", "-a", "-F",
                        "#{pane_id}|#{pane_current_command}|#{pane_current_path}"])
        hits = [p for p, cmd, path in (r.split("|", 2) for r in out.splitlines() if r.count("|") >= 2)
                if path.rstrip("/") == folder.rstrip("/") and cmd in ("claude", "node")]
        return hits[0] if len(hits) == 1 else None

    def _paste(self, pane: str, text: str) -> None:
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            self.run(["tmux", "load-buffer", "-b", "ruti-waker", tmp])
            self.run(["tmux", "paste-buffer", "-p", "-d", "-b", "ruti-waker", "-t", pane])
        finally:
            os.unlink(tmp)
        self.sleep(1)
        self.run(["tmux", "send-keys", "-t", pane, "Enter"])

    def _became_working(self, harness_id: str, before: str | None) -> bool:
        waited = 0.0
        while waited < self.confirm_s:
            self.sleep(5)
            waited += 5
            rows = self.harnesses() or []
            row = self.find(rows, harness_id)
            if row and (row.get("state") == "working" or (before and row.get("lastActive") != before)):
                return True
        return False

    @staticmethod
    def message(req: dict, fresh: bool) -> str:
        cp, why = req.get("checkpoint", ""), req.get("reason") or "pause"
        if fresh:
            return (f"[ruti operator wake] Ты мастер Claude связки Harness -> Codex. Предыдущая "
                    f"сессия мастера ({req.get('harness_id')}) остановилась: {why}. Прочитай чекпойнт "
                    f"{cp} целиком и продолжай с его следующего шага. Перед новой паузой запиши "
                    f"чекпойнт и вызови `ruti operator resume-at`.")
        return (f"[ruti operator wake] Пауза окончена ({why}). Продолжай по чекпойнту {cp}.")

    # -- one tick ------------------------------------------------------------------
    def tick(self) -> dict | None:
        req = self.load()
        if not req or req.get("status") != "pending" or req.get("mode") == "manual":
            return None
        now = self.now()
        if now < float(req.get("after", 0)):
            return None
        quota = self.quota()
        reset = quota_block(quota, now)
        if reset:
            req["after"] = reset + 120
            self.save(req)
            age = now - float(quota.get("captured_at_epoch") or 0)
            return self.log("postponed", until=req["after"], reason="Claude quota still spent",
                            reading_age_s=int(age), source=quota.get("_source"))
        rows = self.harnesses()
        if rows is None:
            return self.log("error", error="`harness ps` failed")
        old = self.find(rows, req["harness_id"]) if req.get("harness_id") else None
        if old and old.get("state") == "working":
            return self._close(req, "done", how="already working")
        idle_for = None
        if old and old.get("lastActive"):
            try:
                idle_for = now - datetime.fromisoformat(old["lastActive"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                idle_for = None
        pane = self.master_pane(req["folder"]) if old else None
        if old and pane and idle_for is not None and idle_for < self.fresh_after_s:
            self.log("poke", pane=pane, harness_id=old["id"], idle_s=int(idle_for))
            self._paste(pane, self.message(req, fresh=False))
            if self._became_working(old["id"], old.get("lastActive")):
                return self._close(req, "done", how="poked", pane=pane)
            return self._fail(req, "poked pane did not start working")
        return self._fresh(req, old)

    def _fresh(self, req: dict, old: dict | None) -> dict:
        if not Path(req["folder"]).is_dir():
            req["status"] = "failed"
            self.save(req)
            return self.log("failed", reason="master folder no longer exists; prepare a new worktree "
                            "and re-arm resume-at with --folder", folder=req["folder"])
        try:
            added = (self.trust or trust_mod.trust_everywhere)([req["folder"]])
            if added:
                self.log("trusted", folder=req["folder"], changes=added)
        except Exception as exc:  # noqa: BLE001 -- the trust screen check below still catches it
            self.log("error", error=f"pre-trusting the folder failed: {exc!r}")
        argv = ["harness", "new", "claude", req["folder"], "--json",
                "--name", f"master (resumed {time.strftime('%m-%d %H:%M')})",
                "--mode", req.get("permission_mode", "auto"),
                "--task", self.message(req, fresh=True)]
        if req.get("model"):
            argv[4:4] = ["--model", req["model"]]
        self.log("start", folder=req["folder"], replaces=(old or {}).get("id"))
        try:
            out = json.loads(self.run(argv) or "{}")
        except ValueError:
            out = {}
        new_id = (out.get("agent") or {}).get("id")
        if not out.get("ok") or not new_id:
            return self._fail(req, "harness new did not start a master", output=str(out)[:300])
        self.sleep(10)
        if not self.find(self.harnesses() or [], new_id):
            return self._fail(req, "new master is not listed by `harness ps`", new_id=new_id)
        if not self._became_working(new_id, None):
            # A session exists now: retrying would start a second one. Hand it to a person.
            screen = self._screens(req["folder"])
            why = ("Claude asks whether to trust the folder; answer it in the pane"
                   if TRUST_MARK in screen else "new master did not start working")
            req["status"] = "failed"
            req["new_id"] = new_id
            self.save(req)
            return self.log("failed", reason=why, new_id=new_id, old_kept=(old or {}).get("id"))
        closed = self._close_old(old) if old and old.get("state") != "working" else None
        return self._close(req, "done", how="new session", new_id=new_id, closed=closed)

    def _screens(self, folder: str) -> str:
        out = self.run(["tmux", "list-panes", "-a", "-F",
                        "#{pane_id}|#{pane_current_command}|#{pane_current_path}"])
        panes = [p for p, _cmd, path in (r.split("|", 2) for r in out.splitlines() if r.count("|") >= 2)
                 if path.rstrip("/") == folder.rstrip("/")]
        return "\n".join(self.run(["tmux", "capture-pane", "-p", "-t", p]) for p in panes)

    def _close_old(self, old: dict) -> str | None:
        """Close the replaced master; its stray background loop must not wake it later.

        `harness close` refuses while the old session still runs a background command
        (e.g. a budget-wait loop); left alone, that loop finishes and wakes a second
        master that re-reads its whole context. Since the new master already works,
        the old one is closed with --now. Returns the closed id, or None (needs_admin).
        """
        for argv in (["harness", "close", old["id"]], ["harness", "close", old["id"], "--now"]):
            self.run(argv)
            if not self.find(self.harnesses() or [], old["id"]):
                return old["id"]
        self.log("needs_admin", reason="old master could not be closed; two masters may run",
                 harness_id=old["id"])
        return None

    def _close(self, req: dict, status: str, **fields) -> dict:
        req.update(status=status, handled_at=self.now(), **{k: v for k, v in fields.items() if k != "how"})
        req["how"] = fields.get("how")
        self.save(req)
        return self.log(status, **fields)

    def _fail(self, req: dict, reason: str, **fields) -> dict:
        req["attempts"] = int(req.get("attempts", 0)) + 1
        if req["attempts"] >= self.max_attempts:
            req["status"] = "failed"
            self.save(req)
            return self.log("failed", reason=reason, **fields)
        req["after"] = self.now() + 300
        self.save(req)
        return self.log("retry", reason=reason, attempt=req["attempts"], **fields)

    def loop(self, interval: float = 60.0) -> None:
        self.log("started", pid_self=str(os.getpid()))
        last_sweep, refused = 0.0, None
        while True:
            try:  # side-agent cards shown in panes nobody reads (the master's)
                from .admin import collect_ysk

                cards = collect_ysk(self._dir() / "ysk.jsonl")
                if cards:
                    self.log("ysk", count=len(cards))
            except Exception as exc:  # noqa: BLE001
                self.log("error", error=f"ysk collect failed: {exc!r}")
            if self.now() - last_sweep >= 300:  # git per worktree: every 5 minutes is plenty
                last_sweep = self.now()
                try:
                    changed = trust_mod.sweep()  # agent worktrees never stop on the trust screen
                    now_refused = sorted(changed.pop("refused", []))
                    if changed:
                        self.log("trusted", changes=changed)
                    if now_refused != refused:
                        refused = now_refused
                        if now_refused:
                            self.log("needs_admin", reason="agent worktree settings differ from main; "
                                     "Claude will ask for trust there", folders=now_refused)
                except Exception as exc:  # noqa: BLE001
                    self.log("error", error=f"trust sweep failed: {exc!r}")
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 -- the service must keep running
                self.log("error", error=repr(exc))
            self.sleep(interval)


def request(*, harness_id: str, folder: str, checkpoint: str, after: float, reason: str,
            mode: str = "auto", model: str | None = None, now: float | None = None) -> dict:
    return {"status": "pending", "mode": mode, "harness_id": harness_id, "folder": folder.rstrip("/"),
            "checkpoint": checkpoint, "after": after, "reason": reason, "model": model,
            "set_at": now if now is not None else time.time(), "attempts": 0}

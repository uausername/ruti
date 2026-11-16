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
  ``harness new claude`` and told to read the checkpoint; once it is listed, the old
  idle master is closed (its conversation is kept).

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

Runner = Callable[[list[str]], str]

QUOTA_LIMITS = {"five_hour": 95, "seven_day": 97}


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
    """The latest reset among windows at/over their limit that have not reset yet."""
    blocked = []
    for key, limit in QUOTA_LIMITS.items():
        w = quota.get(key) or {}
        used, reset = w.get("used_percentage"), w.get("resets_at")
        try:
            used, reset = float(used), float(reset)
        except (TypeError, ValueError):
            continue
        if used >= limit and reset > now:
            blocked.append(reset)
    return max(blocked) if blocked else None


def load_quota() -> dict:
    try:
        return json.loads((config.STATE_ROOT / "quota.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
        reset = quota_block(self.quota(), now)
        if reset:
            req["after"] = reset + 120
            self.save(req)
            return self.log("postponed", until=req["after"], reason="Claude quota still spent")
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
        if old and old.get("state") != "working":
            self.run(["harness", "close", old["id"]])
        return self._close(req, "done", how="new session", new_id=new_id, closed=(old or {}).get("id"))

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
        while True:
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

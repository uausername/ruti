"""Claude and Codex quota as one input for the operator (`ruti operator quota`).

Claude: the freshest ``quota.json`` written by a Claude status line, in WSL or Windows
(see ``waker.load_quota``). Codex: the freshest ``rate_limits`` record among recent
rollouts of *any* Codex session, WSL (``~/.codex/sessions``) or Windows -- every Codex
session of the account spends the same limit, whichever model it runs.

``gate`` answers "may a new Codex worker start now?":

* Claude: the waker's limits (5h >= 95%, week >= 97%; stricter for a stale reading);
* Codex: 5h >= 70% or week >= 85% (a new worker would run into the limit mid-task);
* at most ``max_parallel`` Codex harnesses alive at once (default 4, the user's choice
  on 2026-11-16; the 70% / 85% limits above are what stops a window running dry).

A reading that has already reset never blocks.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import codex_telemetry
from . import waker

CODEX_LIMITS = {"five_hour": 70, "seven_day": 85}
MAX_PARALLEL = 4


def codex_session_dirs() -> list[Path]:
    override = os.environ.get("RUTI_CODEX_SESSIONS")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return [home / "sessions", *sorted(Path("/mnt/c/Users").glob("*/.codex/sessions"))]


def codex_reading(dirs: list[Path] | None = None, now: float | None = None,
                  max_files: int = 12, max_age_days: float = 8) -> dict:
    """The freshest Codex rate-limit record among the newest rollouts."""
    now = time.time() if now is None else now
    files = []
    for d in dirs if dirs is not None else codex_session_dirs():
        try:
            for p in d.rglob("rollout-*.jsonl"):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if now - mtime < max_age_days * 86400:
                    files.append((mtime, p))
        except OSError:
            continue
    best: dict = {}
    for _, path in sorted(files, reverse=True)[:max_files]:
        try:
            with open(path, "rb") as fh:
                sid = json.loads(fh.readline())["payload"]["id"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        r = codex_telemetry.read(path, sid, now=now)
        if r.get("captured_at_epoch") and r["captured_at_epoch"] > best.get("captured_at_epoch", 0):
            best = {k: r.get(k) for k in ("five_hour", "seven_day", "captured_at_epoch", "model")}
            best["_source"] = str(path)
    return best


def block(reading: dict, limits: dict, now: float) -> float | None:
    out = []
    for key, limit in limits.items():
        w = reading.get(key) or {}
        try:
            used, reset = float(w.get("used_percentage")), float(w.get("resets_at"))
        except (TypeError, ValueError):
            continue
        if used >= limit and reset > now:
            out.append(reset)
    return max(out) if out else None


def _view(reading: dict, now: float) -> dict:
    cap = reading.get("captured_at_epoch")
    return {"five_hour": reading.get("five_hour"), "seven_day": reading.get("seven_day"),
            "age_s": int(now - float(cap)) if cap else None, "source": reading.get("_source")}


def summary(now: float | None = None, claude: dict | None = None, codex: dict | None = None) -> dict:
    now = time.time() if now is None else now
    claude = waker.load_quota() if claude is None else claude
    codex = codex_reading(now=now) if codex is None else codex
    return {"claude": {**_view(claude, now), "blocked_until": waker.quota_block(claude, now)},
            "codex": {**_view(codex, now), "blocked_until": block(codex, CODEX_LIMITS, now)}}


def gate(*, now: float | None = None, claude: dict | None = None, codex: dict | None = None,
         live_codex: int | None = None, max_parallel: int = MAX_PARALLEL) -> dict:
    """{"ok": bool, "reasons": [...], "summary": {...}} for starting one more Codex worker."""
    now = time.time() if now is None else now
    s = summary(now, claude, codex)
    reasons = []
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M %z", time.localtime(t))  # noqa: E731
    if s["claude"]["blocked_until"]:
        reasons.append(f"Claude quota spent until {fmt(s['claude']['blocked_until'])}")
    if s["codex"]["blocked_until"]:
        reasons.append(f"Codex quota at its limit (5h >= {CODEX_LIMITS['five_hour']}% or week >= "
                       f"{CODEX_LIMITS['seven_day']}%) until {fmt(s['codex']['blocked_until'])}")
    if live_codex is not None and live_codex >= max_parallel:
        reasons.append(f"{live_codex} Codex harnesses already running (max {max_parallel})")
    return {"ok": not reasons, "reasons": reasons, "summary": s, "live_codex": live_codex}


def live_codex_count(run=None) -> int | None:
    import subprocess

    try:
        out = (run or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=60).stdout))(
            ["harness", "ps", "--json"])
        rows = json.loads(out or "{}").get("harnesses") or []
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    return sum(1 for r in rows if "codex" in str(r.get("agent", "")).lower()
               and r.get("state") not in ("stopped", "closed", None))

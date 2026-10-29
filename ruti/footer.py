"""Compact local Ruti state for the Codex TUI footer (no inference or quota polling)."""

from __future__ import annotations

import argparse
import math
import os
import re
import time
import unicodedata

from . import delegate, modes, runs, sessions, statusline
from .config import read_json

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _clean(value: object, limit: int = 32) -> str:
    text = _ANSI.sub("", str(value))
    text = " ".join("".join(ch for ch in text if not unicodedata.category(ch).startswith("C")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(session_id: str | None = None) -> str:
    """Read cached local state without probes or subprocesses; never raise to the TUI."""
    try:
        session_id = session_id or sessions.current_session_id()
        # Cache only: the shared refresher performs HTTP, nvidia-smi and writes.
        facts = read_json(statusline.FACTS_FILE, default={})
        facts = facts if isinstance(facts, dict) else {}
        state = modes.current(session_id) if session_id else {}
        pieces = [("Ruti OFF" if sessions.is_disabled(session_id) else "Ruti on")
                  if session_id else "Ruti unknown"]
        if state.get("coding"):
            pieces.append("coding")
        if state.get("free", "off") != "off":
            pieces.append("free:" + _clean(state["free"], 8))
        for key, label in (("wait", "wait"), ("flow", "flow"),
                           ("manager", "manager"), ("timemachine", "timemachine"),
                           ("effplus", "eff+")):
            if state.get(key):
                pieces.append(label + "*")

        active = [entry for entry in runs.active(delegate.RUNNING_FILE)
                  if session_id and entry.get("session") == session_id]
        if active:
            entry = active[0]
            alias = _clean(entry.get("model", "?").split("/")[-1], 25)
            started = entry.get("started_at")
            elapsed = (f"{max(0, int(time.time() - started))}s"
                       if _number(started) and started <= time.time() else "unknown")
            pieces.append(f"{len(active)} jobs" if len(active) > 1 else f"{alias} {elapsed}")
        at = facts.get("at")
        freshness = ("unknown" if not _number(at) or at > time.time() else
                     "stale" if time.time() - at >= statusline.FACTS_TTL_SECONDS else "live")
        proxy = facts.get("proxy")
        pieces.append("proxy " + (freshness if freshness != "live" else
                                  "up" if proxy is True else
                                  "down" if proxy is False else "unknown"))
        loaded = facts.get("loaded")
        local = (",".join(_clean(item.get("id") or "unknown", 18) for item in loaded[:2])
                 if freshness == "live" and isinstance(loaded, list) and loaded else
                 freshness if freshness != "live" else "unknown")
        pieces.append("local:" + local)
        gpu = facts.get("gpu")
        if (freshness == "live" and isinstance(gpu, dict)
                and _number(gpu.get("free_mib")) and gpu["free_mib"] >= 0):
            pieces.append(f"GPU{gpu['free_mib'] / 1024:.1f}G")
        else:
            pieces.append("GPU " + (freshness if freshness != "live" else "unknown"))
        return _clean(" ".join(pieces), 120)
    except Exception:
        return "Ruti unavailable"


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="Raw Codex thread ID")
    args = parser.parse_args()
    os.environ["RUTI_HOST"] = "codex"
    os.environ["RUTI_SESSION_ID"] = args.session_id
    print(render())


if __name__ == "__main__":
    main()

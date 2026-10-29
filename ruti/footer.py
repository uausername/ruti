"""Compact local Ruti state for the Codex TUI footer (no inference or quota polling)."""

from __future__ import annotations

import argparse
import os
import re
import time
import unicodedata

from . import delegate, modes, runs, sessions, statusline

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _clean(value: object, limit: int = 32) -> str:
    text = _ANSI.sub("", str(value))
    text = " ".join("".join(ch for ch in text if not unicodedata.category(ch).startswith("C")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(session_id: str | None = None) -> str:
    """Read only local state and bounded loopback health checks; never raise to the TUI."""
    try:
        session_id = session_id or sessions.current_session_id()
        facts = statusline._refresh_facts()
        state = modes.current(session_id)
        pieces = ["Ruti OFF" if sessions.is_disabled(session_id) else "Ruti on"]
        if state.get("coding"):
            pieces.append("coding")
        if state.get("free", "off") != "off":
            pieces.append("free:" + _clean(state["free"], 8))

        active = [entry for entry in runs.active(delegate.RUNNING_FILE)
                  if session_id and entry.get("session") == session_id]
        if active:
            entry = active[0]
            alias = _clean(entry.get("model", "?").split("/")[-1], 25)
            elapsed = max(0, int(time.time() - float(entry.get("started_at", time.time()))))
            pieces.append(f"{len(active)} jobs" if len(active) > 1 else f"{alias} {elapsed}s")
        pieces.append("proxy up" if facts.get("proxy") else "proxy down")
        loaded = facts.get("loaded") or []
        local = ",".join(_clean(item.get("id", "?"), 18) for item in loaded[:2]) or "none"
        pieces.append("local:" + local)
        gpu = facts.get("gpu")
        if isinstance(gpu, dict) and isinstance(gpu.get("free_mib"), (int, float)):
            pieces.append(f"GPU{gpu['free_mib'] / 1024:.1f}G")
        return _clean(" ".join(pieces), 120)
    except Exception:
        return "Ruti unavailable"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="Raw Codex thread ID")
    args = parser.parse_args()
    os.environ["RUTI_HOST"] = "codex"
    os.environ["RUTI_SESSION_ID"] = args.session_id
    print(render())


if __name__ == "__main__":
    main()

"""How full each session's context window is, as the status line last saw it.

The status line is the only place Claude Code reports `context_window.used_percentage`,
and the model never sees the status line. The prompt hook reads this back so the manager
is told, in numbers, when a conversation is past the point where it should wrap up --
the global CLAUDE.md asks for that at 50%, and without a reading the model can only
guess.

Keyed by session id rather than read from `quota.json`: that file is machine-wide, the
last session to repaint wins, and with two sessions open it would hand one the other's
number.
"""

from __future__ import annotations

import time
from typing import Any

from .config import STATE_ROOT, read_json, write_json

CONTEXT_FILE = STATE_ROOT / "context.json"

# The line the global CLAUDE.md draws; the status line turns amber at the same point.
WARN_PERCENT = 50.0

MAX_AGE_SECONDS = 7 * 86400


def _load() -> dict[str, Any]:
    data = read_json(CONTEXT_FILE, default={})
    return data if isinstance(data, dict) else {}


def record(session_id: str | None, used_percentage: Any) -> None:
    """Remember a session's reading. Runs on every repaint, so it never raises."""
    try:
        if not session_id or used_percentage is None:
            return
        value = float(used_percentage)
        data = _load()
        previous = data.get(session_id)
        # The status line repaints every few seconds; the percentage moves once a turn.
        if isinstance(previous, dict) and previous.get("used_percentage") == value:
            return
        now = time.time()
        data[session_id] = {"used_percentage": value, "at": now}
        data = {
            sid: entry for sid, entry in data.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("at"), (int, float))
            and now - entry["at"] < MAX_AGE_SECONDS
        }
        write_json(CONTEXT_FILE, data)
    except Exception:
        pass


def used(session_id: str | None) -> float | None:
    try:
        if not session_id:
            return None
        entry = _load().get(session_id)
        if not isinstance(entry, dict):
            return None
        return float(entry["used_percentage"])
    except Exception:
        return None


def _flow_on(session_id: str | None) -> bool:
    try:
        from . import modes  # imported here: modes is not needed by the status line path

        return bool(modes.current(session_id).get("flow"))
    except Exception:
        return False


def warning(session_id: str | None) -> str:
    """One line for the prompt hook, or "" while the session is under the line."""
    pct = used(session_id)
    if pct is None or pct < WARN_PERCENT:
        return ""
    if _flow_on(session_id):
        return (
            f"ruti context -- {pct:.0f}% of this conversation's context window is used, "
            f"past the {WARN_PERCENT:.0f}% line, and flow mode is on: finish the step in "
            "hand, write the handoff with `ruti flow handoff`, and end the turn -- a "
            "fresh session opens in a new window and continues from it."
        )
    return (
        f"ruti context -- {pct:.0f}% of this conversation's context window is used, "
        f"past the {WARN_PERCENT:.0f}% line. Do not start new substantive work: "
        "summarise what is done and what is left, and offer the user the choice of "
        "/compact or a fresh session -- unless they have already chosen to continue."
    )

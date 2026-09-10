"""Per-session on/off toggle for routing and delegation.

Scoped to a single Claude Code session, deliberately not to CLAUDE.md: a `ruti off`
here must not follow the user into every other project or every future session, and
it must still survive *this* session's own context compaction, which a purely verbal
"don't delegate" does not. `CLAUDE_CODE_SESSION_ID` is the environment variable Claude
Code exports to every tool subprocess -- the same id the hooks receive as `session_id`
on stdin -- so it is what ties a `ruti off` run from a Bash tool call to the
`UserPromptSubmit` hook that reads the toggle back on every later prompt.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .config import STATE_ROOT, file_lock, read_json, write_json

SESSIONS_FILE = STATE_ROOT / "sessions.json"

ENV_VAR = "CLAUDE_CODE_SESSION_ID"

# An entry this old belongs to a session that is long over; kept only so a crashed
# session doesn't leave a phantom toggle that nothing will ever clear.
MAX_AGE_SECONDS = 7 * 86400


def current_session_id() -> str | None:
    return os.environ.get(ENV_VAR) or None


def _load() -> dict[str, Any]:
    data = read_json(SESSIONS_FILE, default={})
    return data if isinstance(data, dict) else {}


def is_disabled(session_id: str | None) -> bool:
    if not session_id:
        return False
    return bool(_load().get(session_id, {}).get("disabled"))


def set_disabled(session_id: str, disabled: bool) -> None:
    with file_lock("sessions", timeout=10.0):
        data = _load()
        record = data.get(session_id) or {}
        if disabled:
            record["disabled"] = True
        else:
            record.pop("disabled", None)
        record["at"] = time.time()
        # Keep the record only while it still carries something worth remembering;
        # `ruti on` with no mode set should leave sessions.json as it found it.
        meaningful = (
            record.get("disabled")
            or record.get("coding")
            or record.get("free", "off") != "off"
        )
        if meaningful:
            data[session_id] = record
        else:
            data.pop(session_id, None)
        write_json(SESSIONS_FILE, data)


def prune(max_age: float = MAX_AGE_SECONDS) -> None:
    """Drop entries old enough that the session behind them cannot still be open."""
    with file_lock("sessions", timeout=10.0):
        data = _load()
        cutoff = time.time() - max_age
        kept = {sid: rec for sid, rec in data.items() if rec.get("at", 0) >= cutoff}
        if kept != data:
            write_json(SESSIONS_FILE, kept)

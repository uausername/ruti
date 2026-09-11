"""Per-session task modes: a coding mode and a free-models mode.

`ruti` is not only for programming. When the work in a session *is* programming, the
manager should lean on the coding harness and on coding-tuned models; when the user
wants to stay off paid metered APIs, delegation should prefer zero-cost ones. Both
are session-scoped for the same reason `ruti off` is (see `sessions.py`): a mode set
while working on one project must not follow the user into every other session, but
it must survive *this* session's own context compaction, which a purely verbal
instruction does not.

State rides in the same `sessions.json` as the on/off toggle, one record per
session:

    {"<session-id>": {"disabled": bool, "coding": bool,
                      "free": "off" | "soft" | "hard", "at": <unix ts>}}

`at` is refreshed on every write so `sessions.prune()` does not discard an active
mode as if it were a stale toggle from a session long over.
"""

from __future__ import annotations

import time
from typing import Any

from .config import file_lock, read_json, write_json
from .sessions import SESSIONS_FILE

# "soft" deprioritises paid APIs and warns; "hard" refuses them outright.
FREE_LEVELS: tuple[str, ...] = ("off", "soft", "hard")

DEFAULTS: dict[str, Any] = {"coding": False, "free": "off"}


def _load() -> dict[str, Any]:
    data = read_json(SESSIONS_FILE, default={})
    return data if isinstance(data, dict) else {}


def _normalise_free(value: Any) -> str:
    if value is True:  # tolerate an older on/off boolean
        return "soft"
    if value in FREE_LEVELS:
        return str(value)
    return "off"


def current(session_id: str | None) -> dict[str, Any]:
    """The modes in effect for this session. No session or no record -> defaults."""
    if not session_id:
        return dict(DEFAULTS)
    record = _load().get(session_id) or {}
    return {
        "coding": bool(record.get("coding", DEFAULTS["coding"])),
        "free": _normalise_free(record.get("free", DEFAULTS["free"])),
    }


def set_coding(session_id: str, on: bool) -> None:
    _update(session_id, "coding", bool(on))


def set_free(session_id: str, level: str) -> None:
    if level not in FREE_LEVELS:
        raise ValueError(f"free level must be one of {FREE_LEVELS}, not {level!r}")
    _update(session_id, "free", level)


def _update(session_id: str, key: str, value: Any) -> None:
    with file_lock("sessions", timeout=10.0):
        data = _load()
        record = data.get(session_id) or {}
        record[key] = value
        record["at"] = time.time()
        data[session_id] = record
        write_json(SESSIONS_FILE, data)


def active_summary(modes: dict[str, Any]) -> str:
    """A compact 'coding, free:hard' description, or '' when nothing is on."""
    parts: list[str] = []
    if modes.get("coding"):
        parts.append("coding")
    if modes.get("free", "off") != "off":
        parts.append(f"free:{modes['free']}")
    return ", ".join(parts)

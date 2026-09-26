"""Per-session task modes: coding, free models, the classifier, and the council.

`ruti` is not only for programming. When the work in a session *is* programming, the
manager should lean on the coding harness and on coding-tuned models; when the user
wants to stay off paid metered APIs, delegation should prefer zero-cost ones; when a
session should stop sending task descriptions off the machine, the classifier goes
quiet; and when the work is full of genuinely ambiguous calls, a council can stand for
them. All are session-scoped for the same reason `ruti off` is (see `sessions.py`): a
mode set while working on one project must not follow the user into every other
session, but it must survive *this* session's own context compaction, which a purely
verbal instruction does not.

Three of the four save money or context. `council` is the exception -- it spends more
on purpose -- which is why it is the only one that defaults to off and has an `auto`
level that has to justify each convening before it happens.

State rides in the same `sessions.json` as the on/off toggle, one record per
session:

    {"<session-id>": {"disabled": bool, "coding": bool,
                      "free": "off" | "soft" | "hard", "jev": bool,
                      "council": "off" | "on" | "auto", "wait": bool,
                      "wait_state": {...}, "at": <unix ts>}}

`at` is refreshed on every write so `sessions.prune()` does not discard an active
mode as if it were a stale toggle from a session long over.
"""

from __future__ import annotations

import time
from typing import Any

from .config import STATE_ROOT, file_lock, read_json, write_json
from .sessions import SESSIONS_FILE

# The user's own defaults, layered over `DEFAULTS` below. Consulted by `current()` for
# any mode a session has not set itself, so a default reaches every session -- including
# ones already running that never touched that mode -- and an explicit `ruti mode ...`
# in a session always wins. No SessionStart step: a hook that copied defaults into each
# new session would miss sessions it never saw start.
DEFAULTS_FILE = STATE_ROOT / "mode-defaults.json"

# "soft" deprioritises paid APIs and warns; "hard" refuses them outright.
FREE_LEVELS: tuple[str, ...] = ("off", "soft", "hard")

# "on" stands a council for every judgement call; "auto" asks the classifier whether
# this particular question is ambiguous and consequential enough to be worth N answers.
# A council is the one feature here that deliberately spends more rather than less, so
# it stays off unless a session says otherwise.
COUNCIL_LEVELS: tuple[str, ...] = ("off", "on", "auto")

# `jev` defaults to on because a classification costs about $0.000025 and can only make
# routing more cautious. It is a switch rather than a setting so that one session can
# stop sending task descriptions off the machine without unsetting a key that the rest
# of the toolchain shares.
DEFAULTS: dict[str, Any] = {"coding": False, "free": "off", "jev": True,
                            "council": "off", "wait": False}

BOOL_MODES: tuple[str, ...] = ("coding", "jev", "wait")
_LEVELS: dict[str, tuple[str, ...]] = {"free": FREE_LEVELS, "council": COUNCIL_LEVELS}
_TRUE, _FALSE = ("on", "true", "1", "yes"), ("off", "false", "0", "no")


def _load() -> dict[str, Any]:
    data = read_json(SESSIONS_FILE, default={})
    return data if isinstance(data, dict) else {}


def validate_default(key: str, value: Any) -> Any:
    """The stored form of `value` for mode `key`; ValueError if it is not one.

    Accepts what a person types (`on`, `off`, `soft`, ...) as well as the stored form,
    so the CLI and a hand-edited file go through the same check.
    """
    if key in BOOL_MODES:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{key} takes on or off, not {value!r}")
    if key in _LEVELS:
        if value in _LEVELS[key]:
            return str(value)
        raise ValueError(f"{key} takes one of {', '.join(_LEVELS[key])}, not {value!r}")
    raise ValueError(f"no mode called {key!r} -- one of {', '.join(DEFAULTS)}")


def user_defaults() -> dict[str, Any]:
    """The overrides the user has set, minus anything a hand edit left invalid."""
    data = read_json(DEFAULTS_FILE, default={})
    if not isinstance(data, dict):
        return {}
    valid: dict[str, Any] = {}
    for key, value in data.items():
        try:
            valid[key] = validate_default(key, value)
        except ValueError:
            continue
    return valid


def effective_defaults() -> dict[str, Any]:
    return {**DEFAULTS, **user_defaults()}


def set_defaults(values: dict[str, Any]) -> dict[str, Any]:
    """Validate every pair first, then write them together -- or none of them."""
    checked = {key: validate_default(key, value) for key, value in values.items()}
    with file_lock("mode-defaults", timeout=10.0):
        write_json(DEFAULTS_FILE, {**user_defaults(), **checked})
    return checked


def clear_defaults(keys: list[str] | None = None) -> None:
    """Drop the named overrides, or all of them."""
    unknown = [key for key in keys or [] if key not in DEFAULTS]
    if unknown:
        raise ValueError(f"no mode called {unknown[0]!r} -- one of {', '.join(DEFAULTS)}")
    with file_lock("mode-defaults", timeout=10.0):
        kept = {} if not keys else {k: v for k, v in user_defaults().items() if k not in keys}
        write_json(DEFAULTS_FILE, kept)


def _normalise_free(value: Any) -> str:
    if value is True:  # tolerate an older on/off boolean
        return "soft"
    if value in FREE_LEVELS:
        return str(value)
    return "off"


def _normalise_council(value: Any) -> str:
    if value is True:  # tolerate an older on/off boolean
        return "on"
    if value in COUNCIL_LEVELS:
        return str(value)
    return "off"


def current(session_id: str | None) -> dict[str, Any]:
    """The modes in effect for this session: its own settings, then the user's
    defaults (`ruti defaults`), then the built-in ones."""
    defaults = effective_defaults()
    if not session_id:
        return defaults
    record = _load().get(session_id) or {}
    return {
        "coding": bool(record.get("coding", defaults["coding"])),
        "free": _normalise_free(record.get("free", defaults["free"])),
        "jev": bool(record.get("jev", defaults["jev"])),
        "council": _normalise_council(record.get("council", defaults["council"])),
        "wait": bool(record.get("wait", defaults["wait"])),
    }


def set_coding(session_id: str, on: bool) -> None:
    _update(session_id, "coding", bool(on))


def set_jev(session_id: str, on: bool) -> None:
    _update(session_id, "jev", bool(on))


def set_wait(session_id: str, on: bool) -> None:
    _update(session_id, "wait", bool(on))
    if not on:
        # A pause left over from before `off` must not resurface if wait is turned
        # back on later in the same window.
        _update(session_id, "wait_state", {})


def wait_state(session_id: str | None) -> dict[str, Any]:
    """Wait mode's bookkeeping for this session: which window was noticed/paused."""
    if not session_id:
        return {}
    value = (_load().get(session_id) or {}).get("wait_state")
    return value if isinstance(value, dict) else {}


def set_wait_state(session_id: str, state: dict[str, Any]) -> None:
    _update(session_id, "wait_state", state)


def set_council(session_id: str, level: str) -> None:
    if level not in COUNCIL_LEVELS:
        raise ValueError(f"council level must be one of {COUNCIL_LEVELS}, not {level!r}")
    _update(session_id, "council", level)


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
    if modes.get("council", "off") != "off":
        parts.append(f"council:{modes['council']}")
    if modes.get("wait"):
        parts.append("wait")
    return ", ".join(parts)


def coding_aliases() -> list[tuple[str, bool | None]]:
    """(alias, free) for every enabled registered alias that is tuned for code."""
    from . import openrouter, providers

    try:
        records = providers.load_registry()["providers"]
    except Exception:  # the prompt hook calls this: a bad registry must not break it
        return []
    out: list[tuple[str, bool | None]] = []
    seen: set[str] = set()
    for record in records:
        alias = record.get("alias")
        if not alias or alias in seen or not record.get("enabled", True):
            continue
        seen.add(alias)
        # route rules out an alias without verified tool calling; naming it here would
        # point the manager at an executor it cannot use.
        if openrouter.is_coding_record(record) and record.get("supports_tools"):
            out.append((alias, record.get("free")))
    return out


def coding_note(free_level: str, aliases: list[tuple[str, bool | None]] | None = None) -> str:
    """What coding mode tells the manager -- never contradicting the free mode.

    It used to name `pareto-code` unconditionally, which free mode (hard) refuses: the
    hint pointed at the one executor `route` had just ruled out. So the aliases come
    from the registry, split by price, and a paid one is only named where the free
    level permits it.
    """
    aliases = coding_aliases() if aliases is None else aliases
    free = ", ".join(f"`{a}`" for a, is_free in aliases if is_free is True)
    paid = ", ".join(f"`{a}`" for a, is_free in aliases if is_free is not True)
    lead = "ruti coding mode is ON: when you delegate implementation, prefer"

    if free_level == "hard":
        if not free:
            return ("ruti coding mode is ON, but no zero-cost coding alias is registered and "
                    "free mode (hard) refuses paid ones -- use general `*:free` aliases, or "
                    "register a free coding model with `ruti openrouter setup`.")
        # The metered ones are not named at all: this mode refuses them.
        return f"{lead} the zero-cost coding aliases ({free}) over general-purpose ones."
    if free_level == "soft":
        if not free and not paid:
            return ("ruti coding mode is ON, but no coding alias is registered -- "
                    "`ruti openrouter setup` registers free coding models.")
        if not free:
            return (f"{lead} the coding aliases ({paid}), but they are metered: warn the "
                    "user before using one (free mode is soft).")
        return (f"{lead} the zero-cost coding aliases ({free}) over general-purpose ones"
                + (f"; the metered ones ({paid}) only after warning the user." if paid else "."))
    if not free and not paid:
        return ("ruti coding mode is ON, but no coding alias is registered -- "
                "`ruti openrouter setup` registers `pareto-code` and free coding models.")
    listed = ", ".join(part for part in (paid, free) if part)
    return f"{lead} the coding-tuned aliases ({listed}) over general-purpose ones."

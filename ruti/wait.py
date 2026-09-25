"""Wait mode: ride the five-hour window to its edge, pause, and pick up after the reset.

There is no paid overage on the account this is built for, so hitting 100% mid-task is
a hard stop that loses the turn. Wait mode trades the last few percent for a clean
break instead, in three steps, all driven by hooks so none of it depends on the
manager remembering a rule it read an hour ago:

* **90%** -- `PostToolUse` injects one notice per window: assess what is open, finish
  only what fits, start nothing large, keep the task list current.
* **95%** -- `PreToolUse` refuses every tool call except `ruti` itself and TodoWrite,
  telling the manager to write a checkpoint into its reply and end the turn. The
  refusal marks the session paused for this window.
* **Reset** -- the `Stop` hook sees the pause, sleeps until the window's `resets_at`
  (plus a margin), then answers `{"decision": "block"}` with a resume instruction.
  Blocking a stop is the one way a hook can make Claude Code keep going, and because
  the hook is still running, it is the *same* interactive session that continues --
  no `--resume`, no second process racing the TUI for the transcript.

Why the reset time rather than watching the percentage fall back to zero: the status
line only learns a new number from an API response, and nothing is sent while the
session is paused. A reading whose `resets_at` has passed is, by definition, a window
that has reset -- `effective_used` counts it as 0%.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from . import modes, quota

NOTICE_AT = 90.0
PAUSE_AT = 95.0

# Past the reset before resuming: clocks disagree, and resuming a few seconds early
# would hit the old window at 95% and pause straight back into another five hours.
RESUME_MARGIN_SECONDS = 90

# Never sleep longer than one window plus change. A reset time further out than this
# is a bad reading, not a reason to hold the session for a day.
MAX_WAIT_SECONDS = 5 * 3600 + 15 * 60

# How often the sleeping Stop hook checks whether it has been called off.
POLL_SECONDS = 30

# The Stop hook's registered timeout: long enough for a full window and the margin.
STOP_HOOK_TIMEOUT = MAX_WAIT_SECONDS + 15 * 60

# Still allowed while paused: `ruti` itself (so `ruti mode wait off` works) and the
# task list, which is where the checkpoint is best kept.
ALWAYS_ALLOWED_TOOLS = frozenset({"TodoWrite"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_SHELL_CHAINING = ("&", "|", ";", "`", "$(", "\n", ">", "<")


def effective_used(snapshot: quota.Quota) -> float | None:
    """Five-hour utilisation, with a window whose reset has passed counted as 0%."""
    window = snapshot.five_hour
    if window is None or snapshot.freshness == "never":
        return None
    remaining = window.resets_in_seconds
    if remaining is not None and remaining <= 0:
        return 0.0
    return window.used_percentage


def window_key(snapshot: quota.Quota) -> str:
    """Identifies the current window, so a notice or pause is per window, not forever."""
    window = snapshot.five_hour
    return str(window.resets_at) if window is not None and window.resets_at else ""


def reset_clock(snapshot: quota.Quota) -> str:
    """Local HH:MM of the reset, or '?' when the reading has no reset time."""
    window = snapshot.five_hour
    remaining = window.resets_in_seconds if window is not None else None
    if remaining is None:
        return "?"
    return datetime.fromtimestamp(time.time() + remaining).strftime("%H:%M")


def _ruti_only(command: str) -> bool:
    command = command.strip()
    return (command == "ruti" or command.startswith("ruti ")) and not any(
        token in command for token in _SHELL_CHAINING)


def tool_allowed_while_paused(tool_name: str, tool_input: Any) -> bool:
    if tool_name in ALWAYS_ALLOWED_TOOLS:
        return True
    if tool_name in SHELL_TOOLS and isinstance(tool_input, dict):
        return _ruti_only(str(tool_input.get("command") or ""))
    return False


def pre_tool_use(session_id: str, payload: dict[str, Any],
                 snapshot: quota.Quota) -> dict[str, Any] | None:
    """Refuse the tool call once the window is at the pause line."""
    used = effective_used(snapshot)
    if used is None or used < PAUSE_AT:
        return None
    if tool_allowed_while_paused(str(payload.get("tool_name") or ""),
                                 payload.get("tool_input")):
        return None

    key = window_key(snapshot)
    state = modes.wait_state(session_id)
    if state.get("paused") != key:
        modes.set_wait_state(session_id, {**state, "paused": key, "noticed": key})

    clock = reset_clock(snapshot)
    resume = (f"ruti will resume this session automatically after the reset at {clock}"
              if clock != "?" else
              "the reset time is unknown, so ruti cannot resume automatically -- tell "
              "the user to send 'continue' once the window resets")
    reason = (
        f"ruti wait mode: {used:.0f}% of the 5h window used -- paused at {PAUSE_AT:.0f}%. "
        "Do not call any more tools. Write a checkpoint in your reply: what is done, "
        "what was in progress and exactly where it stopped, and the remaining steps in "
        f"order. Then end the turn; {resume}. (`ruti mode wait off` overrides.)"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def post_tool_use(session_id: str, snapshot: quota.Quota) -> dict[str, Any] | None:
    """Once per window, at the notice line: assess the open work before the pause."""
    used = effective_used(snapshot)
    if used is None or used < NOTICE_AT or used >= PAUSE_AT:
        return None
    key = window_key(snapshot)
    state = modes.wait_state(session_id)
    if state.get("noticed") == key:
        return None
    modes.set_wait_state(session_id, {**state, "noticed": key})
    context = (
        f"ruti wait mode: {used:.0f}% of the 5h window used (resets {reset_clock(snapshot)}). "
        "Assess the open tasks now: finish only what fits before "
        f"{PAUSE_AT:.0f}%, start no large new step, and keep the task list current. At "
        f"{PAUSE_AT:.0f}% tool calls are refused and the session pauses until the reset, "
        "then resumes automatically -- so leave the work at a point that is easy to "
        "pick up from."
    )
    return {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}
    }


def stop(session_id: str, snapshot: quota.Quota, *,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.time) -> dict[str, Any] | None:
    """If this session paused in the current window, wait out the reset, then resume."""
    state = modes.wait_state(session_id)
    key = window_key(snapshot)
    if not state.get("paused") or state.get("paused") != key:
        return None

    window = snapshot.five_hour
    remaining = window.resets_in_seconds if window is not None else None
    if remaining is None:
        modes.set_wait_state(session_id, {**state, "paused": None})
        return {"systemMessage": "ruti wait mode: the reset time is unknown, so the "
                                 "session cannot resume by itself -- send 'continue' "
                                 "after the window resets."}

    wait_for = min(max(remaining, 0.0) + RESUME_MARGIN_SECONDS, MAX_WAIT_SECONDS)
    deadline = clock() + wait_for
    while clock() < deadline:
        sleep(min(POLL_SECONDS, max(deadline - clock(), 0.0)))
        # Called off while sleeping: wait mode turned off, or the pause cleared.
        if not modes.current(session_id).get("wait"):
            return None
        if modes.wait_state(session_id).get("paused") != key:
            return None

    modes.set_wait_state(session_id, {**modes.wait_state(session_id), "paused": None})
    return {
        "decision": "block",
        "reason": (
            "ruti wait mode: the 5-hour window has reset. Continue the task from the "
            "checkpoint you wrote before pausing -- re-read it above, then carry on with "
            "the next remaining step."
        ),
    }


def prompt_note(session_id: str | None, snapshot: quota.Quota) -> str:
    """The line the prompt hook adds while wait mode is on."""
    state = modes.wait_state(session_id)
    key = window_key(snapshot)
    used = effective_used(snapshot)
    if key and state.get("paused") == key and used is not None and used >= PAUSE_AT:
        return (f"ruti wait mode is ON and PAUSED until the reset at {reset_clock(snapshot)}: "
                "tool calls are refused until then (`ruti mode wait off` overrides).")
    return (f"ruti wait mode is ON: at {NOTICE_AT:.0f}% you will be asked to assess the "
            f"open tasks; at {PAUSE_AT:.0f}% tools are refused -- write a checkpoint and "
            "end the turn, and the session resumes by itself after the reset.")

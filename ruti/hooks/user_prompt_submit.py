"""Tell the manager how much budget it has left, on every prompt, for almost nothing.

Without this the manager is blind between explicit `ruti status` calls, and asking it
to check costs a tool call and a round trip -- which is itself budget.

The debounce is the point. An unconditional block of policy text on every prompt would
be a few hundred tokens times however many prompts a session has, which is a real leak
in a tool whose entire purpose is to stop leaks. So: one line normally, and the full
policy only when the band changes or occasionally as a reminder.
"""

from __future__ import annotations

import json
import sys
import time

from ruti import modes, quota, sessions
from ruti.config import STATE_ROOT, read_json, write_json

MEMO_FILE = STATE_ROOT / "hook-memo.json"

# How many prompts may pass before the full policy is restated even if nothing changed.
FULL_EVERY = 12


def build_context() -> tuple[str, bool]:
    # A session-scoped `ruti off` means routing advice is not just unhelpful here, it's
    # actively wrong -- so replace the whole budget block with a one-line reminder
    # rather than layering it on top.
    session_id = sessions.current_session_id()
    if sessions.is_disabled(session_id):
        return (
            "ruti: OFF for this session -- `route` and `delegate` refuse. Do "
            "implementation work in-session. Run `ruti on` to resume delegation.",
            False,
        )

    snapshot = quota.load()
    memo = read_json(MEMO_FILE, default={}) or {}

    band = snapshot.band
    count = int(memo.get("prompts_since_full", 0)) + 1
    changed = memo.get("last_band") != band
    full = changed or count >= FULL_EVERY

    if snapshot.five_hour is None and snapshot.freshness in ("never", "unknown"):
        # Saying nothing is better than asserting a number we do not have.
        line = ("ruti: subscription usage is unknown (no status-line reading yet). "
                "Treat the budget as tight until one arrives.")
    else:
        line = f"ruti budget -- {snapshot.summary()}"

    if full:
        policy = snapshot.policy
        executors = ", ".join(policy["anthropic_executors"]) or "none"
        line += (
            f"\nManager for this band: {policy['manager']}. "
            f"Anthropic executors permitted: {executors}. "
            f"{policy['guidance']} "
            "Call `ruti route --kind ... --files N --loc N --json` before starting "
            "substantial implementation work, and run delegates through `ruti delegate` "
            "so their output never enters this context."
        )

    # Modes are short and only matter while active, so they ride on every prompt they
    # are set for rather than following the band debounce.
    for note in _mode_notes(session_id):
        line += "\n" + note

    write_json(MEMO_FILE, {
        "last_band": band,
        "prompts_since_full": 0 if full else count,
        "at": time.time(),
    })
    return line, full


def _mode_notes(session_id: str | None) -> list[str]:
    state = modes.current(session_id)
    notes: list[str] = []
    if state["coding"]:
        notes.append(
            "ruti coding mode is ON: when you delegate implementation to subagents, "
            "prefer the `pareto-code` alias (OpenRouter's Pareto coding router) and "
            "other coding-tuned models over general-purpose ones."
        )
    if state["free"] == "soft":
        notes.append(
            "ruti free mode is ON (soft): prefer zero-cost models (`free` router, "
            "`*:free` aliases) when delegating, and warn the user before using a paid "
            "metered API. `ruti route` deprioritises paid APIs but still lists them."
        )
    elif state["free"] == "hard":
        notes.append(
            "ruti free mode is ON (hard): delegate only to zero-cost models (`free` "
            "router, `*:free` aliases). `ruti route` rules out paid metered APIs and "
            "`ruti delegate` refuses them."
        )
    return notes


def main() -> int:
    try:
        sys.stdin.read()  # payload is not needed, but must be drained
    except Exception:
        pass

    try:
        context, _ = build_context()
    except Exception:
        # A hook that fails must not block the prompt.
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

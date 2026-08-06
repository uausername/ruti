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

from ruti import quota
from ruti.config import STATE_ROOT, read_json, write_json

MEMO_FILE = STATE_ROOT / "hook-memo.json"

# How many prompts may pass before the full policy is restated even if nothing changed.
FULL_EVERY = 12


def build_context() -> tuple[str, bool]:
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

    write_json(MEMO_FILE, {
        "last_band": band,
        "prompts_since_full": 0 if full else count,
        "at": time.time(),
    })
    return line, full


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

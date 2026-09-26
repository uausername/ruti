"""The PreToolUse, PostToolUse and Stop hook for wait mode and flow mode.

One module for both because both are registered on the same three events, and a second
registration would be a second Python start on every tool call. See `ruti.wait` and
`ruti.flow` for what each mode does.

Registered for every session, so the common path -- both modes off -- has to cost one
look at the session's modes and nothing else. And like every ruti hook it fails open: a
hook that raised on each tool call would brick the manager, which is worse than any
pause or missed handoff.

Where both modes are on, wait goes first: its refusal at 95% is about the hard stop of
the whole window, and its Stop hook may sleep until the reset and then resume this very
session -- a handoff launched before that would put two sessions on one task.
"""

from __future__ import annotations

import json
import sys


def handle(payload: dict) -> dict | None:
    from ruti import modes

    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None
    active = modes.current(session_id)
    wait_on, flow_on = active.get("wait"), active.get("flow")
    if not (wait_on or flow_on):
        return None

    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        if wait_on:
            from ruti import quota, wait

            refused = wait.pre_tool_use(session_id, payload, quota.load())
            if refused:
                return refused
        if flow_on:
            from ruti import flow

            return flow.pre_tool_use(session_id, payload)
        return None

    if event == "PostToolUse":
        notes = []
        if wait_on:
            from ruti import quota, wait

            notes.append(wait.post_tool_use(session_id, quota.load()))
        if flow_on:
            from ruti import flow

            notes.append(flow.post_tool_use(session_id))
        texts = [n["hookSpecificOutput"]["additionalContext"] for n in notes if n]
        if not texts:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "additionalContext": "\n".join(texts)}}

    if event == "Stop":
        if wait_on:
            from ruti import quota, wait

            resumed = wait.stop(session_id, quota.load())
            if resumed:
                return resumed
        if flow_on:
            from ruti import flow

            return flow.stop(session_id, payload)
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle(payload) if isinstance(payload, dict) else None
    except Exception:
        return 0
    if result:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

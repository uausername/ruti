"""Wait mode's hook: one module for PreToolUse, PostToolUse and Stop (see `ruti.wait`).

Registered for every session, so the common path -- wait mode off -- has to cost one
read of sessions.json and nothing else. And like every ruti hook it fails open: a hook
that raised on each tool call would brick the manager, which is worse than any pause.
"""

from __future__ import annotations

import json
import sys


def handle(payload: dict) -> dict | None:
    from ruti import modes, quota, wait

    session_id = str(payload.get("session_id") or "")
    if not session_id or not modes.current(session_id).get("wait"):
        return None

    snapshot = quota.load()
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        return wait.pre_tool_use(session_id, payload, snapshot)
    if event == "PostToolUse":
        return wait.post_tool_use(session_id, snapshot)
    if event == "Stop":
        return wait.stop(session_id, snapshot)
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

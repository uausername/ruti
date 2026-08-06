"""Surface broken plumbing at the start of a session rather than mid-delegation.

Every failure this reports is one that otherwise stays quiet: a stopped LM Studio
server, an unrepaired certificate chain, a model list that no longer matches disk.
Delegation into a broken chain does not error -- it falls back to a remote provider and
returns a plausible answer -- so the only cheap moment to notice is before any work
has been assigned.

Deliberately silent when everything is healthy: a banner that always prints is a
banner nobody reads.
"""

from __future__ import annotations

import json
import sys


def build_report() -> tuple[str, str] | None:
    """Returns (message for the user, context for the model), or None if all is well."""
    from ruti import doctor

    report = doctor.run_checks()
    problems = [c for c in report.checks if c.status != doctor.OK]
    if not problems:
        return None

    lines = []
    for check in problems:
        mark = "BROKEN" if check.status == doctor.BAD else "warning"
        lines.append(f"  [{mark}] {check.name}: {check.message}")
        if check.detail:
            lines.append(f"           {check.detail}")

    fixable = [c.name for c in report.fixable]
    hint = f"\n  `ruti doctor --fix` can repair: {', '.join(fixable)}" if fixable else ""

    user_message = "ruti found problems with the delegation chain:\n" + "\n".join(lines) + hint
    model_context = (
        "ruti health check at session start found problems that make delegation "
        "unreliable:\n" + "\n".join(lines) + hint +
        "\nDo not delegate work until these are resolved -- a broken local backend does "
        "not fail loudly, it silently answers from a remote provider instead."
    )
    return user_message, model_context


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        report = build_report()
    except Exception:
        return 0
    if report is None:
        return 0

    user_message, model_context = report
    json.dump(
        {
            "systemMessage": user_message,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": model_context,
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

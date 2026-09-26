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
    # Cached unconditionally, healthy or not: the status line reads this passively
    # (`doctor.cached()`) and a stale BAD from a problem fixed since would otherwise
    # sit there for the rest of the session.
    try:
        report.cache()
    except Exception:
        pass

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
    # Only a BAD check stops delegation; warnings are worth knowing about, but telling
    # the manager to stop delegating over them contradicts the policy it is given.
    if any(c.status == doctor.BAD for c in problems):
        verdict = ("\nDo not delegate work until the BROKEN items are resolved -- a broken "
                   "backend does not fail loudly, it silently answers from another provider.")
    else:
        verdict = "\nThese are warnings only; delegation still works."
    model_context = (
        "ruti health check at session start found problems:\n" + "\n".join(lines) + hint +
        verdict
    )
    return user_message, model_context


def _mark_start() -> None:
    """Record where the window stood when this session began."""
    from ruti import ledger, quota

    snapshot = quota.load()
    ledger.record(
        "session_start",
        five_hour=snapshot.five_hour.used_percentage if snapshot.five_hour else None,
        seven_day=snapshot.seven_day.used_percentage if snapshot.seven_day else None,
        band=snapshot.band,
        freshness=snapshot.freshness,
    )

    # A new session gets a new id, so a stale `ruti off` toggle can never affect it --
    # this only keeps sessions.json from accreting entries for sessions long over.
    from ruti import sessions

    sessions.prune()


def main() -> int:
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        payload = parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    try:
        _mark_start()
    except Exception:
        pass

    # Before the health check: a flow continuation that loses its handoff because
    # `doctor` raised would start the new session with no idea what it was doing.
    parts: list[tuple[str, str]] = []
    try:
        from ruti import flow

        continued = flow.session_start(payload)
        if continued:
            parts.append(continued)
    except Exception:
        pass

    try:
        report = build_report()
        if report:
            parts.append(report)
    except Exception:
        pass

    if not parts:
        return 0
    json.dump(
        {
            "systemMessage": "\n\n".join(user for user, _ in parts),
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(model for _, model in parts),
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

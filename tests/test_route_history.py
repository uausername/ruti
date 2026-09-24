"""What came of each ranking: one pairing, read the same way by the report, the
prompt hook and the status line."""

from __future__ import annotations

import json
import re
import time

import pytest
from click.testing import CliRunner

from ruti import cli, doctor, ledger, quota, statusline

S = "session-1"


@pytest.fixture
def write():
    """Append ledger events directly, oldest first, a second apart."""
    clock = [time.time() - 3600]

    def append(event: str, session: str = S, **fields):
        clock[0] += 1
        entry = {"at": clock[0], "event": event, "session": session, **fields}
        with ledger.LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    return append


def _route(write, recommended, **fields):
    return write("route", kind="implement", files=2, loc=120, band="GREEN",
                 recommended=recommended, eligible=3, **fields)


def _delegation(write, model, *, ok=True, substituted=False, **fields):
    return write("delegation", model=model, ok=ok, substituted=substituted, **fields)


def _outcomes(session=S):
    return [row["outcome"] for row in ledger.route_history(ledger.read_events())
            if row["session"] == session]


def test_each_outcome_is_told_apart(write):
    _route(write, "ruti-router/free")
    _delegation(write, "ruti-router/free")                 # followed
    _route(write, "claude:self")                           # in session
    _route(write, "ruti-router/laguna-s-2.1")
    _delegation(write, "ruti-router/inkling-small")        # instead
    _route(write, "ruti-router/north-mini-code")           # not delegated
    assert _outcomes() == ["followed", "in_session", "instead", "not_delegated"]


def test_compliance_counts_are_the_history_counted(write):
    _route(write, "ruti-router/free")
    _route(write, "ruti-router/free")
    _delegation(write, "free")                             # follows both, as before
    _route(write, "claude:self")
    _route(write, "ruti-router/laguna-s-2.1")
    assert ledger.route_compliance(ledger.read_events()) == {
        "total": 4, "followed": 2, "ignored": 1, "recommended_self": 1,
    }


def test_sessions_do_not_follow_each_other(write):
    _route(write, "ruti-router/free")
    _delegation(write, "ruti-router/free", session="another")
    assert _outcomes() == ["not_delegated"]


def test_last_route_and_the_hook_reminder_agree(write):
    assert ledger.last_route(S) is None
    _route(write, "ruti-router/free")
    assert ledger.unfollowed_route(S)["recommended"] == "ruti-router/free"
    _delegation(write, "ruti-router/free")
    assert ledger.last_route(S)["outcome"] == "followed"
    assert ledger.unfollowed_route(S) is None
    _route(write, "claude:self")
    assert ledger.unfollowed_route(S) is None


def test_route_records_the_alternatives(write, registry, monkeypatch):
    from ruti import litellm_cfg, lmstudio, modes, router

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", S)
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models", lambda: ["free", "inkling-small"])
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "hard"})
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    router.rank(router.Task(kind="implement", files=3, loc=300), snapshot)

    (entry,) = ledger.read_events()
    assert entry["modes"] == {"coding": True, "free": "hard"}
    assert 1 <= len(entry["ranked"]) <= 3
    assert entry["ranked"][0]["executor"] == entry["recommended"]
    rejected = {r["executor"]: r["reason"] for r in entry["rejected"]}
    assert "free mode (hard)" in rejected["ruti-router/pareto-code"] \
        or "not served" in rejected["ruti-router/pareto-code"]
    assert "not served" in rejected["ruti-router/gemini-flash-lite"]


def _report(*args):
    result = CliRunner().invoke(cli.main, ["report", *args])
    assert result.exit_code == 0, result.output
    return " ".join((result.output + (result.stderr or "")).split())


def test_report_routes_shows_the_pick_what_was_ruled_out_and_the_outcome(write):
    _route(write, "ruti-router/north-mini-code",
           ranked=[{"executor": "ruti-router/north-mini-code", "score": 1.12},
                   {"executor": "ruti-router/free", "score": 0.9}],
           rejected=[{"executor": "ruti-router/pareto-code",
                      "reason": "free mode (hard) is on and this is a paid metered API"}],
           modes={"coding": True, "free": "hard"})
    _delegation(write, "ruti-router/north-mini-code",
                model_effective="cohere/north-mini-code:free", duration_s=42.0,
                cost_usd=0.0, cost_complete=True)
    _route(write, "ruti-router/free")  # an old-format event: no alternatives recorded

    out = _report("--routes")
    assert "-> ruti-router/north-mini-code" in out
    assert "answered by cohere/north-mini-code:free, ok, 42.0 s, $0.0000" in out
    assert "next: ruti-router/free 0.90" in out
    assert "ruti-router/pareto-code: free mode (hard)" in out
    assert "coding, free:hard" in out
    assert "alternatives were not recorded then" in out
    assert "not delegated" in out


def test_report_routes_json_and_limit(write):
    for _ in range(3):
        _route(write, "claude:self")
    result = CliRunner().invoke(cli.main, ["report", "--routes", "--limit", "2", "--json"])
    rows = json.loads(result.stdout)["routes"]
    assert len(rows) == 2 and all(r["outcome"] == "in_session" for r in rows)


def test_the_summary_says_in_session_not_this_session(write):
    _route(write, "claude:self")
    out = _report()
    assert "1 kept on Claude (in session or a subagent)" in out
    assert "recommended this session" not in out
    assert "ruti report --routes" in out


def _segment(write_events):
    text, run = statusline._route_segment(S)
    return (text and re.sub(r"\x1b\[[0-9;]*m", "", text), run)


@pytest.mark.parametrize("delegation, expected", [
    ({"ok": True}, "→ free ✓"),
    ({"ok": False}, "→ free ✗"),
    ({"ok": True, "substituted": True}, "→ free SUBST"),
])
def test_status_line_shows_a_followed_ranking(write, delegation, expected):
    _route(write, "ruti-router/free")
    run = _delegation(write, "ruti-router/free", **delegation)
    text, shown = _segment(write)
    assert text == expected and shown["at"] == run["at"]


def test_status_line_shows_pending_elsewhere_and_self(write):
    assert statusline._route_segment(S) == (None, None)
    _route(write, "ruti-router/free")
    assert _segment(write)[0] == "→ free …"
    _delegation(write, "ruti-router/laguna-s-2.1")
    assert _segment(write)[0] == "→ free ≠ laguna-s-2.1"
    _route(write, "claude:self")
    assert _segment(write)[0] == "→ self"


def test_status_line_segment_never_raises(monkeypatch):
    monkeypatch.setattr(ledger, "last_route", lambda _s: 1 / 0)
    assert statusline._route_segment(S) == (None, None)


def test_status_line_does_not_repeat_the_run_the_route_segment_shows(write, monkeypatch):
    # `last:` is now read live from the ledger, scoped to this session -- so these are
    # real delegation events, not a faked `_refresh_facts` return.
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {"proxy": True, "loaded": []})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr("ruti.modes.current", lambda _s: {"coding": False, "free": "off"})

    _route(write, "ruti-router/free")
    _delegation(write, "ruti-router/free")
    line = statusline.render({"session_id": S}, quota.Quota(None, None, 0.0))
    assert "→ free ✓" in line and "last:" not in line

    # A second, different delegation after that pairing: no longer the run the route
    # segment already shows, so it earns its own `last:` segment.
    _delegation(write, "ruti-router/inkling-small")
    assert "last:inkling-small" in statusline.render({"session_id": S},
                                                     quota.Quota(None, None, 0.0))


def test_status_line_last_is_scoped_to_this_session_not_the_whole_ledger(write, monkeypatch):
    """The bug this was built to fix: a stale, unrelated failure from a different,
    older session showing up next to this session's own route recommendation as if
    the two were connected."""
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {"proxy": True, "loaded": []})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr("ruti.modes.current", lambda _s: {"coding": False, "free": "off"})

    write("delegation", session="an-older-session", model="ruti-router/north-mini-code",
         ok=False)
    _route(write, "ruti-router/gemini-flash-lite")

    line = statusline.render({"session_id": S}, quota.Quota(None, None, 0.0))
    assert "north-mini-code" not in line
    assert "→ gemini-flash-lite" in line


def test_a_proxy_that_stopped_answering_is_restarted_not_just_started(monkeypatch):
    # A proxy whose accept loop died is still running: `schtasks /Run` alone is ignored.
    from ruti import litellm_cfg

    monkeypatch.setattr(litellm_cfg, "liveliness", lambda *a, **k: False)
    check = doctor._check_proxy_alive()
    assert check.status == doctor.BAD
    assert check.fix is doctor._fix_proxy_restart


def test_a_retry_that_succeeded_is_what_is_shown(write):
    # The flow CLAUDE.md prescribes: substituted, `ruti doctor --fix`, retry.
    _route(write, "ruti-router/free")
    _delegation(write, "ruti-router/free", substituted=True)
    retry = _delegation(write, "ruti-router/free")
    route = ledger.last_route(S)
    assert route["delegation"]["at"] == retry["at"] and route["attempts"] == 2
    assert _segment(write)[0] == "→ free ✓"
    assert "delegated (2 attempts, latest)" in _report("--routes")


@pytest.mark.parametrize("recommended, text", [
    (None, "nothing was eligible"),
    ("claude:self", "recommended writing it in session"),
    ("claude:haiku", "recommended a haiku subagent (ruti does not see subagents)"),
])
def test_report_never_claims_how_undelegated_work_was_done(write, recommended, text):
    _route(write, recommended)
    out = _report("--routes")
    assert text in out and "as recommended" not in out

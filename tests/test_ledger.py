"""What the session cost tally counts, and what it honestly does not.

`session_jev_cost` sums the one thing actually known cheaply: classification calls and
the council's own gate/judge. It does *not* include what a convened council's member
models cost -- pulling that from the proxy's usage log needs a multi-second settle per
model (`usage.collect`), which would tax every `ruti council` call whether or not
anyone reads the total. The tests here pin the boundary, not just the arithmetic.
"""

from __future__ import annotations

import pytest

from ruti import ledger


def test_sums_classifier_cost_from_route_events(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("route", kind="implement", classifier={"cost_usd": 0.000026})
    ledger.record("route", kind="refactor", classifier={"cost_usd": 0.000024})
    assert ledger.session_jev_cost("s1") == pytest.approx(0.00005)


def test_a_route_event_with_no_classifier_contributes_nothing(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("route", kind="implement")  # --kind/--files asserted, no --describe
    assert ledger.session_jev_cost("s1") == 0.0


def test_sums_council_gate_and_judge_spend(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("jev_spend", purpose="council_gate", cost_usd=0.000025)
    ledger.record("jev_spend", purpose="council_judge", cost_usd=0.000021)
    assert ledger.session_jev_cost("s1") == pytest.approx(0.000046)


def test_a_declined_council_still_counted_the_gate(monkeypatch):
    """The gate spends $0.000025 whether or not it convenes -- a refused council that
    saved money on the members must not also look free on the ledger."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("jev_spend", purpose="council_gate", cost_usd=0.000025)
    assert ledger.session_jev_cost("s1") == 0.000025


def test_does_not_count_a_convened_councils_member_answers(monkeypatch):
    """The boundary this function is honest about: `delegation`-shaped events (which
    is how a convened council's own answers would eventually be tracked, matching
    `delegate.py`'s pattern) are never summed here, on purpose."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("delegation", model="gemini-flash", cost_usd=1.5)
    assert ledger.session_jev_cost("s1") == 0.0


def test_other_sessions_spend_is_not_counted(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
    ledger.record("jev_spend", purpose="council_gate", cost_usd=0.5)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert ledger.session_jev_cost("s1") == 0.0


def test_no_session_id_is_zero_not_an_error():
    assert ledger.session_jev_cost(None) == 0.0


def test_no_ledger_file_yet_is_zero_not_an_error():
    assert ledger.session_jev_cost("some-session-nothing-has-written-to-yet") == 0.0

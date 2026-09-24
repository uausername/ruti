"""Six segments that were silent before: the six that add nothing costly.

None of these may block a repaint or raise. `render()` is exercised with
`_refresh_facts` stubbed so no test touches the network, LM Studio, or nvidia-smi --
those are `_refresh_facts`'s own job and are not what changed here.
"""

from __future__ import annotations

import re
import time

import pytest

from ruti import doctor, ledger, quota, sessions, statusline

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture(autouse=True)
def no_live_facts(monkeypatch):
    """`render()`'s other job -- proxy/LM Studio/GPU -- stubbed flat so these tests
    measure only the six new segments, not network or subprocess timing."""
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None,
    })
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))


def five_hour_quota(used=10.0, seven_day=None, captured_at=None):
    return quota.Quota(
        five_hour=quota.Window(used, time.time() + 3600),
        seven_day=quota.Window(seven_day, time.time() + 86400) if seven_day is not None else None,
        captured_at=captured_at if captured_at is not None else time.time(),
    )


# ------------------------------------------------------------------------------ OFF


def test_off_appears_first_and_loud_when_the_session_is_disabled():
    sessions.set_disabled("s1", True)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "OFF" in plain(line)
    # First among the mode segments, not buried after coding/free/council.
    assert plain(line).index("OFF") < plain(line).index("jev")


def test_no_off_segment_for_an_ordinary_session():
    sessions.set_disabled("s1", False)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "OFF" not in plain(line)


# ------------------------------------------------------------------------------ jev


def test_jev_is_green_when_the_mode_is_on_and_a_key_exists(monkeypatch):
    from ruti import jev as jev_mod

    monkeypatch.setattr(jev_mod, "configured", lambda transport=jev_mod.DEFAULT_TRANSPORT: True)
    sessions.set_disabled("s1", False)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "\x1b[32mjev\x1b[0m" in line


def test_jev_is_dim_when_the_session_switch_is_off(monkeypatch):
    from ruti import jev as jev_mod, modes

    monkeypatch.setattr(jev_mod, "configured", lambda transport=jev_mod.DEFAULT_TRANSPORT: True)
    modes.set_jev("s1", False)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "\x1b[90mjev\x1b[0m" in line


def test_jev_is_dim_when_there_is_no_key(monkeypatch):
    from ruti import jev as jev_mod

    monkeypatch.setattr(jev_mod, "configured", lambda transport=jev_mod.DEFAULT_TRANSPORT: False)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "\x1b[90mjev\x1b[0m" in line


# ------------------------------------------------------------------- quota freshness


def test_a_live_reading_gets_no_freshness_suffix():
    line = statusline.render({}, five_hour_quota(captured_at=time.time()))
    assert "~" not in plain(line)


def test_a_stale_reading_is_marked_even_though_the_band_still_looks_fine():
    stale = five_hour_quota(used=10.0, captured_at=time.time() - 1000)
    assert stale.freshness == "stale"
    line = statusline.render({}, stale)
    assert "~stale" in plain(line)


# --------------------------------------------------------------- the 7-day segment


def test_seven_day_is_plain_below_60_percent():
    line = statusline.render({}, five_hour_quota(used=10.0, seven_day=40.0))
    assert "40% 7d" in line
    assert "\x1b[33m40% 7d" not in line and "\x1b[35m40% 7d" not in line


def test_seven_day_is_amber_between_60_and_the_escalation_tier():
    line = statusline.render({}, five_hour_quota(used=10.0, seven_day=65.0))
    assert "\x1b[33m65% 7d\x1b[0m" in line


def test_seven_day_takes_the_bands_own_colour_once_it_is_the_reason_band_is_tight():
    # The user's real numbers on 2026-09-24: 5h comfortable, 7d at 83% -- band becomes
    # YELLOW from the week alone, and the 7d segment should say so in YELLOW, not amber.
    snap = five_hour_quota(used=13.0, seven_day=83.0)
    assert snap.band == quota.YELLOW
    assert snap.seven_day_binding is True
    line = statusline.render({}, snap)
    assert "\x1b[33m83% 7d\x1b[0m" in line  # YELLOW's own colour code, "33"


def test_seven_day_does_not_claim_credit_when_five_hour_is_already_worse():
    # 99% five-hour is CRITICAL on its own; 92% weekly changes nothing here, and the
    # segment must not paint itself as the reason for a band it did not cause.
    snap = five_hour_quota(used=99.0, seven_day=92.0)
    assert snap.seven_day_binding is False
    line = statusline.render({}, snap)
    assert "\x1b[31m92% 7d\x1b[0m" not in line  # not painted CRITICAL's colour
    assert "92% 7d" in plain(line)


# --------------------------------------------------------------------- doctor badge


def test_no_doctor_badge_without_a_cached_report():
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "doctor:" not in plain(line)


def test_a_cached_clean_report_shows_no_badge():
    doctor.Report(checks=[doctor.Check("proxy", doctor.OK, "fine")]).cache()
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "doctor:" not in plain(line)


def test_a_cached_warn_report_shows_an_amber_count():
    doctor.Report(checks=[
        doctor.Check("lmstudio", doctor.WARN, "down"),
        doctor.Check("local-route", doctor.WARN, "stale"),
    ]).cache()
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "\x1b[33mdoctor:2\x1b[0m" in line


def test_a_cached_bad_report_shows_a_red_count():
    doctor.Report(checks=[doctor.Check("lmstudio", doctor.BAD, "down hard")]).cache()
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "\x1b[31mdoctor:1\x1b[0m" in line


# ----------------------------------------------------------------------- jev spend


def test_no_jev_cost_segment_at_zero():
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "jev:$" not in plain(line)


def test_jev_cost_sums_route_classifications_and_council_spend(monkeypatch):
    import os

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    ledger.record("route", kind="implement", classifier={"cost_usd": 0.000026})
    ledger.record("jev_spend", purpose="council_gate", cost_usd=0.000025)
    ledger.record("jev_spend", purpose="council_judge", cost_usd=0.000021)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "jev:$0.0001" in plain(line)  # 0.000072 rounds to 4 places as 0.0001


def test_jev_cost_does_not_leak_across_sessions(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-session")
    ledger.record("jev_spend", purpose="council_gate", cost_usd=0.5)
    line = statusline.render({"session_id": "s1"}, five_hour_quota())
    assert "jev:$" not in plain(line)


# ---------------------------------------------------------- never raises, never blocks


def test_a_missing_session_id_renders_without_the_session_scoped_segments():
    line = statusline.render({}, five_hour_quota())
    assert "OFF" not in line and "jev" not in plain(line).replace("jev:", "")


def test_render_never_raises_even_with_a_hostile_payload(monkeypatch):
    monkeypatch.setattr(ledger, "session_jev_cost", lambda _sid: (_ for _ in ()).throw(
        RuntimeError("boom")))
    # Must not propagate: every session-scoped block in render() is wrapped in its own
    # try/except for exactly this reason.
    line = statusline.render({"session_id": "s1", "model": None, "effort": None}, five_hour_quota())
    assert isinstance(line, str) and line

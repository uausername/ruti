"""The weekly window's escalation, and the flag that says whether it actually mattered.

The old check was a single cliff at 90%: nothing below it changed `band` at all, so a
week sitting at 82% -- eight points from a hard jump straight past YELLOW to ORANGE --
looked exactly like a week at 10%. `seven_day_binding` exists so the status line and
`summary()` can say *why* band is what it is, and it must not claim credit when the
five-hour window was already the worse of the two on its own.
"""

from __future__ import annotations

import time

import pytest

from ruti import quota


def snap(five_hour, seven_day=None):
    return quota.Quota(
        five_hour=quota.Window(five_hour, time.time() + 3600),
        seven_day=quota.Window(seven_day, time.time() + 86400) if seven_day is not None else None,
        captured_at=time.time(),
    )


@pytest.mark.parametrize("seven_day, expected", [
    (0.0, quota.GREEN),
    (60.0, quota.GREEN),
    (74.9, quota.GREEN),
    (75.0, quota.YELLOW),   # the new tier -- this is what the old code missed
    (82.0, quota.YELLOW),   # the real number this was built to catch
    (89.9, quota.YELLOW),
    (90.0, quota.ORANGE),   # exactly the old single-cliff threshold, preserved
    (99.0, quota.ORANGE),
])
def test_seven_day_escalates_a_comfortable_five_hour_window(seven_day, expected):
    assert snap(five_hour=7.0, seven_day=seven_day).band == expected


def test_crossing_both_tiers_lands_where_the_old_single_cliff_did():
    # GREEN -> YELLOW at 75, then YELLOW -> ORANGE at 90: two hops, same destination
    # the pre-tiered code reached in one jump at >= 90.
    assert snap(five_hour=7.0, seven_day=92.0).band == quota.ORANGE


@pytest.mark.parametrize("five_hour_band, five_hour_used, seven_day, expected", [
    # The 75% tier only ever touches GREEN -- a session already at YELLOW from the
    # five-hour window on its own gets no earlier nudge, it needs one; it is not until
    # 90% that the weekly window pushes YELLOW to ORANGE.
    (quota.YELLOW, 50.0, 80.0, quota.YELLOW),
    (quota.YELLOW, 50.0, 92.0, quota.ORANGE),
    (quota.ORANGE, 80.0, 92.0, quota.RED),
])
def test_seven_day_tightens_whatever_band_the_five_hour_window_already_gave(
    five_hour_band, five_hour_used, seven_day, expected
):
    q = snap(five_hour=five_hour_used, seven_day=seven_day)
    assert q._five_hour_band == five_hour_band
    assert q.band == expected


def test_seven_day_never_loosens_a_band_the_five_hour_window_already_forced():
    # 99% five-hour is CRITICAL on its own. A merely-high weekly number must not read
    # as the reason for a band that was already this bad.
    q = snap(five_hour=99.0, seven_day=92.0)
    assert q.band == quota.CRITICAL
    assert q.seven_day_binding is False


def test_binding_is_true_only_when_seven_day_actually_changed_the_outcome():
    assert snap(five_hour=7.0, seven_day=82.0).seven_day_binding is True
    assert snap(five_hour=7.0, seven_day=60.0).seven_day_binding is False


def test_binding_is_false_with_no_seven_day_reading_at_all():
    assert snap(five_hour=7.0, seven_day=None).seven_day_binding is False


def test_summary_names_the_seven_day_window_only_when_it_is_binding():
    binding = snap(five_hour=7.0, seven_day=82.0).summary()
    assert "7d at 82%" in binding

    not_binding = snap(five_hour=7.0, seven_day=40.0).summary()
    assert "7d" not in not_binding

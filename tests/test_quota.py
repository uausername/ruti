"""The weekly window's escalation, and the flag that says whether it actually mattered.

The old check was a single cliff at 90%: nothing below it changed `band` at all, so a
week sitting at 82% -- eight points from a hard jump straight past YELLOW to ORANGE --
looked exactly like a week at 10%. `seven_day_binding` exists so the status line and
`summary()` can say *why* band is what it is, and it must not claim credit when the
five-hour window was already the worse of the two on its own.

The weekly window also gets a reset and a pace, because `7d 23%` alone does not say
whether there is time left: the window is not rolling, so the average pace since it
opened projects to the reset without needing a burn-rate history.
"""

from __future__ import annotations

import re
import time

import pytest

from ruti import quota


def snap(five_hour, seven_day=None, seven_day_in=86400.0):
    return quota.Quota(
        five_hour=quota.Window(five_hour, time.time() + 3600),
        seven_day=(
            quota.Window(seven_day, time.time() + seven_day_in)
            if seven_day is not None else None
        ),
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


def test_summary_names_the_seven_day_window_whether_or_not_it_is_binding():
    # The weekly reading is always worth a clause now: how much is left, and when it
    # resets, are the two numbers a 23% week needs. Binding only adds the *blame*.
    quiet = snap(five_hour=7.0, seven_day=40.0).summary()
    assert "7d 40% used" in quiet
    assert "-- the tighter window" not in quiet

    binding = snap(five_hour=7.0, seven_day=82.0).summary()
    assert "7d 82% used" in binding
    assert "-- the tighter window" in binding


# ------------------------------------------------- a window whose reset has gone by


def passed_snap(used, captured_ago, seven_day=None, seven_day_in=86400.0, five_in=-30.0):
    """A reading captured `captured_ago` seconds ago whose five-hour reset has passed."""
    now = time.time()
    return quota.Quota(
        five_hour=quota.Window(used, now + five_in),
        seven_day=(
            quota.Window(seven_day, now + seven_day_in)
            if seven_day is not None else None
        ),
        captured_at=now - captured_ago,
    )


def test_a_stale_reading_whose_reset_has_passed_is_a_reset_window():
    # The live bug: after the reset the status line still read UNKNOWN 93% and the
    # prompt hook said assume ORANGE, because the reading was old and idle sessions
    # send no API requests. An idle session's reading freezes, but a frozen reading
    # whose own reset has passed is a window that has reset -- an empty one.
    q = passed_snap(93.0, captured_ago=4 * 3600)
    assert q.freshness == "unknown"
    assert q.five_hour_reset_passed is True
    assert q.band == quota.GREEN
    assert q.seven_day_binding is False


def test_the_summary_of_a_passed_window_says_it_reset_and_drops_the_old_number():
    line = passed_snap(93.0, captured_ago=4 * 3600).summary()
    assert line.startswith("[GREEN] 5h window reset at ")
    assert "no reading since" in line
    assert "reading is" not in line
    assert "93%" not in line


def test_a_passed_weekly_window_does_not_escalate_and_says_it_reset():
    # 83% would be YELLOW on its own; from a week that has already reset it says
    # nothing about the week ahead, and the reset is the whole message.
    q = passed_snap(7.0, captured_ago=4 * 3600, seven_day=83.0, seven_day_in=-60.0)
    assert q.seven_day_reset_passed is True
    assert q.band == quota.GREEN
    assert q.seven_day_binding is False
    line = q.summary()
    assert "7d window reset" in line
    assert "83%" not in line
    assert "-- the tighter window" not in line


def test_a_stale_reading_whose_reset_is_still_ahead_stays_unknown():
    # The regression guard: only a passed reset lifts staleness. A frozen 93% that has
    # not reset yet really can be a real 93%, and unknown is the safe reading of it.
    now = time.time()
    q = quota.Quota(
        five_hour=quota.Window(93.0, now + 600),
        seven_day=quota.Window(83.0, now + 86400),
        captured_at=now - 4 * 3600,
    )
    assert q.five_hour_reset_passed is False
    assert q.band == quota.UNKNOWN


def test_a_weekly_window_alone_cannot_bind_when_only_the_five_hour_reset_passed():
    # The weekly window is still live here, so it still counts -- but a passed
    # five-hour reset leaves the base band GREEN either way, and 40% does not move it.
    q = passed_snap(93.0, captured_ago=4 * 3600, seven_day=40.0, seven_day_in=86400.0)
    assert q.band == quota.GREEN
    assert q.seven_day_binding is False


def test_a_passed_reset_with_no_timestamp_to_name_says_so_without_one():
    # A quota.json written before `resets_at` was known to be epoch seconds holds an
    # ISO string; the local time is not available then, and the clause is dropped.
    now = time.time()
    q = quota.Quota(
        five_hour=quota.Window(93.0, "2020-01-01T00:00:00+00:00"),
        seven_day=None,
        captured_at=now - 4 * 3600,
    )
    assert q.five_hour_reset_passed is True
    assert q.summary() == "[GREEN] 5h window reset -- no reading since"


# ----------------------------------------------------------------- the weekly pace


@pytest.mark.parametrize("seconds, expected", [
    (4.1 * 86400, "4.1d"),
    (86400.0, "1.0d"),
    (9.5 * 3600, "9.5h"),
])
def test_format_left_switches_to_days_above_a_day(seconds, expected):
    assert quota.format_left(seconds) == expected


def test_weekly_pace_projects_the_average_so_far_to_the_reset():
    # 23% with 4.1 days left means 2.9 of the 7 days are gone, so 23 * 7 / 2.9.
    q = snap(five_hour=7.0, seven_day=23.0, seven_day_in=4.1 * 86400)
    assert q.seven_day_projected_at_reset == pytest.approx(55.5, rel=1e-3)


def test_weekly_pace_is_withheld_when_too_little_of_the_week_has_gone():
    # A handful of points against the first hours of the window is noise, not a trend.
    q = snap(five_hour=7.0, seven_day=23.0,
             seven_day_in=quota.SEVEN_DAY_SECONDS - 11 * 3600)
    assert q.seven_day_projected_at_reset is None


def test_weekly_pace_is_withheld_without_a_weekly_reading():
    assert snap(five_hour=7.0, seven_day=None).seven_day_projected_at_reset is None


def test_weekly_pace_is_withheld_when_the_reset_is_unknown():
    q = quota.Quota(
        five_hour=quota.Window(7.0, time.time() + 3600),
        seven_day=quota.Window(23.0, None),
        captured_at=time.time(),
    )
    assert q.seven_day_projected_at_reset is None


def test_summary_states_the_weekly_reset_and_the_pace_it_implies():
    line = snap(five_hour=7.0, seven_day=23.0, seven_day_in=4.1 * 86400).summary()
    assert "7d 23% used" in line
    assert "resets " in line
    assert "(in 4.1d)" in line
    assert "on pace for 56%" in line
    # "Fri 14:00"-shaped, in the machine's own local time.
    assert re.search(r"resets \w{3} \d{2}:\d{2} \(in 4\.1d\)", line)


def test_summary_drops_the_weekly_reset_when_the_timestamp_is_an_iso_string():
    # quota.json files written before `resets_at` was known to be epoch seconds hold an
    # ISO string. The reset clause goes, the rest of the line must still be there.
    q = quota.Quota(
        five_hour=quota.Window(7.0, time.time() + 3600),
        seven_day=quota.Window(40.0, "2099-10-02T14:00:00+00:00"),
        captured_at=time.time(),
    )
    line = q.summary()
    assert "7d 40% used" in line
    assert "(in " not in line
    assert line.startswith("[")

"""`quota.capture`: a repaint with nothing measured must not erase what was measured."""

from __future__ import annotations

import time

import pytest

from ruti import quota


@pytest.fixture(autouse=True)
def isolated_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "QUOTA_FILE", tmp_path / "quota.json")


def reading(used, sid="s1"):
    return {"session_id": sid, "rate_limits": {
        "five_hour": {"used_percentage": used, "resets_at": time.time() + 3600},
        "seven_day": {"used_percentage": 5.0, "resets_at": time.time() + 86400}}}


def test_a_reading_is_kept():
    assert quota.capture(reading(12.0)).five_hour.used_percentage == 12.0


@pytest.mark.parametrize("empty", [{}, {"rate_limits": None}, {"rate_limits": {}},
                                   {"session_id": "fresh", "rate_limits": {"five_hour": None}}])
def test_a_fresh_session_without_limits_does_not_wipe_the_reading(empty):
    quota.capture(reading(12.0))
    snapshot = quota.capture(empty)
    assert snapshot.five_hour is not None and snapshot.five_hour.used_percentage == 12.0
    assert quota.load().five_hour.used_percentage == 12.0


def test_a_later_real_reading_still_replaces_it():
    quota.capture(reading(12.0))
    quota.capture({})
    assert quota.capture(reading(20.0, sid="s2")).five_hour.used_percentage == 20.0


def weekly_only(sid="s2"):
    return {"session_id": sid, "rate_limits": {
        "seven_day": {"used_percentage": 11.0, "resets_at": time.time() + 86400}}}


def test_a_session_reporting_only_the_weekly_window_keeps_the_five_hour_one():
    quota.capture(reading(16.0))
    snapshot = quota.capture(weekly_only())
    assert snapshot.five_hour is not None and snapshot.five_hour.used_percentage == 16.0
    assert snapshot.seven_day.used_percentage == 11.0
    assert snapshot.band != quota.UNKNOWN


def test_a_kept_five_hour_reading_ages_instead_of_passing_for_live(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(quota.time, "time", lambda: clock[0])
    quota.capture(reading(16.0))
    first = quota.load().captured_at
    clock[0] += 600
    quota.capture(weekly_only())
    assert quota.load().captured_at == first

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


def test_a_stale_session_cannot_saw_tooth_the_history():
    # Two sessions repainting: one current (37), one still showing what it saw last
    # (32). The lower reading of the same window is dropped, so the history stays
    # monotone and the burn rate survives.
    reset = time.time() + 3600

    def at(used, sid):
        return {"session_id": sid, "rate_limits": {
            "five_hour": {"used_percentage": used, "resets_at": reset}}}

    for _ in range(5):
        quota.capture(at(37.0, "fresh"))
        snap = quota.capture(at(32.0, "stale"))
    assert snap.five_hour.used_percentage == 37.0
    assert [v for _, v in snap.history] == [37.0]
    snap = quota.capture(at(38.0, "fresh"))
    assert [v for _, v in snap.history] == [37.0, 38.0]


def test_a_new_window_starts_lower_and_is_kept():
    quota.capture(reading(80.0))
    nxt = {"session_id": "s1", "rate_limits": {
        "five_hour": {"used_percentage": 2.0, "resets_at": time.time() + 5 * 3600}}}
    assert quota.capture(nxt).five_hour.used_percentage == 2.0


def test_a_reading_of_an_older_window_is_dropped():
    quota.capture(reading(10.0))
    old = {"session_id": "s2", "rate_limits": {
        "five_hour": {"used_percentage": 90.0, "resets_at": time.time() - 600}}}
    assert quota.capture(old).five_hour.used_percentage == 10.0

"""`build_report()` must cache the doctor report unconditionally -- healthy or not.

Skipping the cache write on a healthy run was the natural-looking shortcut and the
wrong one: it would leave a BAD badge from three sessions ago on the status line
forever, because nothing healthy would ever overwrite it.
"""

from __future__ import annotations

from ruti import doctor
from ruti.hooks import session_start


def test_a_healthy_run_still_caches_so_a_stale_bad_badge_can_clear(monkeypatch):
    doctor.Report(checks=[doctor.Check("lmstudio", doctor.BAD, "down")]).cache()
    assert doctor.cached()["problems"]

    monkeypatch.setattr(
        doctor, "run_checks",
        lambda: doctor.Report(checks=[doctor.Check("lmstudio", doctor.OK, "up")]),
    )
    result = session_start.build_report()

    assert result is None  # nothing to tell the user -- everything is fine
    assert doctor.cached()["problems"] == []  # but the cache was still refreshed


def test_an_unhealthy_run_caches_and_reports(monkeypatch):
    monkeypatch.setattr(
        doctor, "run_checks",
        lambda: doctor.Report(checks=[doctor.Check("lmstudio", doctor.WARN, "down")]),
    )
    result = session_start.build_report()

    assert result is not None
    assert doctor.cached()["problems"] == [{"name": "lmstudio", "status": doctor.WARN}]


def test_a_cache_failure_does_not_break_the_report(monkeypatch):
    """`report.cache()` may raise (see test_doctor_cache.py); the hook must swallow it
    and still return the report the user actually asked to see."""
    monkeypatch.setattr(
        doctor, "run_checks",
        lambda: doctor.Report(checks=[doctor.Check("lmstudio", doctor.WARN, "down")]),
    )
    monkeypatch.setattr(
        doctor.Report, "cache",
        lambda self: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = session_start.build_report()
    assert result is not None
    user_message, model_context = result
    assert "lmstudio" in user_message

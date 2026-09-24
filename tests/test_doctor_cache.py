"""`Report.cache()`/`cached()`: the read the status line is actually allowed to make.

`run_checks()` walks `schtasks` and a TLS chain -- seconds, not the milliseconds a
repaint budget allows -- so the status line must never call it directly. This is the
passive read it uses instead: whatever the session-start hook cached last, with no
TTL to expire and no network to hang on.
"""

from __future__ import annotations

import pytest

from ruti import doctor


def test_no_cache_reads_as_none():
    assert doctor.cached() is None


def test_a_clean_report_caches_no_problems():
    doctor.Report(checks=[doctor.Check("proxy", doctor.OK, "fine")]).cache()
    cached = doctor.cached()
    assert cached["worst"] == doctor.OK
    assert cached["problems"] == []


def test_a_problem_report_caches_name_and_status():
    doctor.Report(checks=[
        doctor.Check("proxy", doctor.OK, "fine"),
        doctor.Check("lmstudio", doctor.WARN, "down"),
        doctor.Check("secrets", doctor.BAD, "world-readable"),
    ]).cache()
    cached = doctor.cached()
    assert cached["worst"] == doctor.BAD
    assert {p["name"] for p in cached["problems"]} == {"lmstudio", "secrets"}


def test_a_later_clean_report_clears_a_stale_problem():
    """The status line's `doctor:` badge must not survive a problem that was fixed.
    Cache() is called unconditionally by the hook regardless of outcome, precisely so
    this can happen."""
    doctor.Report(checks=[doctor.Check("lmstudio", doctor.BAD, "down")]).cache()
    assert doctor.cached()["problems"]

    doctor.Report(checks=[doctor.Check("lmstudio", doctor.OK, "up now")]).cache()
    assert doctor.cached()["problems"] == []


def test_cache_itself_is_allowed_to_raise_on_a_write_failure(monkeypatch):
    """`Report.cache()` does not swallow errors -- `session_start.py`'s own call site
    does, with a try/except around it. Pinned here so that boundary does not quietly
    move: a doctor run must not be lost to a bookkeeping failure, but that is the
    caller's job to guarantee, not this function's."""
    import ruti.config as config

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(config, "write_json", fail)
    with pytest.raises(OSError):
        doctor.Report(checks=[doctor.Check("x", doctor.OK, "y")]).cache()

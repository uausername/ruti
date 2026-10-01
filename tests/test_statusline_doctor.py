"""Colour means one thing -- the band, and the window that caused it -- and the doctor
badge says what is wrong and how old the finding is.

Two changes meet in this file. First, the band word and each window are separate
segments: as one segment, `YELLOW 3% 5h` painted a 3% five-hour window in a colour the
*week* had chosen, and YELLOW and ORANGE shared a colour code. Second, the badge stops
counting problems and names the single one that matters, with the age of the report it
came from -- the background refresh in `doctor.refresh_in_background` is what makes that
age meaningful rather than decorative.

`render()` is exercised with `_refresh_facts` stubbed so no test touches the network,
LM Studio, or nvidia-smi; `subprocess.Popen` is passed in as `spawn` so no test spawns a
real `python -m ruti.doctor`.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

import pytest
from click.testing import CliRunner

from ruti import cli, config, doctor, modes, quota, statusline
from ruti.hooks import session_start

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def first_line_segments(line: str) -> list[str]:
    """The raw segments of line 1, escape codes still in -- so "is this coloured" is a
    question about the segment itself rather than about a substring of it."""
    return line.split("\n")[0].split(" · ")


@pytest.fixture(autouse=True)
def no_live_facts(monkeypatch):
    """`render()`'s other job -- proxy/LM Studio/GPU -- stubbed flat, so these tests
    measure the budget line and the badge and nothing else."""
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None,
    })
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))


def quota_at(five_hour=10.0, seven_day=None, seven_day_in=86400.0):
    return quota.Quota(
        five_hour=quota.Window(five_hour, time.time() + 3600),
        seven_day=(
            quota.Window(seven_day, time.time() + seven_day_in)
            if seven_day is not None else None
        ),
        captured_at=time.time(),
    )


# ------------------------------------------------------------------- band and windows


def test_a_week_that_sets_the_band_colours_only_itself_and_the_band_word():
    # The user's real numbers on 2026-09-24: the five hours barely used, the week 83%
    # spent. YELLOW is the week's doing, so the band says `YELLOW(7d)` and the 3% is
    # left plain -- painting it yellow claimed a five-hour window that is not tight.
    snap = quota_at(five_hour=3.0, seven_day=83.0, seven_day_in=86400.0 - 1)
    assert snap.band == quota.YELLOW and snap.seven_day_binding is True

    segments = first_line_segments(statusline.render({"session_id": "s1"}, snap))

    assert "\x1b[33mYELLOW(7d)\x1b[0m" in segments
    five = next(s for s in segments if "3% 5h" in s)
    assert five == "3% 5h/1.0h"  # no escape codes at all: this number is not the band
    # The week wears YELLOW's colour. It also carries a projection arrow of its own --
    # a day left in a week that is 83% spent projects past 90 -- which is the point of
    # colouring the arrow by its tier rather than by the band.
    week = next(s for s in segments if "83% 7d" in s)
    assert week.startswith("\x1b[33m83% 7d/24.0h\x1b[0m")


def test_the_five_hour_window_wears_the_colour_when_it_is_what_set_the_band():
    # The mirror image: 60% of five hours is YELLOW on its own, so both the word and the
    # number it belongs to are yellow.
    snap = quota_at(five_hour=60.0)
    assert snap.band == quota.YELLOW and snap.seven_day_binding is False

    line = statusline.render({"session_id": "s1"}, snap)

    assert "\x1b[33mYELLOW\x1b[0m" in line
    assert "\x1b[33m60% 5h/1.0h\x1b[0m" in line
    assert "(7d)" not in plain(line)


def test_orange_is_its_own_colour_and_not_yellows():
    # YELLOW and ORANGE shared "33", so the one distinction the band exists to make was
    # invisible to anyone who can see colour. 38;5;208 is a real orange; 80% of five
    # hours is ORANGE with nothing weekly involved.
    snap = quota_at(five_hour=80.0)
    assert snap.band == quota.ORANGE

    line = statusline.render({"session_id": "s1"}, snap)

    assert "\x1b[38;5;208mORANGE\x1b[0m" in line
    assert "\x1b[38;5;208m80% 5h/1.0h\x1b[0m" in line


# ------------------------------------------------------------------- projection arrow


def test_the_week_projects_past_90_in_orange(monkeypatch):
    # 83% now, on track for 91% at the reset: 91 is the tier at which the band escalates
    # past YELLOW, so the arrow is orange and not the arrow's own idea of "high".
    monkeypatch.setattr(quota.Quota, "seven_day_projected_at_reset",
                        property(lambda self: 91.0))
    snap = quota_at(five_hour=3.0, seven_day=83.0, seven_day_in=86400.0 - 1)

    line = statusline.render({"session_id": "s1"}, snap)

    assert "↗91%" in plain(line)
    assert "\x1b[38;5;208m↗91%\x1b[0m" in line


def test_a_projection_inside_the_current_tier_carries_no_arrow(monkeypatch):
    # 83% now, on track for 86: still YELLOW by the time it resets, so there is nothing
    # to warn about -- 75 and 90 are the tiers, not every point above them.
    monkeypatch.setattr(quota.Quota, "seven_day_projected_at_reset",
                        property(lambda self: 86.0))
    snap = quota_at(five_hour=3.0, seven_day=83.0, seven_day_in=86400.0 - 1)

    line = statusline.render({"session_id": "s1"}, snap)

    assert "↗" not in plain(line)


def test_five_hours_projected_past_the_window_says_so_in_red(monkeypatch):
    # There is no ladder here: the window resets on its own, so the only overrun that
    # means anything is one that lands past 100 -- it runs out before its reset.
    monkeypatch.setattr(quota.Quota, "projected_at_reset",
                        property(lambda self: 120.0))
    line = statusline.render({"session_id": "s1"}, quota_at(five_hour=60.0))

    assert "↗120%" in plain(line)
    assert "\x1b[31m↗120%\x1b[0m" in line


def test_a_five_hour_projection_that_fits_the_window_carries_no_arrow(monkeypatch):
    monkeypatch.setattr(quota.Quota, "projected_at_reset",
                        property(lambda self: 80.0))
    line = statusline.render({"session_id": "s1"}, quota_at(five_hour=60.0))

    assert "↗" not in plain(line)


# ------------------------------------------------------------- what line 1 is for


def test_context_fill_rides_with_the_budget_and_before_the_modes():
    # It is about this turn, not about the session's settings, and it belongs on the
    # line that is never cut off -- so it precedes the modes, not the machine state.
    modes.set_coding("s1", True)
    line = statusline.render(
        {"session_id": "s1", "context_window": {"used_percentage": 20}}, quota_at())

    budget = plain(line).split("\n")[0]
    assert "code" in budget and "20% ctx" in budget
    assert budget.index("20% ctx") < budget.index("code")


# --------------------------------------------------------------------- doctor badge


def write_report(tmp_path, problems, *, at=None, worst="warn"):
    config.write_json(tmp_path / "doctor-last.json", {
        "at": time.time() - 7200 if at is None else at,
        "worst": worst,
        "problems": problems,
    })


def test_one_problem_is_named_and_says_how_old_it_is(tmp_path):
    write_report(tmp_path, [{"name": "tls", "status": doctor.WARN}])
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "doctor:tls 2h" in plain(line)
    assert "\x1b[33mdoctor:tls 2h\x1b[0m" in line


def test_several_problems_are_a_count(tmp_path):
    write_report(tmp_path, [{"name": "lmstudio", "status": doctor.WARN},
                            {"name": "local-route", "status": doctor.WARN}])
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "doctor:2 2h" in plain(line)


def test_a_bad_finding_is_red_whatever_else_is_also_wrong(tmp_path):
    write_report(tmp_path, [{"name": "secrets", "status": doctor.BAD},
                            {"name": "lmstudio", "status": doctor.WARN}], worst="bad")
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "\x1b[31mdoctor:2 2h\x1b[0m" in line


def test_a_stale_status_line_reading_is_never_a_badge(tmp_path):
    # A status line that is painting right now is its own proof, and the badge's whole
    # complaint was that a stale reading between sessions said `doctor:1` all session.
    write_report(tmp_path, [{"name": "statusline", "status": doctor.WARN}])
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "doctor:" not in plain(line)


def test_a_proxy_alive_on_line_two_is_not_a_badge_either(tmp_path):
    write_report(tmp_path, [{"name": "proxy", "status": doctor.WARN}])
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "proxy ok" in plain(line)
    assert "doctor:" not in plain(line)


def test_a_down_proxy_is_still_worth_a_badge(monkeypatch, tmp_path):
    # The drop above is about the line already showing it, so it stops dropping the
    # moment line 2 says the proxy is down.
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": False, "loaded": [], "gpu": None,
    })
    write_report(tmp_path, [{"name": "proxy", "status": doctor.BAD}], worst="bad")
    line = statusline.render({"session_id": "s1"}, quota_at())

    assert "proxy DOWN" in plain(line)
    assert "\x1b[31mdoctor:proxy 2h\x1b[0m" in line


def test_an_expected_finding_stays_out_of_the_cache_at_session_start():
    # At session start the status line's last reading is stale by definition; the badge
    # reads it for the rest of the session, and `worst` has to be graded on what is left
    # or a run with one real problem reads as clean.
    report = doctor.Report(checks=[
        doctor.Check("statusline", doctor.WARN, "last reading is stale (unknown)",
                     expected_at_start=True),
        doctor.Check("tls", doctor.BAD, "intercepted"),
    ])
    report.cache(skip_expected=True)
    cached = doctor.cached()

    assert cached["problems"] == [{"name": "tls", "status": doctor.BAD}]
    assert cached["worst"] == doctor.BAD

    # The same run without the flag keeps it: mid-session that reading is a real fault.
    report.cache()
    assert doctor.cached()["problems"][0]["name"] == "statusline"


def test_session_start_does_not_report_or_cache_the_expected_stale_reading(monkeypatch):
    monkeypatch.setattr(doctor, "run_checks", lambda: doctor.Report(checks=[
        doctor.Check("statusline", doctor.WARN, "last reading is stale (unknown)",
                     detail="normal between sessions", expected_at_start=True),
    ]))

    assert session_start.build_report() is None
    assert doctor.cached()["problems"] == []


def test_a_manual_doctor_run_is_what_keeps_the_badge_current(monkeypatch):
    # Only the session-start hook used to write the cache, so `ruti doctor --fix` left a
    # problem on the status line until the next session -- which is the session in which
    # the user would have looked for it to be gone.
    monkeypatch.setattr(doctor, "run_checks", lambda: doctor.Report(
        checks=[doctor.Check("proxy", doctor.OK, "alive")]))

    outcome = CliRunner().invoke(cli.main, ["doctor", "--json"])

    assert outcome.exit_code == 0
    assert doctor.cached()["problems"] == []


# --------------------------------------------------------------- background refresh


def collecting_spawn():
    calls: list[tuple[list[str], dict]] = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))

    return calls, spawn


def test_no_report_yet_means_a_run_is_started(tmp_path):
    calls, spawn = collecting_spawn()

    assert doctor.refresh_in_background(spawn=spawn) is True

    argv, kwargs = calls[0]
    assert argv[-2:] == ["-m", "ruti.doctor"]
    # No window, no shell, no inherited handles: this runs while the user is typing.
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["close_fds"] is True
    assert (tmp_path / "doctor-refresh.lock").exists()


def test_a_locked_run_is_not_started_twice(tmp_path):
    calls, spawn = collecting_spawn()
    assert doctor.refresh_in_background(spawn=spawn) is True

    assert doctor.refresh_in_background(spawn=spawn) is False
    assert len(calls) == 1


def test_a_report_written_within_the_hour_is_left_alone():
    doctor.Report(checks=[doctor.Check("proxy", doctor.OK, "alive")]).cache()
    calls, spawn = collecting_spawn()

    assert doctor.refresh_in_background(spawn=spawn) is False
    assert calls == []


def test_an_hour_old_report_is_re_checked(tmp_path):
    write_report(tmp_path, [], at=time.time() - 7200)
    calls, spawn = collecting_spawn()

    assert doctor.refresh_in_background(spawn=spawn) is True
    assert len(calls) == 1


def test_a_lock_older_than_its_ttl_is_a_crashed_run_not_a_running_one(tmp_path):
    # Otherwise one killed run would stop every later refresh for good, which is the
    # silent failure the doctor itself exists to catch.
    lock = tmp_path / "doctor-refresh.lock"
    lock.write_text("0", encoding="utf-8")
    old = time.time() - (doctor.REFRESH_LOCK_TTL + 1)
    os.utime(lock, (old, old))
    calls, spawn = collecting_spawn()

    assert doctor.refresh_in_background(spawn=spawn) is True
    assert len(calls) == 1


def test_a_failed_spawn_is_swallowed(tmp_path):
    # Called from the prompt hook: a health check that fails must not touch the prompt.
    def boom(argv, **kwargs):
        raise OSError("cannot spawn")

    assert doctor.refresh_in_background(spawn=boom) is False


def test_the_background_run_refreshes_the_cache_and_unlocks_itself(monkeypatch, tmp_path):
    lock = tmp_path / "doctor-refresh.lock"
    lock.write_text("1", encoding="utf-8")
    monkeypatch.setattr(doctor, "run_checks", lambda: doctor.Report(
        checks=[doctor.Check("lmstudio", doctor.BAD, "down")]))

    assert doctor._refresh_main() == 0

    assert not lock.exists()
    assert doctor.cached()["problems"] == [{"name": "lmstudio", "status": doctor.BAD}]

def test_the_arrow_takes_the_highest_tier_it_crosses():
    # 70% heading for 95% crosses both 75 (YELLOW) and 90 (ORANGE): the orange one is
    # what the week will actually do, so that is the colour.
    arrow = statusline._arrow(70.0, 95.0, statusline.SEVEN_DAY_ARROW_TIERS)
    assert "38;5;208" in arrow and "↗95%" in arrow

"""Wait mode: notice at 90%, refuse tools at 95%, sleep through the reset, resume."""

from __future__ import annotations

import time

import pytest

from ruti import install, modes, quota, sessions, wait
from ruti.hooks import user_prompt_submit, wait_gate

SID = "session-wait"


def snap(used: float, resets_in: float | None = 3600.0) -> quota.Quota:
    resets_at = None if resets_in is None else time.time() + resets_in
    return quota.Quota(quota.Window(used, resets_at), None, captured_at=time.time())


def pre(snapshot, tool="Edit", tool_input=None):
    payload = {"tool_name": tool, "tool_input": tool_input or {}}
    return wait.pre_tool_use(SID, payload, snapshot)


def test_nothing_happens_while_wait_mode_is_off(monkeypatch):
    monkeypatch.setattr(quota, "load", lambda: snap(99))
    assert wait_gate.handle({"session_id": SID, "hook_event_name": "PreToolUse",
                             "tool_name": "Edit"}) is None


def test_below_the_lines_nothing_is_said():
    modes.set_wait(SID, True)
    assert pre(snap(89)) is None
    assert wait.post_tool_use(SID, snap(89)) is None


def test_the_notice_comes_once_per_window():
    modes.set_wait(SID, True)
    s = snap(91)
    first = wait.post_tool_use(SID, s)
    assert "Assess the open tasks" in first["hookSpecificOutput"]["additionalContext"]
    assert wait.post_tool_use(SID, s) is None
    # a new window notices again
    assert wait.post_tool_use(SID, snap(92, resets_in=7200)) is not None


def test_the_notice_says_what_to_do_if_the_turn_ends_early():
    # The live bug: the manager took the notice, stopped at 93% of its own accord, and
    # the Stop hook had no pause to act on, so the session sat there waiting for the
    # user. The notice has to name the arming, or the same thing happens again.
    modes.set_wait(SID, True)
    context = wait.post_tool_use(SID, snap(91))["hookSpecificOutput"]["additionalContext"]
    assert "ruti mode wait pause" in context


# ------------------------------------------------------- pausing before the pause line


def test_pause_arms_the_resume_so_the_stop_hook_waits_and_continues():
    modes.set_wait(SID, True)
    s = snap(93, resets_in=600)
    assert wait.pause(SID, s) == wait.reset_clock(s)
    now = [time.time()]
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    out = wait.stop(SID, s, sleep=fake_sleep, clock=lambda: now[0])
    assert out["decision"] == "block"
    assert "has reset" in out["reason"]
    assert 600 <= sum(slept) <= 600 + wait.RESUME_MARGIN_SECONDS + 1
    assert not modes.wait_state(SID).get("paused")


def test_pause_marks_the_notice_as_seen_too():
    modes.set_wait(SID, True)
    s = snap(93)
    modes.set_wait_state(SID, {})
    wait.pause(SID, s)
    assert wait.post_tool_use(SID, s) is None


def test_an_early_pause_still_allows_tools():
    # The whole difference from the 95% refusal: this arms a resume, it does not stop
    # the work. Refusing tools here would make `pause` indistinguishable from a pause.
    modes.set_wait(SID, True)
    wait.pause(SID, snap(93))
    assert pre(snap(93)) is None
    assert pre(snap(94.9)) is None


@pytest.mark.parametrize("state, snapshot, why", [
    ("off", lambda: snap(93), "wait mode is off"),
    ("on", lambda: snap(93, resets_in=None), "the reset time is unknown"),
    ("on", lambda: snap(93, resets_in=-10), "the reset has already passed"),
])
def test_pause_refuses_and_changes_nothing_when_there_is_nothing_to_wait_for(state, snapshot, why):
    modes.set_wait(SID, state == "on")
    s = snapshot()
    modes.set_wait_state(SID, {})
    assert wait.pause(SID, s) is None, why
    assert modes.wait_state(SID).get("paused") is None, why
    # ...and so the Stop hook still releases the session rather than holding it.
    assert wait.stop(SID, s, sleep=lambda _s: 1 / 0) is None


def test_pause_does_nothing_without_a_five_hour_reading_at_all():
    modes.set_wait(SID, True)
    modes.set_wait_state(SID, {})
    assert wait.pause(SID, quota.Quota(None, None, captured_at=time.time())) is None
    assert modes.wait_state(SID).get("paused") is None


def test_the_prompt_note_says_how_to_stop_early():
    modes.set_wait(SID, True)
    assert "ruti mode wait pause" in wait.prompt_note(SID, snap(20))


def test_mode_wait_pause_reports_the_clock_and_arms_the_resume(monkeypatch):
    from click.testing import CliRunner
    from ruti import cli

    s = snap(93, resets_in=1800)
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    monkeypatch.setattr(quota, "load", lambda: s)
    modes.set_wait(SID, True)
    modes.set_wait_state(SID, {})

    result = CliRunner().invoke(cli.main, ["mode", "wait", "pause"])
    assert result.exit_code == 0, result.output
    assert f"paused until the reset at {wait.reset_clock(s)}" in result.output
    assert modes.wait_state(SID)["paused"] == wait.window_key(s)


def test_mode_wait_pause_fails_loudly_when_wait_mode_is_off(monkeypatch):
    from click.testing import CliRunner
    from ruti import cli

    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    monkeypatch.setattr(quota, "load", lambda: snap(93))
    modes.set_wait(SID, False)
    modes.set_wait_state(SID, {})

    result = CliRunner().invoke(cli.main, ["mode", "wait", "pause"])
    assert result.exit_code == 1
    assert "ruti mode wait on` first" in result.output
    assert modes.wait_state(SID).get("paused") is None


def test_at_the_pause_line_tools_are_refused_and_the_session_is_paused():
    modes.set_wait(SID, True)
    s = snap(95.5)
    out = pre(s)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "checkpoint" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert modes.wait_state(SID)["paused"] == wait.window_key(s)


def test_ruti_and_the_task_list_stay_usable_while_paused():
    modes.set_wait(SID, True)
    s = snap(97)
    assert pre(s, "Bash", {"command": "ruti mode wait off"}) is None
    assert pre(s, "PowerShell", {"command": "ruti status"}) is None
    assert pre(s, "TodoWrite") is None
    assert pre(s, "Bash", {"command": "ruti status && rm -rf x"}) is not None
    assert pre(s, "Bash", {"command": "rutix"}) is not None


def test_a_window_whose_reset_has_passed_counts_as_empty():
    modes.set_wait(SID, True)
    assert wait.effective_used(snap(99, resets_in=-10)) == 0.0
    assert pre(snap(99, resets_in=-10)) is None


def test_stop_waits_out_the_reset_and_then_resumes():
    modes.set_wait(SID, True)
    s = snap(96, resets_in=600)
    pre(s)
    now = [time.time()]
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    out = wait.stop(SID, s, sleep=fake_sleep, clock=lambda: now[0])
    assert out["decision"] == "block"
    assert "has reset" in out["reason"]
    assert 600 <= sum(slept) <= 600 + wait.RESUME_MARGIN_SECONDS + 1
    assert not modes.wait_state(SID).get("paused")


def test_stop_without_a_pause_returns_at_once():
    modes.set_wait(SID, True)
    assert wait.stop(SID, snap(96), sleep=lambda s: 1 / 0) is None


def test_turning_wait_off_mid_sleep_releases_the_session():
    modes.set_wait(SID, True)
    s = snap(96, resets_in=3600)
    pre(s)

    def sleep_then_cancel(seconds):
        modes.set_wait(SID, False)

    assert wait.stop(SID, s, sleep=sleep_then_cancel) is None


def test_the_wait_is_capped_even_for_a_nonsense_reset_time():
    modes.set_wait(SID, True)
    s = snap(96, resets_in=86400 * 3)
    pre(s)
    now = [0.0]

    def fake_sleep(seconds):
        now[0] += seconds

    wait.stop(SID, s, sleep=fake_sleep, clock=lambda: now[0])
    assert now[0] <= wait.MAX_WAIT_SECONDS + 1


def test_the_prompt_hook_names_the_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    monkeypatch.setattr(user_prompt_submit, "MEMO_FILE", tmp_path / "memo.json")
    monkeypatch.setattr(quota, "load", lambda: snap(20))
    modes.set_wait(SID, True)
    context, _ = user_prompt_submit.build_context("")
    assert "wait mode is ON" in context


def test_ruti_on_does_not_drop_wait_mode():
    modes.set_wait(SID, True)
    sessions.set_disabled(SID, True)
    sessions.set_disabled(SID, False)
    assert modes.current(SID)["wait"]


def test_install_registers_the_three_wait_hooks():
    settings = install.desired_settings({})
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        [entry] = settings["hooks"][event]
        assert entry["hooks"][0]["args"] == ["-m", "ruti.hooks.wait_gate"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] >= wait.MAX_WAIT_SECONDS
    # reinstalling replaces rather than stacks
    again = install.desired_settings(settings)
    assert len(again["hooks"]["Stop"]) == 1


# ------------------------------------------------------ Opus in ORANGE, with wait on


@pytest.mark.parametrize("band, wait_on, opus", [
    (quota.ORANGE, True, True),
    (quota.ORANGE, False, False),
    # Only ORANGE: UNKNOWN has no live reading for wait mode to pause on, and RED is
    # too close to 95% for the difference to matter.
    (quota.UNKNOWN, True, False),
    (quota.RED, True, False),
])
def test_wait_mode_lifts_the_no_opus_rule_in_orange_only(band, wait_on, opus):
    assert ("opus permitted" in quota.manager(band, wait=wait_on)) is opus


def test_outside_wait_mode_the_band_policy_is_unchanged():
    for band, policy in quota.BAND_POLICY.items():
        assert quota.manager(band, wait=False) == policy["manager"]


@pytest.mark.parametrize("wait_on, expected", [
    (True, "Manager for this band: opus permitted"),
    (False, "Manager for this band: sonnet, no opus"),
])
def test_the_prompt_hook_names_the_manager_wait_mode_allows(monkeypatch, tmp_path,
                                                            wait_on, expected):
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    monkeypatch.setattr(user_prompt_submit, "MEMO_FILE", tmp_path / "memo.json")
    monkeypatch.setattr(quota, "load", lambda: snap(72))
    assert snap(72).band == quota.ORANGE
    modes.set_wait(SID, wait_on)
    context, _ = user_prompt_submit.build_context("")
    assert expected in context

"""Wait mode: notice at 90%, refuse tools at 95%, sleep through the reset, resume."""

from __future__ import annotations

import time

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

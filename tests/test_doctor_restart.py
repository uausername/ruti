"""Restarting the proxy must not report success while the old process still runs."""

from __future__ import annotations

import pytest

from ruti import doctor, proc


@pytest.fixture
def fake(monkeypatch):
    state = {"listening": [38684], "taskkill_ok": False, "started": 0}

    def run(argv, **_kwargs):
        if argv[0] == "taskkill":
            if state["taskkill_ok"]:
                state["listening"] = []
            return proc.Result(argv, 0 if state["taskkill_ok"] else 1, "", "", 0.0)
        if argv[:2] == ["schtasks", "/End"]:
            return proc.Result(argv, 0, "", "", 0.0)
        raise AssertionError(f"unexpected command {argv}")

    def start():
        state["started"] += 1
        return "started via the RutiLiteLLM scheduled task"

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(doctor, "_listening_pids", lambda port=4000: list(state["listening"]))
    # 38684 is the interpreter pip's litellm.exe launcher started: a LiteLLM proxy.
    monkeypatch.setattr(doctor, "_process_table",
                        lambda: {38684: (500, "python.exe"), 500: (4, "litellm.exe")})
    monkeypatch.setattr(doctor, "_fix_proxy_start", start)
    monkeypatch.setattr(doctor, "_is_elevated", lambda: False)
    monkeypatch.setattr(doctor, "PORT_RELEASE_SECONDS", 0)
    return state


def test_a_refused_kill_fails_the_fix_instead_of_starting_a_second_proxy(fake):
    with pytest.raises(RuntimeError) as error:
        doctor._fix_proxy_restart()
    message = str(error.value)
    assert "PID 38684" in message
    assert "not elevated" in message
    assert "Start-ScheduledTask -TaskName RutiLiteLLM" in message
    # liveliness would have been answered by the old process: never get that far
    assert fake["started"] == 0


def test_a_confirmed_kill_goes_on_to_start_the_proxy(fake):
    fake["taskkill_ok"] = True
    assert doctor._fix_proxy_restart() == (
        "killed 38684, then started via the RutiLiteLLM scheduled task"
    )
    assert fake["started"] == 1


def test_a_process_that_survives_a_successful_taskkill_is_still_caught(fake, monkeypatch):
    def run(argv, **_kwargs):
        return proc.Result(argv, 0, "", "", 0.0)  # says it worked, port stays held

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(RuntimeError, match="still listening"):
        doctor._fix_proxy_restart()
    assert fake["started"] == 0


def test_something_else_on_the_port_is_never_killed(fake, monkeypatch):
    # A dev server squatting :4000 is not ruti's to kill, even from the "not
    # answering" branch or `openrouter setup`, which now both restart the proxy.
    monkeypatch.setattr(doctor, "_process_table", lambda: {38684: (4, "ruby.exe")})
    fake["taskkill_ok"] = True
    with pytest.raises(RuntimeError) as error:
        doctor._fix_proxy_restart()
    assert "PID 38684 (ruby.exe)" in str(error.value) and "not a LiteLLM" in str(error.value)
    assert fake["listening"] == [38684] and fake["started"] == 0


def test_litellm_is_recognised_by_its_launcher():
    table = {1: (2, "python.exe"), 2: (3, "litellm.exe"), 4: (3, "python.exe")}
    assert doctor._is_litellm(1, table) and doctor._is_litellm(2, table)
    assert not doctor._is_litellm(4, table) and not doctor._is_litellm(99, table)


def test_the_process_table_sees_this_process():
    import os
    import sys

    if sys.platform != "win32":
        pytest.skip("Toolhelp is Windows-only")
    parent, image = doctor._process_table()[os.getpid()]
    assert image.startswith("python") and parent

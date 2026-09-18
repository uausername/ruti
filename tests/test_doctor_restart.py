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
        raise AssertionError(f"unexpected command {argv}")

    def start():
        state["started"] += 1
        return "started via the RutiLiteLLM scheduled task"

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(doctor, "_listening_pids", lambda port=4000: list(state["listening"]))
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

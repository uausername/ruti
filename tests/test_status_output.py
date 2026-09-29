"""`ruti status` must not raise alarms `ruti doctor` knows to be the resting state."""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from ruti import cli, litellm_cfg, lmstudio, quota, vram


@pytest.fixture
def status(monkeypatch):
    snapshot = quota.Quota(
        five_hour=quota.Window(7.0, time.time() + 4 * 3600),
        seven_day=quota.Window(32.0, time.time() + 3.2 * 86400),
        captured_at=time.time(),
    )
    monkeypatch.setattr(quota, "load", lambda: snapshot)
    monkeypatch.setattr(vram, "primary_gpu", lambda: None)
    monkeypatch.setattr(lmstudio, "available", lambda: True)
    monkeypatch.setattr(litellm_cfg, "liveliness", lambda: False)
    monkeypatch.setattr(litellm_cfg, "include_is_wired", lambda: True)

    def run(*, server_up: bool, loaded: list | None = None) -> str:
        monkeypatch.setattr(lmstudio, "server_running", lambda: server_up)
        monkeypatch.setattr(lmstudio, "loaded_models", lambda: loaded or [])
        result = CliRunner().invoke(cli.main, ["status"])
        assert result.exit_code == 0, result.output
        # `output` already carries stderr too, where the console writes (click >= 8.2).
        return " ".join(result.output.split())

    return run


def test_a_stopped_lm_studio_is_the_resting_state_not_an_outage(status):
    # It used to print "DOWN -- every local request will fail over to a remote
    # provider", but the proxy has no fallback, and `delegate` starts the server itself.
    out = status(server_up=False)
    assert "DOWN" not in out and "fail over" not in out
    assert "server stopped" in out and "starts it" in out


def test_no_loaded_model_is_not_a_warning_either(status):
    out = status(server_up=True)
    assert "server up, no model loaded" in out and "loads one on demand" in out


def test_the_weekly_window_is_stated_once(status):
    out = status(server_up=False)
    assert out.count("7d 32% used") == 1
    assert "of the 7d window used" not in out

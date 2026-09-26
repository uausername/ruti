"""`openrouter setup` must leave the proxy serving what it wrote, not advise a restart
that does not work."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from ruti import cli, doctor, litellm_cfg, modes, openrouter, providers, sessions

FREE_ROUTER = {
    "provider": "openrouter", "alias": "free", "model": "openrouter/openrouter/free",
    "env_var": "RUTI_OPENROUTER_KEY_1", "supports_tools": True, "free": True,
    "coding": False, "enabled": True,
}
CODING = "cohere/north-mini-code:free"
GENERAL = "thinkingmachines/inkling-small:free"


@pytest.fixture
def setup(monkeypatch):
    state = {"registry": {"version": 1, "providers": [dict(FREE_ROUTER)]},
             "restarts": 0, "restart_error": None, "coding_mode": False,
             "served": None}

    def restart():
        state["restarts"] += 1
        if state["restart_error"]:
            raise RuntimeError(state["restart_error"])
        return "killed 16052, then started via the RutiLiteLLM scheduled task"

    def declared():
        return [r["alias"] for r in state["registry"]["providers"]]

    monkeypatch.setattr(providers, "load_registry", lambda: state["registry"])
    monkeypatch.setattr(providers, "save_registry", lambda reg: state.update(saved=reg))
    monkeypatch.setattr(cli, "load_dotenv", lambda: {"RUTI_OPENROUTER_KEY_1": "sk-or-v1-x" * 3})
    monkeypatch.setattr(openrouter, "fetch_catalog", lambda **_k: [])
    for name in ("write_providers", "wire_include", "sync_opencode"):
        monkeypatch.setattr(litellm_cfg, name, lambda *_a, **_k: None)
    monkeypatch.setattr(litellm_cfg, "routable_aliases", lambda: [])
    monkeypatch.setattr(litellm_cfg, "declared_models", declared)
    monkeypatch.setattr(litellm_cfg, "served_models",
                        lambda: state["served"] if state["served"] is not None else declared())
    monkeypatch.setattr(doctor, "_fix_proxy_restart", restart)
    monkeypatch.setattr(sessions, "current_session_id", lambda: "s1")
    monkeypatch.setattr(modes, "current",
                        lambda _sid: {"coding": state["coding_mode"], "free": "off"})

    def run(*extra):
        result = CliRunner().invoke(
            cli.main, ["openrouter", "setup", "--skip-verify", "--yes", *extra])
        state["output"] = " ".join((result.output + (result.stderr or "")).split())
        assert result.exit_code == 0, state["output"]
        return state["output"]

    state["run"] = run
    return state


def test_setup_restarts_the_proxy_and_confirms_what_it_serves(setup):
    out = setup["run"]("--models", f"{CODING},{GENERAL}")
    assert setup["restarts"] == 1
    assert "serving 3 of 3 declared model(s)" in out
    # the advice that left the old proxy on :4000 is gone
    assert "Stop-ScheduledTask" not in out and "Start-ScheduledTask" not in out


def test_a_proxy_still_missing_models_after_the_restart_is_reported(setup):
    setup["served"] = ["free"]
    out = setup["run"]("--models", CODING)
    assert "serving 1 of 2" in out and "north-mini-code" in out


def test_no_restart_leaves_the_proxy_alone_and_names_the_fix(setup):
    out = setup["run"]("--models", CODING, "--no-restart")
    assert setup["restarts"] == 0
    assert "ruti doctor --fix" in out
    assert "Stop-ScheduledTask" not in out


def test_a_failed_restart_says_so_and_points_at_doctor(setup):
    setup["restart_error"] = "could not stop the running proxy (PID 16052)"
    out = setup["run"]("--models", CODING)
    assert "restart failed" in out and "PID 16052" in out
    assert "ruti doctor" in out


def test_the_coding_mode_hint_only_appears_when_it_would_change_something(setup):
    out = setup["run"]("--models", CODING)
    assert "ruti mode coding on" in out

    setup["coding_mode"] = True
    setup["registry"]["providers"] = [dict(FREE_ROUTER)]
    out = setup["run"]("--models", CODING)
    assert "ruti mode coding on" not in out


def test_no_coding_hint_when_nothing_registered_is_a_coding_model(setup):
    out = setup["run"]("--models", GENERAL)
    assert "ruti mode coding on" not in out


def test_known_coding_models_are_marked_and_coding_flag_marks_the_rest(setup):
    setup["run"]("--models", f"{CODING},{GENERAL}")
    by_alias = {r["alias"]: r for r in setup["saved"]["providers"]}
    assert by_alias["north-mini-code"]["coding"] is True
    assert by_alias["inkling-small"]["coding"] is False

    setup["registry"]["providers"] = [dict(FREE_ROUTER)]
    setup["run"]("--models", GENERAL, "--coding")
    by_alias = {r["alias"]: r for r in setup["saved"]["providers"]}
    assert by_alias["inkling-small"]["coding"] is True


def test_coding_flag_marks_an_already_registered_alias_without_a_restart(setup):
    setup["run"]("--models", GENERAL)
    restarts = setup["restarts"]
    out = setup["run"]("--models", GENERAL, "--coding")
    assert "marked 1 alias(es) as coding" in out
    assert setup["restarts"] == restarts  # a flag in providers.json changes nothing served
    by_alias = {r["alias"]: r for r in setup["saved"]["providers"]}
    assert by_alias["inkling-small"]["coding"] is True


def test_marking_alone_does_not_promise_an_env_write(setup, monkeypatch):
    # No key on file: one is asked for, but marking an existing alias never uses it.
    monkeypatch.setattr(cli, "load_dotenv", lambda: {})
    setup["registry"]["providers"].append(
        {"provider": "openrouter", "alias": "inkling-small", "enabled": True,
         "model": "openrouter/thinkingmachines/inkling-small:free", "env_var": "GONE"})
    result = CliRunner().invoke(
        cli.main, ["openrouter", "setup", "--skip-verify", "--yes", "--key-stdin",
                   "--coding", "--models", GENERAL], input="sk-or-v1-abcdefghijkl\n")
    out = " ".join((result.output + (result.stderr or "")).split())
    assert result.exit_code == 0, out
    assert "marked 1 alias(es) as coding" in out and "litellm/.env" not in out


# ------------------------------------------------------------------ OpenCode sync


def test_setup_declares_to_opencode_what_is_on_disk_not_what_the_old_proxy_serves(
        setup, monkeypatch):
    # Before the restart the proxy still serves an alias removed since; handing its list
    # to OpenCode brought that alias back.
    setup["served"] = ["free", "kimi"]
    monkeypatch.setattr(litellm_cfg, "routable_aliases", lambda: setup["served"])
    synced = []
    monkeypatch.setattr(litellm_cfg, "sync_opencode", lambda names, **_k: synced.append(names))
    setup["run"]("--models", GENERAL, "--no-restart")
    assert synced and "kimi" not in synced[-1]
    assert "inkling-small" in synced[-1]


def test_provider_remove_drops_the_alias_from_opencode(setup, monkeypatch):
    synced = []
    monkeypatch.setattr(litellm_cfg, "sync_opencode", lambda names, **_k: synced.append(names))
    result = CliRunner().invoke(cli.main, ["provider", "remove", "free", "--yes"])
    assert result.exit_code == 0, result.output
    assert synced == [[]]


def test_setup_registers_a_zero_priced_model_as_free(setup, monkeypatch):
    monkeypatch.setattr(openrouter, "fetch_catalog", lambda **_k: [{
        "id": "stealth/space-bunny-alpha", "context_length": 1_000_000,
        "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"],
    }])
    setup["run"]("--models", "stealth/space-bunny-alpha", "--no-restart")
    [record] = [r for r in setup["saved"]["providers"] if r["alias"] == "space-bunny-alpha"]
    assert record["free"] is True

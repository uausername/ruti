import json
from types import SimpleNamespace

from click.testing import CliRunner

from ruti import cli, delegate, delegate_config, lmstudio, planner, proc, usage


def test_remote_alias_is_refused_before_any_model_request(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "probe", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(delegate.proc, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = delegate.run("private brief", model="ruti-router/gemini-flash",
                          directory=tmp_path, local_only=True)
    assert not result.ok and "local-*" in result.error


def test_local_load_failure_never_sends_brief_to_proxy_or_opencode(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(planner, "ensure_resident", fail)
    monkeypatch.setattr(delegate, "probe", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(delegate.proc, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = delegate.run("private brief", model="ruti-router/local-test",
                          directory=tmp_path, local_only=True)
    assert not result.ok and "no task was sent" in result.error
    assert not list(delegate.LOG_DIR.glob("task-*.md"))


def test_local_only_routes_directly_and_does_not_collect_proxy_usage(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(planner, "ensure_resident", lambda *a, **k: {})
    monkeypatch.setattr(lmstudio, "loaded_models", lambda: [SimpleNamespace(identifier="local-test")])
    monkeypatch.setattr(delegate, "probe", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(usage, "collect", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(delegate, "_opencode_executable", lambda: "opencode")

    def run(argv, **kwargs):
        if argv[0] == "opencode":
            calls.append((argv, kwargs))
        return proc.Result(argv, 0, "", "", 0)

    monkeypatch.setattr(delegate.proc, "run", run)
    result = delegate.run("private brief", model="ruti-router/local-test",
                          directory=tmp_path, local_only=True)
    assert result.ok and result.local_only
    [(argv, kwargs)] = calls
    assert "--pure" in argv and "ruti-local-only/local-test" in argv
    config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["ruti-local-only"]
    assert config["small_model"] == "ruti-local-only/local-test"
    assert config["provider"]["ruti-local-only"]["options"]["baseURL"] == "http://127.0.0.1:1234/v1"
    assert result.model_effective == "unknown"
    assert "direct LM Studio" in result.usage.note


def test_child_config_keeps_existing_runtime_options(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"permission": {"bash": "deny"}}))
    _, env = delegate_config.environment("aaaaaaaa")
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"] == {"bash": "deny"}
    assert config["provider"]["ruti-router"]["options"]["headers"]["x-ruti-run-id"] == "aaaaaaaa"


def test_cli_exposes_local_only_and_fails_cleanly_for_remote_alias():
    result = CliRunner().invoke(cli.main, ["delegate", "--model", "gemini-flash",
                                          "--task", "x", "--local-only", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["local_only"] is True

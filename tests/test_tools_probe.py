"""A tools probe that got no answer is "not verified", never "unsupported"."""

from __future__ import annotations

import litellm
import pytest

from ruti import config, providers, tls


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """test_key with TLS and discovery out of the way, and the tools call scripted."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "CA_BUNDLE", bundle)
    monkeypatch.setattr(tls, "bundle_works", lambda *a, **k: True)
    monkeypatch.setattr(litellm, "get_valid_models", lambda **_k: [])

    def run(tools_error: Exception):
        def completion(**kwargs):
            if "tools" in kwargs:
                raise tools_error
            return object()

        monkeypatch.setattr(litellm, "completion", completion)
        return providers.test_key("openrouter", "openrouter/cohere/north-mini-code:free",
                                  "sk-or-v1-test")

    return run


def _tools(verdict: providers.Verdict) -> providers.Stage:
    return next(s for s in verdict.stages if s.name == "tools")


def test_a_rate_limited_tools_probe_is_unverified_not_unsupported(probe):
    verdict = probe(litellm.RateLimitError("429", llm_provider="openrouter", model="m"))
    stage = _tools(verdict)
    assert stage.result == providers.UNVERIFIED
    assert "not verified" in stage.detail and "rate limited" in stage.detail
    assert "unsupported" not in stage.detail
    assert verdict.supports_tools is None
    assert verdict.usable and verdict.failure is None


def test_a_timeout_is_unverified_too(probe):
    verdict = probe(litellm.Timeout("slow", model="m", llm_provider="openrouter"))
    assert _tools(verdict).result == providers.UNVERIFIED


def test_a_real_refusal_still_counts_as_unsupported(probe):
    verdict = probe(litellm.BadRequestError("tools not supported", model="m",
                                            llm_provider="openrouter"))
    stage = _tools(verdict)
    assert stage.result == providers.FAIL and "unsupported" in stage.detail
    assert verdict.supports_tools is False


def test_route_says_unverified_rather_than_unsupported(monkeypatch):
    from ruti import litellm_cfg, router

    record = {"provider": "gemini", "alias": "g", "model": "gemini/x", "env_var": "K",
              "supports_tools": None, "enabled": True}
    monkeypatch.setattr(providers, "load_registry",
                        lambda: {"version": 1, "providers": [record]})
    monkeypatch.setattr(litellm_cfg, "served_models", lambda: ["g"])
    (candidate,) = router._remote_candidates(router.Task())
    assert "not verified" in candidate.blockers[0]
    assert "ruti provider test g" in candidate.blockers[0]


def test_provider_test_records_a_conclusive_answer(monkeypatch):
    from click.testing import CliRunner

    from ruti import cli

    record = {"provider": "gemini", "alias": "g", "model": "gemini/x", "env_var": "K",
              "supports_tools": None, "enabled": True}
    registry = {"version": 1, "providers": [record]}
    saved = []
    monkeypatch.setattr(providers, "load_registry", lambda: registry)
    monkeypatch.setattr(providers, "save_registry", saved.append)
    monkeypatch.setattr(cli, "load_dotenv", lambda: {"K": "secret-key-123"})
    verdict = providers.Verdict(stages=[providers.Stage("chat", providers.PASS),
                                        providers.Stage("tools", providers.PASS)],
                                supports_tools=True)
    monkeypatch.setattr(providers, "test_key", lambda *a, **k: verdict)

    CliRunner().invoke(cli.main, ["provider", "test", "g"])
    assert saved and saved[-1]["providers"][0]["supports_tools"] is True


@pytest.mark.parametrize("stages", [
    # the chat stage failed, so the tools probe never ran
    [providers.Stage("chat", providers.FAIL, "could not reach the provider")],
    [providers.Stage("chat", providers.PASS), providers.Stage("tools", providers.UNVERIFIED)],
])
def test_provider_test_keeps_the_record_when_tools_were_not_judged(monkeypatch, stages):
    from click.testing import CliRunner

    from ruti import cli

    record = {"provider": "gemini", "alias": "g", "model": "gemini/x", "env_var": "K",
              "supports_tools": True, "enabled": True}
    saved = []
    monkeypatch.setattr(providers, "load_registry",
                        lambda: {"version": 1, "providers": [record]})
    monkeypatch.setattr(providers, "save_registry", saved.append)
    monkeypatch.setattr(cli, "load_dotenv", lambda: {"K": "secret-key-123"})
    # supports_tools keeps its dataclass default, False -- which is not an answer
    monkeypatch.setattr(providers, "test_key",
                        lambda *a, **k: providers.Verdict(stages=stages))
    CliRunner().invoke(cli.main, ["provider", "test", "g"])
    assert not saved and record["supports_tools"] is True


def test_an_upstream_5xx_is_unverified_too(probe):
    verdict = probe(litellm.ServiceUnavailableError("503", model="m", llm_provider="openrouter"))
    assert _tools(verdict).result == providers.UNVERIFIED


def test_provider_list_shows_unverified_rather_than_no(monkeypatch):
    from click.testing import CliRunner

    from ruti import cli

    record = {"provider": "gemini", "alias": "g", "model": "gemini/x", "env_var": "K",
              "supports_tools": None, "enabled": True}
    monkeypatch.setattr(providers, "load_registry",
                        lambda: {"version": 1, "providers": [record]})
    monkeypatch.setattr(cli, "load_dotenv", lambda: {"K": "secret-key-123"})
    result = CliRunner().invoke(cli.main, ["provider", "list"])
    assert "unverified" in result.output + (result.stderr or "")

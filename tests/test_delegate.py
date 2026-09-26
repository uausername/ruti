"""A delegation reports the model that really worked, and substitution keeps its meaning.

The acceptance case: the proxy answers a router alias under the name of the model the
router picked. ruti must show both names and must not call that a substitution --
`substituted` stays reserved for a fallback standing in for a backend that is down.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from ruti import cli, delegate, ledger, proc, usage


class FakeProxy:
    """Answers /v1/chat/completions the way LiteLLM would, with chosen headers."""

    def __init__(self, model, *, group, fallbacks=0):
        reply = {"model": model, "choices": []}
        headers = {"x-litellm-model-group": group,
                   "x-litellm-attempted-fallbacks": str(fallbacks)}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_opencode(monkeypatch):
    """Stands in for `opencode run`: the proxy callback's lines appear as it works."""
    served = []

    def run(argv, **kwargs):
        if argv[0] == "git":
            return proc.Result(argv, 1, "", "not a repository", 0.0)
        with usage.USAGE_LOG.open("a", encoding="utf-8") as handle:
            for entry in served:
                handle.write(json.dumps({**entry, "at": time.time()}) + "\n")
        return proc.Result(argv, 0, "done", "", 0.1)

    monkeypatch.setattr(delegate.proc, "run", run)
    monkeypatch.setattr(usage, "SETTLE_SECONDS", 0)
    return served


def _served(model, alias="pareto-code", cost=0.0123):
    return {"requested": alias, "group": alias, "provider": "openrouter",
            "model": model, "upstream": "Anthropic", "id": "gen-1", "cost_usd": cost,
            "completion_tokens": 900}


def test_router_pick_is_shown_and_is_not_a_substitution(
        registry, fake_opencode, monkeypatch, tmp_path):
    proxy = FakeProxy("anthropic/claude-fable-5-1", group="pareto-code")
    monkeypatch.setattr(delegate, "PROXY_BASE", proxy.url)
    fake_opencode.extend([_served("anthropic/claude-fable-5-1")] * 3)
    try:
        outcome = delegate.run("write it", model="ruti-router/pareto-code",
                               directory=tmp_path, timeout=30)
    finally:
        proxy.close()

    assert outcome.model_requested == "ruti-router/pareto-code"
    assert outcome.model_answering == "anthropic/claude-fable-5-1"
    assert outcome.model_effective == "anthropic/claude-fable-5-1"
    assert outcome.router is True
    assert outcome.substituted is False

    summary = outcome.summary()
    assert summary["model_effective"] == "anthropic/claude-fable-5-1"
    assert summary["substituted"] is False
    assert summary["cost_usd"] == pytest.approx(0.0369)
    assert summary["usage"]["requests"] == 3

    recorded = [e for e in ledger.read_events() if e["event"] == "delegation"][-1]
    assert recorded["model_effective"] == "anthropic/claude-fable-5-1"
    assert recorded["provider"] == "openrouter"
    assert recorded["cost_usd"] == pytest.approx(0.0369)


def test_without_usage_lines_the_effective_model_is_unknown_not_the_alias(
        registry, fake_opencode, monkeypatch, tmp_path):
    proxy = FakeProxy("pareto-code", group="pareto-code")  # LiteLLM's rewritten name
    monkeypatch.setattr(delegate, "PROXY_BASE", proxy.url)
    try:
        outcome = delegate.run("write it", model="ruti-router/pareto-code",
                               directory=tmp_path, timeout=30)
    finally:
        proxy.close()
    assert outcome.model_effective == usage.UNKNOWN
    assert outcome.usage.note
    assert outcome.substituted is False


def test_a_local_model_answered_by_the_fallback_is_still_a_substitution(
        registry, fake_opencode, monkeypatch, tmp_path):
    proxy = FakeProxy("gemini-2.5-flash", group="gemini-flash", fallbacks=1)
    monkeypatch.setattr(delegate, "PROXY_BASE", proxy.url)
    try:
        outcome = delegate.run("write it", model="ruti-router/local-qwen3-4b",
                               directory=tmp_path, timeout=30)
    finally:
        proxy.close()
    assert outcome.substituted is True
    assert outcome.router is False


@pytest.mark.parametrize("answer, router, expected", [
    # the original rule, untouched for fixed models
    (delegate.Probe("local-qwen3-4b"), False, False),
    (delegate.Probe("gemini-2.5-flash"), False, True),
    (delegate.Probe(None), False, False),
    # what the proxy states outright wins either way
    (delegate.Probe("local-qwen3-4b", fallbacks=1), False, True),
    (delegate.Probe("x", group="gemini-flash", fallbacks=0), True, True),
    # a router naming its pick is doing its job
    (delegate.Probe("anthropic/claude-fable-5-1", group="pareto-code", fallbacks=0), True, False),
    (delegate.Probe("anthropic/claude-fable-5-1"), True, False),
    (delegate.Probe("gemini-2.5-flash", fallbacks=1), True, True),
])
def test_substitution_rule(answer, router, expected):
    alias = "pareto-code" if router else "local-qwen3-4b"
    assert delegate.is_substitution(alias, answer, router=router) is expected


def _outcome(model_effective, **usage_fields):
    outcome = delegate.Outcome(model_requested="ruti-router/pareto-code",
                               model_answering="pareto-code", router=True, exit_code=0,
                               model_effective=model_effective)
    outcome.usage = usage.Usage(alias="pareto-code", model=model_effective, **usage_fields)
    return outcome


def test_cli_prints_alias_and_real_model(monkeypatch):
    outcome = _outcome(
        "anthropic/claude-fable-5-1", requests=7, costed_requests=7, cost_usd=0.0412,
        models=[{"model": "anthropic/claude-fable-5-1", "upstream": "Anthropic",
                 "requests": 7, "cost_usd": 0.0412}],
    )
    monkeypatch.setattr(cli.delegate_mod, "run", lambda *a, **k: outcome)
    monkeypatch.setattr(cli, "_refuse_if_disabled", lambda _j: None)
    monkeypatch.setattr(cli, "_guard_free_mode", lambda *_a: None)

    result = CliRunner().invoke(cli.main, ["delegate", "--model", "pareto-code",
                                           "--task", "x"])
    text = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "pareto-code -> anthropic/claude-fable-5-1" in text
    assert "$0.0412" in text

    result = CliRunner().invoke(cli.main, ["delegate", "--model", "pareto-code",
                                           "--task", "x", "--json"])
    payload = json.loads(result.stdout)
    assert payload["model_requested"] == "ruti-router/pareto-code"
    assert payload["model_effective"] == "anthropic/claude-fable-5-1"
    assert payload["substituted"] is False


def test_cli_says_unknown_rather_than_repeating_the_alias(monkeypatch):
    outcome = _outcome(usage.UNKNOWN, note="the proxy is not recording which model answers")
    monkeypatch.setattr(cli.delegate_mod, "run", lambda *a, **k: outcome)
    monkeypatch.setattr(cli, "_refuse_if_disabled", lambda _j: None)
    monkeypatch.setattr(cli, "_guard_free_mode", lambda *_a: None)

    result = CliRunner().invoke(cli.main, ["delegate", "--model", "pareto-code",
                                           "--task", "x"])
    text = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "pareto-code -> unknown" in text
    assert "not recording" in text


def test_a_fallback_taking_over_mid_run_is_a_substitution(
        registry, fake_opencode, monkeypatch, tmp_path):
    # The probe before the run is answered honestly; the rate limit hits later, and the
    # proxy's fallback answers the rest under the alias's own name.
    proxy = FakeProxy("pareto-code", group="pareto-code")
    monkeypatch.setattr(delegate, "PROXY_BASE", proxy.url)
    rerouted = {**_served("gemini/gemini-2.5-flash"), "group": "gemini-flash"}
    fake_opencode.extend([_served("anthropic/claude-fable-5-1"), rerouted, rerouted])
    try:
        outcome = delegate.run("write it", model="ruti-router/pareto-code",
                               directory=tmp_path, timeout=30)
    finally:
        proxy.close()
    assert outcome.substituted is True
    assert outcome.usage.fallback_requests == 2
    assert "2 of 3 request(s) were answered by gemini-flash" in outcome.usage.note
    assert outcome.summary()["substituted"] is True

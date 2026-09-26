"""Reading the proxy's usage log back for one delegation."""

from __future__ import annotations

import json

import pytest

from ruti import usage


def _write(entries):
    with usage.USAGE_LOG.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _entry(at, *, requested="pareto-code", model="anthropic/claude-fable-5-1",
           cost=0.01, provider="openrouter", gen_id="gen-a", tokens=50):
    return {"at": at, "requested": requested, "group": requested, "model": model,
            "provider": provider, "id": gen_id, "cost_usd": cost,
            "completion_tokens": tokens, "upstream": "Anthropic"}


def test_only_this_alias_inside_the_window_counts():
    _write([
        _entry(99.0),                                  # the probe, before the run
        _entry(100.5),
        _entry(101.0, requested="free", model="x/y:free"),
        _entry(102.0),
        _entry(200.0),                                 # somebody's later run
    ])
    found = usage.in_window(usage.read_log(), "pareto-code", 100.0, 150.0)
    assert [e["at"] for e in found] == [100.5, 102.0]


def test_the_dominant_model_is_reported_and_costs_add_up():
    entries = [
        _entry(1, model="anthropic/claude-fable-5-1", cost=0.02),
        _entry(2, model="anthropic/claude-fable-5-1", cost=0.03),
        _entry(3, model="deepseek/deepseek-v4", cost=0.001),
    ]
    spent = usage.aggregate("pareto-code", entries)
    assert spent.model == "anthropic/claude-fable-5-1"
    assert [r["model"] for r in spent.models] == ["anthropic/claude-fable-5-1",
                                                  "deepseek/deepseek-v4"]
    assert spent.cost_usd == pytest.approx(0.051)
    assert spent.cost_complete


def test_unnamed_requests_never_borrow_the_alias():
    spent = usage.aggregate("pareto-code", [_entry(1, model=None, cost=None)])
    assert spent.model == usage.UNKNOWN
    assert spent.cost_usd is None
    assert not spent.cost_complete


def test_collect_says_why_when_the_proxy_recorded_nothing(monkeypatch):
    monkeypatch.setattr(usage, "recording_wired", lambda: False)
    spent = usage.collect("pareto-code", 0.0, 10.0, settle=0)
    assert spent.model == usage.UNKNOWN
    assert "not recording" in spent.note

    monkeypatch.setattr(usage, "recording_wired", lambda: True)
    spent = usage.collect("pareto-code", 0.0, 10.0, settle=0)
    assert "restart the proxy" in spent.note


def test_openrouter_is_asked_only_about_what_the_proxy_could_not_name():
    _write([
        _entry(5.0, gen_id="gen-known"),
        _entry(6.0, gen_id="gen-blind", model=None, cost=None),
    ])
    asked = []

    def lookup(gen_id):
        asked.append(gen_id)
        return {"model": "openai/gpt-6", "provider_name": "OpenAI", "total_cost": 0.2}

    spent = usage.collect("pareto-code", 0.0, 10.0, settle=0, lookup=lookup)
    assert asked == ["gen-blind"]
    assert {r["model"] for r in spent.models} == {"anthropic/claude-fable-5-1", "openai/gpt-6"}
    assert spent.cost_usd == pytest.approx(0.21)


def test_a_generation_openrouter_has_not_indexed_yet_is_retried(monkeypatch):
    monkeypatch.setattr(usage.time, "sleep", lambda _s: None)
    _write([_entry(5.0, gen_id="gen-late", model=None, cost=None)])
    calls = []

    def lookup(gen_id):
        calls.append(gen_id)
        return None if len(calls) == 1 else {"model": "z-ai/glm-5", "total_cost": 0.0}

    spent = usage.collect("pareto-code", 0.0, 10.0, settle=0, lookup=lookup)
    assert calls == ["gen-late", "gen-late"]
    assert spent.model == "z-ai/glm-5"


def test_money_is_split_by_provider_and_old_runs_are_not_passed_off_as_free(registry):
    events = [
        {"event": "delegation", "model": "ruti-router/pareto-code", "tier": "remote",
         "provider": "openrouter", "cost_usd": 0.04,
         "models": {"anthropic/claude-fable-5-1": {"requests": 7, "cost_usd": 0.04}}},
        # recorded before cost tracking: provider derived, money not invented
        {"event": "delegation", "model": "ruti-router/free", "tier": "remote"},
        {"event": "delegation", "model": "ruti-router/local-qwen3-4b", "tier": "local"},
    ]
    money = usage.summarise_costs(events)
    assert money["counted"]
    assert money["by_provider"]["openrouter"] == {"runs": 2, "costed_runs": 1, "usd": 0.04}
    assert money["by_provider"]["local"]["usd"] == 0.0
    assert money["uncosted_runs"] == 1
    assert money["by_model"]["anthropic/claude-fable-5-1"]["usd"] == pytest.approx(0.04)


def test_local_zero_alone_does_not_count_as_money_being_tracked(registry):
    money = usage.summarise_costs([
        {"event": "delegation", "model": "ruti-router/local-qwen3-4b", "tier": "local"},
        {"event": "delegation", "model": "ruti-router/pareto-code", "tier": "remote"},
    ])
    assert not money["counted"]


def test_requests_another_group_answered_are_counted_as_fallback():
    entries = [_entry(1, requested="laguna-s-2.1", model="gemini/gemini-2.5-flash"),
               _entry(2, requested="laguna-s-2.1", model="poolside/laguna-s-2.1:free")]
    entries[0]["group"] = "gemini-flash"
    result = usage.aggregate("laguna-s-2.1", entries)
    assert result.fallback_requests == 1 and result.fallback_groups == ["gemini-flash"]
    assert result.summary()["fallback_requests"] == 1


def test_a_routers_own_pick_is_not_a_fallback():
    result = usage.aggregate("pareto-code", [_entry(1), _entry(2, model="openai/gpt-6")])
    assert result.fallback_requests == 0
    assert "fallback_requests" not in result.summary()


def test_the_proxys_fallback_header_counts_even_under_the_same_group():
    entry = _entry(1, requested="free")
    entry["fallbacks"] = "1"
    assert usage.served_by_fallback(entry, "free")

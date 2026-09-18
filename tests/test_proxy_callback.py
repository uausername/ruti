"""The proxy-side callback: it must name the model a router picked, never the alias."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CALLBACK = Path(__file__).resolve().parent.parent / "litellm" / "ruti_usage.py"


@pytest.fixture(scope="module")
def cb():
    spec = importlib.util.spec_from_file_location("ruti_usage_under_test", CALLBACK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kwargs(*, requested="pareto-code", group="pareto-code",
            deployment="openrouter/openrouter/pareto-code", gen_id="gen-1",
            original=None, stream=False, extra=None):
    return {
        "original_response": original,
        "litellm_params": {"proxy_server_request": {"body": {"model": requested}}},
        "standard_logging_object": {
            "id": gen_id, "model": deployment, "model_group": group,
            "custom_llm_provider": deployment.split("/")[0], "stream": stream,
            "prompt_tokens": 100, "completion_tokens": 40,
            "hidden_params": {"additional_headers": extra or {}},
        },
    }


def test_plain_response_names_the_routers_pick(cb):
    body = {"id": "gen-1", "model": "anthropic/claude-fable-5-1", "provider": "Anthropic",
            "usage": {"cost": 0.0123}}
    record = cb.build_record(_kwargs(original=json.dumps(body)), 1.0, 2.0)
    assert record["requested"] == "pareto-code"
    assert record["model"] == "anthropic/claude-fable-5-1"
    assert record["model_source"] == "response"
    assert record["upstream"] == "Anthropic"
    assert record["cost_usd"] == pytest.approx(0.0123)


def test_stream_chunks_are_captured_before_litellm_renames_them(cb):
    # The same path LiteLLM takes: raw OpenRouter chunks through the patched parser.
    from litellm.llms.openrouter.chat.transformation import (
        OpenRouterChatCompletionStreamingHandler as Handler,
    )

    assert cb.STREAM_PATCHED, "the chunk_parser hook no longer matches this LiteLLM"
    parser = Handler(streaming_response=iter(()), sync_stream=True)
    for chunk in (
        {"id": "gen-2", "created": 1, "model": "qwen/qwen3-coder:free", "provider": "Chutes",
         "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {"id": "gen-2", "created": 1, "model": "qwen/qwen3-coder:free", "provider": "Chutes",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0}},
    ):
        parser.chunk_parser(chunk)

    record = cb.build_record(
        _kwargs(requested="free", group="free", deployment="openrouter/openrouter/free",
                gen_id="gen-2", original="first stream response received", stream=True),
        1.0, 2.0,
    )
    assert record["model"] == "qwen/qwen3-coder:free"
    assert record["upstream"] == "Chutes"
    assert record["cost_usd"] == 0


def test_a_router_it_could_not_read_stays_unnamed(cb):
    record = cb.build_record(_kwargs(gen_id="gen-never-seen"), 1.0, 2.0)
    # Never the router's own name standing in for a model.
    assert record["model"] is None
    assert record["model_source"] is None


def test_a_fixed_model_is_named_by_its_deployment(cb):
    record = cb.build_record(
        _kwargs(requested="gemini-flash", group="gemini-flash",
                deployment="gemini/gemini-2.5-flash", gen_id="x"),
        1.0, 2.0,
    )
    assert record["model"] == "gemini/gemini-2.5-flash"
    assert record["model_source"] == "deployment"


def test_cost_falls_back_to_the_figure_litellm_copied_from_openrouter(cb):
    record = cb.build_record(
        _kwargs(gen_id="gen-3", extra={"llm_provider-x-litellm-response-cost": 0.5}),
        1.0, 2.0,
    )
    assert record["cost_usd"] == 0.5


def test_a_fallback_is_filed_under_what_the_client_asked_for(cb):
    body = {"model": "google/gemini-2.5-flash"}
    record = cb.build_record(
        _kwargs(requested="local-qwen3-4b", group="gemini-flash",
                deployment="gemini/gemini-2.5-flash", original=json.dumps(body),
                extra={"x-litellm-attempted-fallbacks": 1}),
        1.0, 2.0,
    )
    assert record["requested"] == "local-qwen3-4b"
    assert record["group"] == "gemini-flash"
    assert record["fallbacks"] == 1

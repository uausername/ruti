"""A LiteLLM proxy callback: one line per completion, naming the model that really ran.

Loaded by `litellm_settings.callbacks: ruti_usage.handler` in config.yaml. It exists
because the proxy hides the one fact a router makes interesting. When a client asks
for `pareto-code`, OpenRouter picks a real model and says which in the `model` field
of its response -- and LiteLLM then overwrites that field with the name the client
asked for (`_override_openai_response_model`), and in a stream discards it earlier
still (`CustomStreamWrapper` stamps its own model name on every chunk). By the time
any client or ordinary callback sees the response, "pareto-code" is all that is left.
A delegation that OpenRouter handed to a frontier model reads exactly like one that
went to a free model.

So the raw value is taken where it still exists: from the provider's JSON body for a
plain response, and from each raw OpenRouter chunk, keyed by its generation id, for a
stream. The cost comes from the same place -- OpenRouter states it in `usage.cost`,
and LiteLLM always asks it to. Nothing here makes a network call.

`ruti delegate` reads the resulting `usage.jsonl` for the window its run occupied.
This file deliberately imports nothing from ruti: it runs inside the proxy process,
and an import error here would stop the proxy from starting at all. For the same
reason every path through it swallows its own exceptions -- losing one usage line is
acceptable, failing a completion because bookkeeping broke is not.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

MAX_BYTES = 4 * 1024 * 1024

# Raw (model, upstream provider, cost) per generation id, filled as stream chunks
# arrive and read once the stream's success event fires. Bounded, because a stream
# that errors midway never reaches the success event to be removed.
_SEEN: OrderedDict[str, dict[str, Any]] = OrderedDict()
_SEEN_MAX = 4096


def _usage_log() -> Path:
    """Mirrors ruti.config's state root. Duplicated on purpose -- see the docstring."""
    override = os.environ.get("RUTI_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
        root = Path(base) / "ruti" if base else Path.home() / ".local" / "state" / "ruti"
    return root / "usage.jsonl"


def _remember(chunk: Any) -> None:
    if not isinstance(chunk, dict):
        return
    gen_id = chunk.get("id")
    if not gen_id:
        return
    seen = _SEEN.get(gen_id)
    if seen is None:
        seen = _SEEN[gen_id] = {}
        while len(_SEEN) > _SEEN_MAX:
            _SEEN.popitem(last=False)
    if chunk.get("model"):
        seen["model"] = chunk["model"]
    if chunk.get("provider"):
        seen["upstream"] = chunk["provider"]
    usage = chunk.get("usage")
    if isinstance(usage, dict) and usage.get("cost") is not None:
        seen["cost"] = usage["cost"]


def _patch_openrouter_stream() -> bool:
    """Record each raw OpenRouter chunk before LiteLLM rewrites its model name.

    A patch of a LiteLLM internal, so it may stop matching after an upgrade. If it
    does, this returns False and streams simply lose their model name -- `ruti
    delegate` then falls back to OpenRouter's generation endpoint by id, which still
    works because the id survives LiteLLM intact.
    """
    try:
        from litellm.llms.openrouter.chat.transformation import (
            OpenRouterChatCompletionStreamingHandler as Handler,
        )
    except Exception:
        return False
    # Wrap LiteLLM's own parser, not an earlier copy of this wrapper: if the module is
    # ever loaded twice, the newest load must be the one whose table gets filled.
    original = getattr(Handler.chunk_parser, "_ruti_original", Handler.chunk_parser)

    def chunk_parser(self: Any, chunk: dict) -> Any:
        try:
            _remember(chunk)
        except Exception:
            pass
        return original(self, chunk)

    chunk_parser._ruti_original = original  # type: ignore[attr-defined]
    Handler.chunk_parser = chunk_parser
    return True


STREAM_PATCHED = _patch_openrouter_stream()


def _raw_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The provider's own JSON for a non-streamed response, if LiteLLM kept it."""
    original = kwargs.get("original_response")
    if not isinstance(original, str) or not original.strip().startswith("{"):
        return {}
    try:
        body = json.loads(original)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _requested(kwargs: dict[str, Any], payload: dict[str, Any]) -> str | None:
    """The name the client asked for -- not the group that served it.

    They differ after a fallback, and that difference is the point: the line has to
    be findable under the alias the delegation requested, while `group` records who
    actually took the request.
    """
    params = kwargs.get("litellm_params") or {}
    request = params.get("proxy_server_request") or {}
    body = request.get("body") if isinstance(request, dict) else None
    if isinstance(body, dict) and body.get("model"):
        return str(body["model"])
    return payload.get("model_group")


def _is_router(deployment: str) -> bool:
    # OpenRouter's routing endpoints live under its own `openrouter/` namespace, so a
    # deployment of one reads `openrouter/openrouter/<router>`.
    return deployment.startswith("openrouter/openrouter/")


def build_record(kwargs: dict[str, Any], start: Any, end: Any) -> dict[str, Any]:
    payload = kwargs.get("standard_logging_object") or {}
    hidden = payload.get("hidden_params") or {}
    extra = hidden.get("additional_headers") or {}
    gen_id = payload.get("id")
    deployment = str(payload.get("model") or "")
    provider = payload.get("custom_llm_provider")

    body = _raw_body(kwargs)
    seen = _SEEN.pop(gen_id, {}) if gen_id else {}

    model = body.get("model") or seen.get("model")
    source = "response" if model else None
    if not model and deployment and not _is_router(deployment):
        # No router in the path: the provider serves the model it was asked for, and
        # that is the configured deployment -- a concrete model, never the alias.
        model, source = deployment, "deployment"

    cost = ((body.get("usage") or {}).get("cost") if isinstance(body.get("usage"), dict)
            else None)
    if cost is None:
        cost = seen.get("cost")
    if cost is None:
        # LiteLLM copies OpenRouter's `usage.cost` here. Its own price-list estimate
        # (`response_cost`) is deliberately not used: for a router it has no price at
        # all, and for a free-tier key it would invent a bill that never arrives.
        cost = extra.get("llm_provider-x-litellm-response-cost")

    def _epoch(value: Any) -> float | None:
        if hasattr(value, "timestamp"):
            return value.timestamp()
        return float(value) if isinstance(value, (int, float)) else None

    return {
        "at": _epoch(end) or time.time(),
        "start": _epoch(start),
        "requested": _requested(kwargs, payload),
        "group": payload.get("model_group"),
        "deployment": deployment or None,
        "provider": provider,
        "model": model,
        "model_source": source,
        "upstream": body.get("provider") or seen.get("upstream"),
        "id": gen_id,
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "prompt_tokens": payload.get("prompt_tokens"),
        "completion_tokens": payload.get("completion_tokens"),
        "fallbacks": extra.get("x-litellm-attempted-fallbacks"),
        "stream": bool(payload.get("stream")),
    }


def _append(record: dict[str, Any]) -> None:
    path = _usage_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class RutiUsage(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            _append(build_record(kwargs, start_time, end_time))
        except Exception:
            pass

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            _append(build_record(kwargs, start_time, end_time))
        except Exception:
            pass


handler = RutiUsage()

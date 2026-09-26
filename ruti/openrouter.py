"""The OpenRouter catalogue, and ruti's shortlist of coding-worthy free models.

There are two honest ways to answer "what should coding work route to": a baked-in
shortlist that was known-good when this was written, and a live query to OpenRouter's
public model list so the answer does not rot. `ruti openrouter models` merges them --
the shortlist first, then anything else in the catalogue that clears the same bar --
and marks which are actually present upstream right now. `ruti openrouter setup`
registers the ones you pick as LiteLLM aliases.

The catalogue endpoint needs no key. It is cached because it is ~1 MB of JSON and the
list changes on the order of days, not seconds.

Two of the "models" here are not models at all. `openrouter/pareto-code` and
`openrouter/free` are OpenRouter routing endpoints: each dynamically picks a real
model per request (Pareto picks a strong coding model within a cost tier; the free
router rotates zero-cost models to spread rate limits). They are registered as
aliases like anything else, just flagged so the rest of ruti can reason about them.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .config import STATE_ROOT, read_json, write_json

CATALOG_URL = "https://openrouter.ai/api/v1/models"
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
CATALOG_CACHE = STATE_ROOT / "openrouter-catalog.json"
CATALOG_TTL_SECONDS = 6 * 3600

# OpenRouter's own routing endpoints -- verified against
# openrouter.ai/docs/guides/routing/routers. Not single models.
PARETO_CODE = "openrouter/pareto-code"
FREE_ROUTER = "openrouter/free"
ROUTERS: tuple[str, ...] = (PARETO_CODE, FREE_ROUTER)

# Endpoints built for code, by their publishers' own description in the catalogue --
# which carries no machine-readable coding flag, so this is kept by hand. A model's
# name is not evidence: `north-mini-code` qualifies because Cohere ships it as an
# agentic coding model, not because of the suffix. Checked 2026-09-18.
CODING_MODELS: frozenset[str] = frozenset({
    PARETO_CODE,
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
})

# Confirmed present in the catalogue at the time of writing (2026-09) and
# tool-capable. `recommended()` drops any that have since disappeared upstream;
# this is the floor, not the ceiling. The free coding models come first: before
# 2026-09-18 the free part held none at all -- one was a finance-tuned model -- so
# coding mode had nothing zero-cost to prefer. `north-mini-code` was dropped on
# 2026-09-26 for poor delegation results; the three after `laguna` replaced it.
# `space-bunny-alpha` is a stealth model: zero-priced, but temporary by nature.
DEFAULT_SHORTLIST: tuple[str, ...] = (
    PARETO_CODE,
    FREE_ROUTER,
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "stealth/space-bunny-alpha",
    "z-ai/glm-5.3-flash",
    "thinkingmachines/inkling-small:free",
    "nvidia/nemotron-3.5-lightning:free",
    "dots-studio/dots-3-note-preview:free",
)

# Below this an OpenCode delegation cannot even hold its own 8k-token system prompt
# plus a modest brief with any room to work.
MIN_CONTEXT = 32_000


def fetch_catalog(*, force: bool = False, timeout: float = 15.0) -> list[dict[str, Any]]:
    """OpenRouter's public model list, cached for six hours. [] if unreachable."""
    cached = read_json(CATALOG_CACHE, default=None)
    fresh = (
        isinstance(cached, dict)
        and time.time() - cached.get("at", 0) < CATALOG_TTL_SECONDS
    )
    if fresh and not force:
        return cached.get("models", [])

    try:
        with urllib.request.urlopen(CATALOG_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data") or []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        # A stale cache still beats nothing.
        return cached.get("models", []) if isinstance(cached, dict) else []

    try:
        write_json(CATALOG_CACHE, {"at": time.time(), "models": models})
    except OSError:
        pass
    return models


def is_free(slug: str, entry: dict[str, Any] | None = None) -> bool:
    """Zero-cost by slug, or by the catalogue's own price when there is an entry.

    The suffix alone misses zero-priced models published without it -- stealth models
    such as `stealth/space-bunny-alpha` -- which then ranked and were refused as metered.
    A router's price is `-1` (it depends on the model picked), so it never reads as 0.
    """
    if slug.endswith(":free") or slug == FREE_ROUTER:
        return True
    pricing = (entry or {}).get("pricing") or {}
    try:
        prices = [float(pricing[key]) for key in ("prompt", "completion")]
        prices += [float(pricing["request"])] if "request" in pricing else []
    except (KeyError, TypeError, ValueError):
        return False
    return all(price == 0 for price in prices)


def is_router(slug: str) -> bool:
    return slug in ROUTERS


def slug_of(litellm_model: str) -> str:
    """`openrouter/openrouter/pareto-code` -> `openrouter/pareto-code`; others as given."""
    prefix = "openrouter/"
    return litellm_model[len(prefix):] if litellm_model.startswith(prefix) else litellm_model


def is_router_record(record: dict[str, Any]) -> bool:
    """Whether a registered alias is a router rather than one fixed model.

    Records written before the flag existed are judged from their model string, so an
    existing registry needs no migration.
    """
    if record.get("router") is not None:
        return bool(record["router"])
    model = str(record.get("model") or "")
    return model.startswith("openrouter/") and is_router(slug_of(model))


def is_coding(slug: str) -> bool:
    """Whether this endpoint is specifically for code: one of `CODING_MODELS`.

    Anything else is marked at registration (`ruti openrouter setup --coding`) or by
    hand in providers.json -- nothing is guessed from a name.
    """
    return slug in CODING_MODELS


def is_coding_record(record: dict[str, Any]) -> bool:
    """Whether a registered alias is tuned for code.

    The record's own flag, or the model being a known coding one. Records written
    before `CODING_MODELS` existed carry `coding: false` for every model except
    pareto-code, so the flag alone would keep coding mode inert for them.
    """
    if record.get("coding"):
        return True
    model = str(record.get("model") or "")
    return model.startswith("openrouter/") and is_coding(slug_of(model))


def generation(gen_id: str, api_key: str, *, timeout: float = 15.0) -> dict[str, Any] | None:
    """OpenRouter's own record of one completion: the model it ran, and what it cost.

    None when the id is not (yet) known -- OpenRouter indexes a generation a few
    seconds after it finishes -- or when OpenRouter cannot be reached.
    """
    from urllib.parse import urlencode

    request = urllib.request.Request(
        f"{GENERATION_URL}?{urlencode({'id': gen_id})}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def alias_for(slug: str) -> str:
    """The short name to route to through the proxy, e.g. inkling-small, pareto-code."""
    base = slug.split("/")[-1]
    return base[: -len(":free")] if base.endswith(":free") else base


def litellm_model_for(slug: str) -> str:
    """The model string LiteLLM needs -- OpenRouter is litellm-native under this prefix."""
    return f"openrouter/{slug}"


def _supports_tools(entry: dict[str, Any]) -> bool:
    return "tools" in (entry.get("supported_parameters") or [])


def _context(entry: dict[str, Any] | None) -> int:
    if not entry:
        return 0
    top = entry.get("top_provider") or {}
    return int(entry.get("context_length") or top.get("context_length") or 0)


def _row(slug: str, entry: dict[str, Any] | None, *, shortlisted: bool) -> dict[str, Any]:
    router = is_router(slug)
    return {
        "id": slug,
        "alias": alias_for(slug),
        "present": entry is not None or router,
        "shortlisted": shortlisted,
        "router": router,
        "free": is_free(slug, entry),
        "coding": is_coding(slug),
        "context_length": _context(entry),
        # The routing endpoints exist to drive agentic/coding work; the catalogue
        # entry for them does not always advertise `tools`, but they do support it.
        "supports_tools": True if router else (_supports_tools(entry) if entry else False),
        "name": (entry or {}).get("name") or slug,
    }


def recommended(
    catalog: list[dict[str, Any]] | None = None,
    *,
    free_only: bool = True,
    coding_only: bool = True,
) -> list[dict[str, Any]]:
    """Shortlist first (in declared order), then everything else that clears the bar."""
    catalog = catalog if catalog is not None else fetch_catalog()
    by_id = {e.get("id"): e for e in catalog}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for slug in DEFAULT_SHORTLIST:
        if free_only and not is_free(slug, by_id.get(slug)) and not is_router(slug):
            continue
        rows.append(_row(slug, by_id.get(slug), shortlisted=True))
        seen.add(slug)

    extras: list[dict[str, Any]] = []
    for entry in catalog:
        slug = entry.get("id")
        if not slug or slug in seen:
            continue
        if free_only and not is_free(slug, entry):
            continue
        if coding_only and not _supports_tools(entry):
            continue
        if _context(entry) < MIN_CONTEXT:
            continue
        extras.append(_row(slug, entry, shortlisted=False))

    extras.sort(key=lambda r: r["context_length"], reverse=True)
    return rows + extras

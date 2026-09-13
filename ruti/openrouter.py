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
CATALOG_CACHE = STATE_ROOT / "openrouter-catalog.json"
CATALOG_TTL_SECONDS = 6 * 3600

# OpenRouter's own routing endpoints -- verified against
# openrouter.ai/docs/guides/routing/routers. Not single models.
PARETO_CODE = "openrouter/pareto-code"
FREE_ROUTER = "openrouter/free"
ROUTERS: tuple[str, ...] = (PARETO_CODE, FREE_ROUTER)

# Confirmed present in the catalogue at the time of writing (2026-09) and
# tool-capable. `recommended()` drops any that have since disappeared upstream;
# this is the floor, not the ceiling.
DEFAULT_SHORTLIST: tuple[str, ...] = (
    PARETO_CODE,
    FREE_ROUTER,
    "thinkingmachines/inkling-small:free",
    "dots-studio/dots-3-note-preview:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "nvidia/nemotron-3.5-lightning:free",
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


def is_free(slug: str) -> bool:
    return slug.endswith(":free") or slug == FREE_ROUTER


def is_router(slug: str) -> bool:
    return slug in ROUTERS


def is_coding(slug: str) -> bool:
    """Whether this endpoint is specifically for code.

    Only `pareto-code` qualifies by construction: it is OpenRouter's coding router.
    The catalogue advertises no coding flag, and a model's name is not evidence, so
    nothing else is guessed at -- a record in providers.json can be marked by hand.
    """
    return slug == PARETO_CODE


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
        "free": is_free(slug),
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
        if free_only and not is_free(slug) and not is_router(slug):
            continue
        rows.append(_row(slug, by_id.get(slug), shortlisted=True))
        seen.add(slug)

    extras: list[dict[str, Any]] = []
    for entry in catalog:
        slug = entry.get("id")
        if not slug or slug in seen:
            continue
        if free_only and not is_free(slug):
            continue
        if coding_only and not _supports_tools(entry):
            continue
        if _context(entry) < MIN_CONTEXT:
            continue
        extras.append(_row(slug, entry, shortlisted=False))

    extras.sort(key=lambda r: r["context_length"], reverse=True)
    return rows + extras

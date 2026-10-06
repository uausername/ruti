"""Check that registered OpenRouter aliases still match the live catalogue.

`ruti doctor` runs at every session start and hourly in the background, so a
model that left the catalogue, stopped being free, raised its prices, or lost
tool-call support surfaces there instead of as a failed delegation. This module
only REPORTS findings -- removing or replacing a model is the user's call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config, openrouter, providers
from .config import read_json, write_json

PRICE_RISE = 1.5  # completion price at least 1.5x the baseline -> finding


@dataclass(frozen=True)
class Finding:
    alias: str
    slug: str
    kind: str  # "gone" | "not_free" | "price_rise" | "no_tools"
    message: str  # one short human sentence, e.g. "no longer in OpenRouter's catalogue"


def prices_file():  # resolved at call time so tests' patched config.STATE_ROOT applies
    return config.STATE_ROOT / "openrouter-prices.json"


def _parse_prices(entry: dict[str, Any] | None) -> tuple[float, float] | None:
    """Per-M prompt/completion prices from a catalogue entry, or None if unparseable.

    A bad pricing block is treated as "unknown" rather than a finding, so a malformed
    entry cannot produce a false positive on a free or pricier check.
    """
    pricing = (entry or {}).get("pricing") or {}
    try:
        prompt_per_token = float(pricing["prompt"])
        completion_per_token = float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None
    return prompt_per_token * 1_000_000, completion_per_token * 1_000_000


def review(records, catalog, baseline) -> tuple[list[Finding], dict]:
    """Compare registered OpenRouter records against the live catalogue.

    Pure: no I/O. Returns findings and a new baseline dict (the old one plus any
    first sightings seeded in). The input baseline is never mutated -- a model's
    price only ever rises against the first price seen, so existing entries are
    not overwritten even when prices creep up gradually.
    """
    by_id = {e.get("id"): e for e in catalog}
    findings: list[Finding] = []
    new_baseline = dict(baseline)

    for record in records:
        if record.get("provider") != "openrouter":
            continue
        if record.get("enabled", True) is False:
            continue

        alias = record.get("alias", "")
        slug = openrouter.slug_of(record["model"])
        entry = by_id.get(slug)

        if entry is None:
            findings.append(Finding(
                alias, slug, "gone",
                "no longer in OpenRouter's catalogue",
            ))
            continue

        if openrouter.is_router_record(record):
            continue

        prices = _parse_prices(entry)
        if prices is not None:
            prompt_per_m, completion_per_m = prices

            if record.get("free") is True:
                if prompt_per_m > 0 or completion_per_m > 0:
                    findings.append(Finding(
                        alias, slug, "not_free",
                        f"no longer free: ${prompt_per_m:.2f} / "
                        f"${completion_per_m:.2f} per M tokens",
                    ))

            seen = new_baseline.get(slug)
            old = seen.get("completion") if isinstance(seen, dict) else None
            if not isinstance(old, (int, float)):
                # First sighting -- or a baseline entry damaged on disk, which is
                # re-seeded rather than allowed to break the whole check.
                new_baseline[slug] = {
                    "prompt": prompt_per_m,
                    "completion": completion_per_m,
                }
            elif old > 0 and completion_per_m >= old * PRICE_RISE:
                findings.append(Finding(
                    alias, slug, "price_rise",
                    f"output price up from ${old:.2f} to "
                    f"${completion_per_m:.2f} per M tokens",
                ))

        if record.get("supports_tools"):
            if "tools" not in (entry.get("supported_parameters") or []):
                findings.append(Finding(
                    alias, slug, "no_tools",
                    "the catalogue no longer lists tool calls",
                ))

    return findings, new_baseline


def check() -> list[Finding] | None:
    """Review registered OpenRouter models against the live catalogue.

    Returns None when the catalogue is unreachable -- not a finding, just unknown,
    so the caller can say "not checked" rather than "all clear".
    """
    catalog = openrouter.fetch_catalog()
    if not catalog:
        return None

    records = providers.load_registry()["providers"]
    baseline = read_json(prices_file(), default={})
    if not isinstance(baseline, dict):
        baseline = {}

    findings, new_baseline = review(records, catalog, baseline)
    if new_baseline != baseline:
        try:
            write_json(prices_file(), new_baseline)
        except OSError:
            pass
    return findings

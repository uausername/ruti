"""Zero-cost is what the catalogue charges, not only what the slug is called.

Stealth models are published at price 0 without a `:free` suffix, and were registered,
ranked and refused under free mode as metered.
"""

from __future__ import annotations

import pytest

from ruti import openrouter


def entry(prompt="0", completion="0", **extra):
    return {"pricing": {"prompt": prompt, "completion": completion, **extra},
            "supported_parameters": ["tools"], "context_length": 1_000_000}


@pytest.mark.parametrize("slug", ["poolside/laguna-s-2.1:free", openrouter.FREE_ROUTER])
def test_the_slug_alone_still_says_free(slug):
    assert openrouter.is_free(slug)


def test_a_zero_priced_entry_is_free_without_the_suffix():
    assert openrouter.is_free("stealth/space-bunny-alpha", entry())


@pytest.mark.parametrize("pricing", [
    entry("0.00000004", "0.0000005"),   # an ordinary paid model
    entry("-1", "-1"),                  # a router: the price depends on its pick
    entry(request="0.001"),             # free tokens, paid per request
    {"pricing": {"prompt": "0"}},       # half a price is not a price
    {"pricing": {"prompt": "zero", "completion": "0"}},
    {},
    None,
])
def test_anything_short_of_a_zero_price_is_metered(pricing):
    assert not openrouter.is_free("some/model", pricing)


def test_recommended_lists_a_zero_priced_model_and_leaves_out_a_paid_one():
    catalog = [{"id": "stealth/bunny", **entry()},
               {"id": "z-ai/paid", **entry("0.00000004", "0.0000005")}]
    rows = {r["id"]: r for r in openrouter.recommended(catalog, free_only=True)}
    assert rows["stealth/bunny"]["free"] is True
    assert "z-ai/paid" not in rows

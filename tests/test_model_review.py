"""Tests for ruti.model_review — model catalogue review and doctor integration."""
from __future__ import annotations

import pytest

from ruti import config, doctor, model_review, openrouter, providers


def rec(alias, model, **kw):
    return {
        "provider": "openrouter",
        "alias": alias,
        "model": model,
        "supports_tools": True,
        "enabled": True,
        **kw,
    }


def entry(slug, prompt="0.0000001", completion="0.0000005", tools=True):
    return {
        "id": slug,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"] if tools else [],
        "context_length": 262144,
    }


def test_record_not_in_catalogue():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [],
        {},
    )
    assert findings == [model_review.Finding("test", "openrouter/test", "gone",
                                              "no longer in OpenRouter's catalogue")]


def test_router_record_missing_from_catalogue():
    findings, baseline = model_review.review(
        [rec("free", "openrouter/openrouter/free", free=True)],
        [],
        {},
    )
    assert findings == [model_review.Finding("free", "openrouter/free", "gone",
                                              "no longer in OpenRouter's catalogue")]
    assert baseline == {}


def test_router_record_present_unpriced_no_tools():
    findings, baseline = model_review.review(
        [rec("free", "openrouter/openrouter/free", free=True)],
        [entry("openrouter/free", tools=False)],
        {},
    )
    assert findings == []
    assert baseline == {}


def test_free_record_non_zero_prices():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test", free=True)],
        [entry("openrouter/test", prompt="0.0000005", completion="0.000001")],
        {},
    )
    assert len(findings) == 1
    assert findings[0].kind == "not_free"


def test_free_record_zero_prices():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test", free=True)],
        [entry("openrouter/test", prompt="0", completion="0")],
        {},
    )
    assert findings == []


def test_tools_supported_but_catalogue_missing_tools():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test", supports_tools=True)],
        [entry("openrouter/test", tools=False)],
        {},
    )
    assert len(findings) == 1
    assert findings[0].kind == "no_tools"


def test_tools_not_supported_record_no_finding():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test", supports_tools=False)],
        [entry("openrouter/test", tools=False)],
        {},
    )
    assert findings == []


def test_first_sighting_empty_baseline():
    baseline = {}
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.0000005")],
        baseline,
    )
    assert findings == []
    # _parse_prices multiplies by 1M: 0.0000001 -> 0.1, 0.0000005 -> 0.5
    assert new_baseline == {"openrouter/test": {"prompt": pytest.approx(0.1), "completion": pytest.approx(0.5)}}


def test_first_sighting_baseline_object_not_mutated():
    baseline = {}
    _findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.0000005")],
        baseline,
    )
    assert baseline == {}  # input baseline unchanged


def test_price_rise_1_5x():
    baseline = {"openrouter/test": {"prompt": 0.1, "completion": 0.5}}
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.00000075")],
        baseline,
    )
    assert len(findings) == 1
    assert findings[0].kind == "price_rise"
    assert new_baseline["openrouter/test"]["completion"] == 0.5


def test_price_rise_1_4x_no_finding():
    baseline = {"openrouter/test": {"prompt": 0.1, "completion": 0.5}}
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.0000007")],
        baseline,
    )
    assert findings == []
    assert new_baseline["openrouter/test"]["completion"] == 0.5


def test_baseline_entry_never_overwritten():
    baseline = {"openrouter/test": {"prompt": 0.1, "completion": 0.5}}
    findings1, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.00000075")],
        baseline,
    )
    assert findings1[0].kind == "price_rise"
    # Second review: baseline still has completion 0.5, not overwritten
    findings2, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.00000075")],
        baseline,
    )
    assert findings2[0].kind == "price_rise"
    assert baseline["openrouter/test"]["completion"] == 0.5


def test_damaged_baseline_entry_reseeded():
    baseline = {"openrouter/test": "junk"}
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.0000005")],
        baseline,
    )
    assert findings == []
    assert new_baseline == {"openrouter/test": {"prompt": pytest.approx(0.1), "completion": pytest.approx(0.5)}}


def test_damaged_baseline_entry_reseeded_dict():
    baseline = {"openrouter/test": {}}
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="0.0000001", completion="0.0000005")],
        baseline,
    )
    assert findings == []
    assert new_baseline == {"openrouter/test": {"prompt": pytest.approx(0.1), "completion": pytest.approx(0.5)}}


def test_gemini_provider_ignored():
    findings, _ = model_review.review(
        [{"provider": "gemini", "alias": "gemini-flash-lite", "model": "gemini/gemini-2.5-flash-lite",
          "supports_tools": True, "enabled": True}],
        [],
        {},
    )
    assert findings == []


def test_enabled_false_record_ignored():
    findings, _ = model_review.review(
        [rec("test", "openrouter/openrouter/test", enabled=False)],
        [],
        {},
    )
    assert findings == []


def test_unparseable_pricing_no_finding():
    findings, new_baseline = model_review.review(
        [rec("test", "openrouter/openrouter/test")],
        [entry("openrouter/test", prompt="abc")],
        {},
    )
    assert findings == []
    assert new_baseline == {}


def test_check_returns_none_when_catalogue_empty(monkeypatch):
    monkeypatch.setattr(openrouter, "fetch_catalog", lambda **k: [])
    result = model_review.check()
    assert result is None


def test_check_returns_empty_with_single_entry(monkeypatch, tmp_path):
    from ruti.config import STATE_ROOT
    import json

    # Patch STATE_ROOT so prices file goes to tmp_path
    monkeypatch.setattr(config, "STATE_ROOT", tmp_path)

    monkeypatch.setattr(providers, "load_registry",
                        lambda: {"version": 1, "providers": [rec("test", "openrouter/openrouter/test")]})
    monkeypatch.setattr(openrouter, "fetch_catalog",
                        lambda **k: [entry("openrouter/test", prompt="0.0000001", completion="0.0000005")])

    # First call - creates the prices file
    result = model_review.check()
    assert result == []
    prices_file = config.STATE_ROOT / "openrouter-prices.json"
    assert prices_file.exists()

    # Read current prices and double the completion price
    prices = json.loads(prices_file.read_text(encoding="utf-8"))
    # Change completion from 0.0000005 to 0.000001 (2x)
    prices["openrouter/test"]["completion"] = 0.000001
    prices_file.write_text(json.dumps(prices) + "\n", encoding="utf-8")

    # Second call - should find price_rise
    result2 = model_review.check()
    assert len(result2) == 1
    assert result2[0].kind == "price_rise"


def test_doctor_check_none_status(monkeypatch):
    from ruti import doctor
    monkeypatch.setattr(model_review, "check", lambda: None)
    check = doctor._check_openrouter_models()
    assert check.status == doctor.OK
    assert "not checked" in check.message


def test_doctor_check_empty_status(monkeypatch):
    from ruti import doctor
    monkeypatch.setattr(model_review, "check", lambda: [])
    check = doctor._check_openrouter_models()
    assert check.status == doctor.OK


def test_doctor_check_warn_status(monkeypatch):
    from ruti import doctor
    monkeypatch.setattr(model_review, "check",
                        lambda: [model_review.Finding("dead", "x/y", "gone",
                                                       "no longer in OpenRouter's catalogue")])
    check = doctor._check_openrouter_models()
    assert check.status == doctor.WARN
    assert "dead" in check.detail
    assert "ruti provider remove" in check.detail
"""`route` must tell quota apart from money, and routers apart from models."""

from __future__ import annotations

import time

import pytest

from ruti import litellm_cfg, lmstudio, modes, quota, router


@pytest.fixture
def ranking(registry, monkeypatch):
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models",
                        lambda: [r["alias"] for r in registry])
    snapshot = quota.Quota(
        five_hour=quota.Window(10.0, time.time() + 3 * 3600), seven_day=None,
        captured_at=time.time(),
    )
    assert snapshot.band == quota.GREEN

    def rank(kind, files, loc, *, coding=True):
        monkeypatch.setattr(modes, "current", lambda _sid: {"coding": coding, "free": "off"})
        result = router.rank(router.Task(kind=kind, files=files, loc=loc), snapshot)
        return result, {e["executor"]: e for e in result["ranked"]}

    return rank


def test_routers_and_billing_are_marked(ranking):
    _, by_name = ranking("implement", 3, 300)
    pareto = by_name["ruti-router/pareto-code"]
    assert pareto["router"] is True
    assert pareto["metered"] is True
    assert "USD" in pareto["pays_in"] and "OpenRouter" in pareto["pays_in"]
    # "no subscription quota" must never be the whole story for a metered executor
    assert any("metered" in reason for reason in pareto["reasons"])
    assert any("router" in reason for reason in pareto["reasons"])

    free = by_name["ruti-router/free"]
    assert free["router"] is True and free["metered"] is False

    inkling = by_name["ruti-router/inkling-small"]
    assert inkling["router"] is False and inkling["metered"] is False

    assert by_name["ruti-router/gemini-flash-lite"]["metered"] is None
    assert by_name["claude:sonnet"]["metered"] is False
    assert "subscription" in by_name["claude:sonnet"]["pays_in"]


def test_an_easy_task_puts_every_free_executor_above_the_paid_router(ranking):
    # The task that started this: a 1-file, ~100-line helper, spelled out in detail.
    result, by_name = ranking("boilerplate", 1, 137, coding=True)
    assert result["task"]["difficulty"] <= router.LOW_DIFFICULTY
    order = [e["executor"] for e in result["ranked"]]
    paid = order.index("ruti-router/pareto-code")
    for free_one in ("ruti-router/free", "ruti-router/inkling-small"):
        assert order.index(free_one) < paid
    # a price nobody recorded is not treated as free either
    assert order.index("ruti-router/inkling-small") < order.index("ruti-router/gemini-flash-lite")
    assert any("difficulty" in r for r in by_name["ruti-router/pareto-code"]["reasons"])
    assert result["ranked"][0]["executor"] != "ruti-router/pareto-code"


def test_hard_work_still_gets_the_coding_router_in_coding_mode(ranking):
    result, _ = ranking("analyze", 3, 300, coding=True)
    assert result["task"]["difficulty"] > router.LOW_DIFFICULTY
    assert result["ranked"][0]["executor"] == "ruti-router/pareto-code"

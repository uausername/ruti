"""Coding mode must change the ranking, and its hint must not contradict free mode.
Plus `route --probe`, which ranks without leaving advice to be chased."""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from ruti import cli, ledger, litellm_cfg, lmstudio, modes, openrouter, quota, router
from ruti.hooks import user_prompt_submit

# As `openrouter setup` wrote it before CODING_MODELS existed: coding=False.
NORTH = {
    "provider": "openrouter", "alias": "north-mini-code",
    "model": "openrouter/cohere/north-mini-code:free",
    "env_var": "RUTI_OPENROUTER_KEY_1", "supports_tools": True, "free": True,
    "coding": False, "context_window": 256_000, "enabled": True,
}


def test_the_free_part_of_the_shortlist_has_coding_models():
    free_coding = [s for s in openrouter.DEFAULT_SHORTLIST
                   if openrouter.is_free(s) and openrouter.is_coding(s)]
    assert free_coding, "coding mode would have nothing zero-cost to prefer"
    rows = openrouter.recommended([], free_only=True)
    assert any(r["coding"] and r["free"] for r in rows)


def test_a_record_written_before_the_tags_still_counts_as_coding():
    assert openrouter.is_coding_record(NORTH)
    assert not openrouter.is_coding_record(
        {"model": "openrouter/thinkingmachines/inkling-small:free", "coding": False})
    # marked by hand, or by `setup --coding`
    assert openrouter.is_coding_record({"model": "gemini/gemini-2.5-flash", "coding": True})


@pytest.fixture
def ranking(registry, monkeypatch):
    registry.append(dict(NORTH))
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models", lambda: [r["alias"] for r in registry])
    snapshot = quota.Quota(
        five_hour=quota.Window(10.0, time.time() + 3 * 3600), seven_day=None,
        captured_at=time.time(),
    )

    def rank(*, coding: bool, free: str = "off", record: bool = True):
        monkeypatch.setattr(modes, "current", lambda _sid: {"coding": coding, "free": free})
        result = router.rank(router.Task(kind="implement", files=3, loc=300), snapshot,
                             record=record)
        return result, {e["executor"]: e for e in result["ranked"]}

    return rank


def test_coding_mode_ranks_a_free_coding_model_above_a_general_one(ranking):
    _, off = ranking(coding=False)
    north, inkling = "ruti-router/north-mini-code", "ruti-router/inkling-small"
    assert off[north]["coding"] is True
    assert off[north]["score"] == off[inkling]["score"]

    _, on = ranking(coding=True)
    assert on[north]["score"] > on[inkling]["score"]


def test_under_free_hard_coding_mode_picks_a_free_coding_model(ranking):
    result, _ = ranking(coding=True, free="hard")
    assert result["ranked"][0]["executor"] == "ruti-router/north-mini-code"


ALIASES = [("pareto-code", False), ("north-mini-code", True)]


def test_the_hint_under_free_hard_never_names_pareto_code():
    note = modes.coding_note("hard", ALIASES)
    assert "pareto-code" not in note
    assert "`north-mini-code`" in note

    note = modes.coding_note("hard", [("pareto-code", False)])
    assert "pareto-code" not in note and "no zero-cost coding alias" in note


def test_the_hint_names_metered_ones_only_where_free_mode_permits():
    assert "`pareto-code`" in modes.coding_note("off", ALIASES)
    soft = modes.coding_note("soft", ALIASES)
    assert soft.index("north-mini-code") < soft.index("pareto-code")
    assert "warning the user" in soft


def test_the_prompt_hook_reads_the_registry(registry, monkeypatch):
    registry.append(dict(NORTH))
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "hard"})
    (note, *_rest) = user_prompt_submit._mode_notes("s1")
    assert "north-mini-code" in note and "pareto-code" not in note


def test_a_probe_leaves_no_advice_for_the_hook_to_chase(ranking, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    result, _ = ranking(coding=False, record=False)
    assert result["probe"] is True
    assert ledger.unfollowed_route("s1") is None

    ranking(coding=False)
    assert ledger.unfollowed_route("s1") is not None


def test_route_probe_flag_skips_the_ledger(ranking, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setattr(cli, "_refuse_if_disabled", lambda _j: None)
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": False, "free": "off"})
    monkeypatch.setattr(quota, "load", lambda: quota.Quota(
        five_hour=quota.Window(10.0, time.time() + 3 * 3600), seven_day=None,
        captured_at=time.time()))
    result = CliRunner().invoke(cli.main, ["route", "--probe", "--json"])
    assert result.exit_code == 0, result.output
    assert ledger.unfollowed_route("s1") is None
    assert not ledger.LEDGER.exists()


def test_the_hint_skips_coding_aliases_route_rules_out(registry):
    # Registered while the free model was rate limited: tools unverified.
    registry.append(dict(NORTH, supports_tools=None))
    assert "north-mini-code" not in [alias for alias, _ in modes.coding_aliases()]

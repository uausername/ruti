"""The language rankings as `route`, `openrouter suggest` and the prompt hook use them.

`test_rankings.py` covers the module itself -- fetching, caching, slug resolution. This
covers what the rest of ruti does with a ranking once it has one: which executor it
moves and by how much, what it leaves alone, and that nothing breaks when there is none.
"""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from ruti import cli, litellm_cfg, lmstudio, modes, quota, rankings, router, track
from ruti.hooks import user_prompt_submit


def ranking_of(*shares: tuple[str, float], language: str = "TypeScript") -> rankings.Ranking:
    """A ranking holding exactly these (slug, percent) pairs, ranked in the order given."""
    return rankings.Ranking(
        language=language,
        days=["2026-09-23", "2026-09-24", "2026-09-25"],
        shares={slug.lower(): rankings.Share(slug=slug, percent=percent, rank=i)
                for i, (slug, percent) in enumerate(shares, start=1)},
        others=100.0 - sum(percent for _, percent in shares),
    )


@pytest.fixture
def ranked(registry, monkeypatch):
    """`router.rank` over the conftest registry, with the ranking and modes chosen per test."""
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models", lambda: [r["alias"] for r in registry])
    monkeypatch.setattr(track, "load", lambda: {})
    snapshot = quota.Quota(
        five_hour=quota.Window(10.0, time.time() + 3 * 3600), seven_day=None,
        captured_at=time.time(),
    )
    asked: list[str | None] = []

    def rank(ranking: rankings.Ranking | None, *, coding: bool = True,
             language: str | None = "TypeScript"):
        monkeypatch.setattr(modes, "current",
                            lambda _sid: {"coding": coding, "free": "off"})

        def load(lang, **_kwargs):
            asked.append(lang)
            return ranking

        monkeypatch.setattr(rankings, "load", load)
        result = router.rank(router.Task(kind="implement", files=3, loc=300), snapshot,
                             record=False, language=language)
        everyone = result["ranked"] + result["rejected"]
        return result, {e["executor"]: e for e in everyone}

    rank.asked = asked  # type: ignore[attr-defined]
    return rank


def test_the_language_leader_gets_the_full_bonus(ranked):
    _, before = ranked(None)
    result, after = ranked(ranking_of(("thinkingmachines/inkling-small:free", 12.0),
                                      ("z-ai/glm-5.3-flash", 6.0)))

    inkling = after["ruti-router/inkling-small"]
    assert inkling["ranking"] == {"percent": 12.0, "rank": 1, "factor": 1.3}
    assert inkling["score"] == pytest.approx(
        round(before["ruti-router/inkling-small"]["score"] * 1.3, 3))
    assert any("12.0% of TypeScript tokens" in r and "#1" in r and "x1.30" in r
               for r in inkling["reasons"])
    assert result["rankings"]["language"] == "TypeScript"
    assert result["rankings"]["detected"] is False
    assert len(result["rankings"]["days"]) == 3


def test_an_unranked_model_is_left_exactly_as_it_was(ranked):
    _, before = ranked(None)
    _, after = ranked(ranking_of(("thinkingmachines/inkling-small:free", 12.0)))

    lite = after["ruti-router/gemini-flash-lite"]
    assert lite["ranking"] is None
    assert lite["score"] == before["ruti-router/gemini-flash-lite"]["score"]
    assert not any("rankings:" in r for r in lite["reasons"])


def test_half_the_leaders_share_is_half_the_bonus(ranked):
    _, after = ranked(ranking_of(("z-ai/glm-5.3-flash", 20.0),
                                 ("thinkingmachines/inkling-small:free", 10.0)))
    assert after["ruti-router/inkling-small"]["ranking"]["factor"] == pytest.approx(1.15)


def test_a_free_alias_does_not_borrow_its_paid_siblings_share(ranked):
    # The user's call: only the exact endpoint counts. The paid variant of the same
    # weights is a different endpoint with different limits.
    _, after = ranked(ranking_of(("thinkingmachines/inkling-small", 12.0)))
    assert after["ruti-router/inkling-small"]["ranking"] is None


def test_coding_mode_off_never_asks_for_a_ranking(ranked):
    result, after = ranked(ranking_of(("thinkingmachines/inkling-small:free", 12.0)),
                           coding=False)
    assert ranked.asked == []
    assert after["ruti-router/inkling-small"]["ranking"] is None
    assert result["rankings"]["unavailable"] == "coding mode is off"


def test_no_ranking_to_be_had_changes_nothing(ranked):
    result, after = ranked(None)
    _, off = ranked(None, coding=False)
    for name, entry in after.items():
        assert entry["ranking"] is None
        if name.startswith("ruti-router/") and not entry["coding"]:
            assert entry["score"] == off[name]["score"]
    assert "could not be fetched" in result["rankings"]["unavailable"]


def test_the_language_is_detected_when_not_given(ranked, monkeypatch):
    seen = []
    monkeypatch.setattr(rankings, "detect_language", lambda root: seen.append(root) or "Python")
    result, _ = ranked(ranking_of(("z-ai/glm-5.3-flash", 9.0), language="Python"),
                       language=None)
    assert seen, "detection must run when --language is not given"
    assert ranked.asked == ["Python"]
    assert result["rankings"]["detected"] is True


def test_a_ruled_out_executor_shows_its_share_without_a_score_change(ranked, registry):
    registry[2]["supports_tools"] = False  # inkling-small can no longer drive opencode
    _, after = ranked(ranking_of(("thinkingmachines/inkling-small:free", 12.0)))
    inkling = after["ruti-router/inkling-small"]
    assert inkling["blockers"]
    assert inkling["ranking"]["rank"] == 1
    assert not any("x1.30" in r for r in inkling["reasons"])


# -------------------------------------------------------------------------- the CLI


def test_route_refuses_a_language_openrouter_does_not_rank():
    outcome = CliRunner().invoke(cli.main, ["route", "--kind", "implement",
                                            "--language", "Klingon", "--probe", "--json"])
    assert outcome.exit_code == 2
    assert "Klingon" in outcome.output


def test_route_passes_a_normalised_language_through(monkeypatch):
    seen = {}

    def rank(task, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(router, "rank", rank)
    outcome = CliRunner().invoke(cli.main, ["route", "--kind", "implement",
                                            "--language", "ts", "--probe", "--json"])
    assert outcome.exit_code == 0, outcome.output
    assert seen["language"] == "TypeScript"


CATALOG = [
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash",
     "context_length": 1_048_576, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
    {"id": "z-ai/glm-5.3-flash", "name": "GLM 5.3 Flash", "context_length": 1_048_576,
     "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.0000002", "completion": "0.000001"}},
]


def test_suggest_lists_the_leaders_ruti_lacks(registry, monkeypatch):
    registry.append({"alias": "glm-5.3-flash", "model": "openrouter/z-ai/glm-5.3-flash",
                     "enabled": True})
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "soft"})
    monkeypatch.setattr("ruti.openrouter.fetch_catalog", lambda **_k: CATALOG)
    monkeypatch.setattr(rankings, "load", lambda lang, **_k: ranking_of(
        ("deepseek/deepseek-v4.1-flash", 12.6), ("z-ai/glm-5.3-flash", 7.4), language=lang))

    outcome = CliRunner().invoke(cli.main, ["openrouter", "suggest", "--language",
                                            "typescript", "--json"])
    assert outcome.exit_code == 0, outcome.output
    report = json.loads(outcome.output)
    assert report["language"] == "TypeScript" and report["detected"] is False
    assert [row["slug"] for row in report["suggestions"]] == ["deepseek/deepseek-v4.1-flash"]
    assert report["suggestions"][0]["needs_warning"] is True
    assert report["registered"] == [{"alias": "glm-5.3-flash", "slug": "z-ai/glm-5.3-flash",
                                     "percent": 7.4, "rank": 2}]


def test_suggest_says_so_when_the_ranking_is_unreachable(monkeypatch):
    monkeypatch.setattr("ruti.openrouter.fetch_catalog", lambda **_k: [])
    monkeypatch.setattr(rankings, "load", lambda lang, **_k: None)
    outcome = CliRunner().invoke(cli.main, ["openrouter", "suggest", "--language", "Go"])
    assert outcome.exit_code == 1
    assert "unreachable" in outcome.output


# ------------------------------------------------------------------------- the hook


def test_the_hook_says_nothing_without_a_project(monkeypatch):
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "off"})
    monkeypatch.setattr(rankings, "hint", lambda *a, **k: "should not be asked")
    assert user_prompt_submit._rankings_note("s1", None) is None


def test_the_hook_says_nothing_outside_coding_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": False, "free": "off"})
    monkeypatch.setattr(rankings, "hint", lambda *a, **k: "should not be asked")
    assert user_prompt_submit._rankings_note("s1", str(tmp_path)) is None


def test_the_hook_passes_the_projects_language_and_registry(registry, monkeypatch, tmp_path):
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "hard"})
    seen = {}

    def hint(language, models, *, free_level, **_k):
        seen.update(language=language, models=models, free_level=free_level)
        return "ruti rankings: something better"

    monkeypatch.setattr(rankings, "hint", hint)
    assert user_prompt_submit._rankings_note("s1", str(tmp_path)) == \
        "ruti rankings: something better"
    assert seen["language"] == "Go" and seen["free_level"] == "hard"
    assert "openrouter/thinkingmachines/inkling-small:free" in seen["models"]


def test_a_failing_hint_never_breaks_the_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": True, "free": "off"})

    def broken(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rankings, "detect_language", broken)
    assert user_prompt_submit._rankings_note("s1", str(tmp_path)) is None

"""A project's own rule for the cheapest model that may hold the seat.

The classifier does not read a project's CLAUDE.md, so a prompt about a migration can come
back as plain `implement` work and a Sonnet seat. The floor is what the project says
instead: a model below it is blocked, whatever the task was classified as.
"""

from __future__ import annotations

import json

import pytest

from ruti import manager, quota, seat_floor
from ruti.seat_floor import Floor


class Snap:
    def __init__(self, band):
        self.band = band


RULES = {"floors": [
    {"model": "opus", "why": "migrations, RLS and reducer parity are Opus work",
     "match": ["supabase/migrations", r"\bRLS\b", "EventReducer"]},
    {"model": "sonnet", "why": "tests follow an established shape", "match": ["widget test"]},
]}


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".ruti").mkdir()
    (tmp_path / ".ruti" / "seat-floor.json").write_text(json.dumps(RULES), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def no_advisor(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    monkeypatch.delenv(manager.ADVISOR_ENV, raising=False)


def test_a_match_in_the_prompt_sets_the_floor(project):
    floor = seat_floor.check(project, "add supabase/migrations/0082_x.sql")
    assert floor == Floor("opus", "migrations, RLS and reducer parity are Opus work")


def test_a_match_in_the_message_before_the_prompt_counts_too(project):
    # "go ahead" is judged by what it agrees to.
    assert seat_floor.for_prompt(None, "go ahead", "I will change the EventReducer",
                                 project=str(project)).model == "opus"


def test_the_highest_matching_floor_wins_and_matching_is_case_insensitive(project):
    floor = seat_floor.check(project, "a widget test for the rls policy")
    assert floor is not None and floor.model == "opus"


def test_nothing_matching_nothing_missing_and_nothing_readable_all_mean_no_floor(
        project, tmp_path):
    assert seat_floor.check(project, "rename a button") is None
    assert seat_floor.check(tmp_path / "elsewhere", "supabase/migrations") is None
    assert seat_floor.check(None, "supabase/migrations") is None
    (project / ".ruti" / "seat-floor.json").write_text("{not json", encoding="utf-8")
    assert seat_floor.check(project, "supabase/migrations") is None


def test_a_bad_pattern_or_an_unknown_model_is_skipped_not_fatal(project):
    rules = {"floors": [{"model": "opus", "match": ["(unclosed", "migration"]},
                        {"model": "gpt", "match": ["migration"]}]}
    (project / ".ruti" / "seat-floor.json").write_text(json.dumps(rules), encoding="utf-8")
    assert seat_floor.check(project, "a migration").model == "opus"


def test_a_floor_blocks_every_model_below_it_and_names_the_rule():
    floor = Floor("opus", "migrations are Opus work")
    advice = manager.advise(snapshot=Snap(quota.GREEN), kind="implement", difficulty=0.2,
                            session_id=None, current=None, context_tokens=0, floor=floor)
    best = advice.best
    assert best is not None and best.seat.model == "opus"
    blocked = {e.seat.model: e.blockers for e in advice.ranked if not e.eligible}
    assert blocked["sonnet"] == ["project rule: migrations are Opus work"]
    assert "haiku" in blocked
    assert advice.task["floor"] == "opus: migrations are Opus work"


def test_without_a_floor_the_same_task_may_sit_on_a_cheaper_model():
    advice = manager.advise(snapshot=Snap(quota.YELLOW), kind="implement", difficulty=0.2,
                            session_id=None, current=None, context_tokens=0)
    assert advice.best.seat.model != "opus"
    assert advice.task["floor"] is None


def test_a_floor_moves_a_sonnet_seat_up_now(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    floor = Floor("opus", "migrations are Opus work")
    sonnet = manager.Seat("sonnet", "medium")
    advice = manager.advise(snapshot=Snap(quota.YELLOW), kind="implement", difficulty=0.2,
                            session_id=None, current=sonnet, context_tokens=0, floor=floor)
    assert advice.best.seat.model == "opus"
    assert advice.switch["verdict"] == "now"
    assert manager.is_down(advice) is False

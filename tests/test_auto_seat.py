"""When the session may change its own seat: up at once, down only with good reason.

`manager.advise` is exercised for real (band stubbed, the advisor off); only the
classifier is replaced, since what is pinned here is the policy that sits on its answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ruti import auto_seat, manager, quota

LONG = "x" * 150


class Snap:
    def __init__(self, band):
        self.band = band


def seated(model_id, effort, tokens):
    return {"model": {"id": model_id}, "effort": {"level": effort},
            "context_window": {"context_window_size": 1_000_000, "used_percentage": 5,
                               "current_usage": {"input_tokens": tokens}}}


def guess(kind="boilerplate", difficulty=0.1, confidence=0.9):
    return lambda *_a, **_k: SimpleNamespace(
        kind=kind, difficulty=difficulty, kind_confidence=confidence,
        difficulty_confidence=0.9)


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    monkeypatch.delenv(manager.ADVISOR_ENV, raising=False)
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))


def sit(model_id, effort, tokens=10_000):
    manager.record_seat("s1", seated(model_id, effort, tokens))


def test_the_default_is_on_and_off_does_nothing():
    assert auto_seat.mode() == "on"
    auto_seat.set_mode("off")
    out = auto_seat.plan("s1", LONG, classify=guess())
    assert out["action"] == {} and out["apply"] is False
    with pytest.raises(ValueError):
        auto_seat.set_mode("sometimes")


def test_shadow_names_the_move_and_on_applies_it():
    sit("claude-sonnet-5-5", "low")
    auto_seat.set_mode("shadow")
    shadow = auto_seat.plan("s1", LONG, classify=guess("debug", 0.6, 0.9))
    assert shadow["direction"] == "up" and shadow["action"].get("effort")
    assert shadow["apply"] is False
    auto_seat.set_mode("on")
    live = auto_seat.plan("s1", LONG, classify=guess("debug", 0.6, 0.9))
    assert live["apply"] is True and live["action"] == shadow["action"]


def test_a_move_down_waits_for_the_same_advice_twice_in_a_row():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    first = auto_seat.plan("s1", LONG, classify=guess())
    assert first["direction"] == "down" and first["apply"] is False
    assert "two prompts" in first["held"]
    second = auto_seat.plan("s1", LONG, classify=guess())
    assert second["apply"] is True and second["action"]


def test_a_move_down_needs_a_confident_classification_and_an_easy_task():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    for _ in range(2):
        unsure = auto_seat.plan("s1", LONG, classify=guess(confidence=0.75))
    assert unsure["apply"] is False and "confident" in unsure["held"]
    for _ in range(2):
        hard = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.95))
    assert hard["apply"] is False


def test_a_task_reused_from_the_last_prompt_never_cheapens_the_seat():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    manager.remember_task("s1", "boilerplate", 0.1)
    for _ in range(2):
        out = auto_seat.plan("s1", "yes, go ahead")
    assert out["task"]["source"] == "reused" and out["apply"] is False


def test_a_model_switch_with_a_large_context_stays_a_recommendation():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high", tokens=400_000)
    out = auto_seat.plan("s1", LONG, classify=guess())
    assert out["apply"] is False and out["action"].get("model") is None


def test_no_task_to_judge_holds_and_every_plan_is_journalled(tmp_path):
    out = auto_seat.plan("s1", "ok")
    assert out["apply"] is False and "no task" in out["held"]
    lines = (tmp_path / "auto-seat.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["session"] == "s1"


def test_a_prompt_of_sixty_characters_is_classified_and_a_shorter_one_is_not():
    sit("claude-sonnet-5-5", "low")
    out = auto_seat.plan("s1", "y" * auto_seat.MIN_PROMPT_CHARS, classify=guess("debug", 0.6, 0.9))
    assert out["task"]["source"] == "fresh"
    shorter = auto_seat.plan("s1", "y" * (auto_seat.MIN_PROMPT_CHARS - 1),
                             classify=guess("debug", 0.6, 0.9))
    assert shorter["task"]["source"] == "reused"
    assert auto_seat.MIN_PROMPT_CHARS == 60


def test_a_seat_that_fits_is_recorded_as_checked_so_the_status_line_can_say_so():
    sit("claude-sonnet-5-5", "low")
    out = auto_seat.plan("s1", LONG, classify=guess())
    assert out["verdict"] == "stay"
    assert manager.matched("s1") == "sonnet/low"


def test_the_manager_mark_shows_what_the_manager_may_do(monkeypatch):
    import time

    from ruti import modes, statusline

    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    modes.set_manager("s1", True)
    for value, mark in (("on", "mgr⚡"), ("shadow", "mgr~"), ("off", "mgr")):
        auto_seat.set_mode(value)
        out = statusline.render({"session_id": "s1"}, snapshot)
        third = out.split("\n")[2]
        assert (mark in third) and (mark + " " in third or third.endswith(mark) or "mgr" in third)
        assert ("⚡" in third) == (value == "on") and ("mgr~" in third) == (value == "shadow")


def test_the_word_seat_is_always_there_while_auto_seat_looks_after_the_seat(monkeypatch):
    import time

    from ruti import modes, statusline

    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    modes.set_manager("s1", True)
    sit("claude-sonnet-5-5", "high")
    first = statusline.render({"session_id": "s1"}, snapshot).split("\n")[0]
    assert "seat" in first and "seat✓" not in first       # managed, not yet checked
    manager.clear_recommendation("s1", manager.current_seat("s1"))
    assert "seat✓" in statusline.render({"session_id": "s1"}, snapshot).split("\n")[0]
    auto_seat.set_mode("shadow")
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    assert "seat" not in statusline.render({"session_id": "s1"}, snapshot).split("\n")[0]

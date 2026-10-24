"""When the session may change its own seat: up at once, down only with good reason.

`manager.advise` is exercised for real (band stubbed, the advisor off); only the
classifier is replaced, since what is pinned here is the policy that sits on its answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ruti import auto_seat, manager, prompt_context, quota

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
    shadow = auto_seat.plan("s1", LONG, classify=guess("debug", 0.5, 0.9))
    assert shadow["direction"] == "up" and shadow["action"].get("effort")
    assert shadow["apply"] is False
    auto_seat.set_mode("on")
    live = auto_seat.plan("s1", LONG, classify=guess("debug", 0.5, 0.9))
    assert live["apply"] is True and live["action"] == shadow["action"]


def test_a_move_down_waits_for_the_same_advice_twice_in_a_row():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    first = auto_seat.plan("s1", LONG, classify=guess())
    assert first["direction"] == "down" and first["apply"] is False
    assert "two prompts" in first["held"]
    second = auto_seat.plan("s1", LONG, classify=guess())
    assert second["apply"] is True and second["action"]


def test_a_model_switch_down_over_a_larger_context_needs_a_confident_easy_task():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high", tokens=100_000)
    for _ in range(2):
        unsure = auto_seat.plan("s1", LONG, classify=guess(confidence=0.75))
    assert unsure["action"] == {} and "confidence" in unsure["held"]
    for _ in range(2):
        hard = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.95))
    assert hard["action"] == {} and "difficulty" in hard["held"]


def test_a_model_switch_down_over_a_small_context_needs_only_a_fresh_classification():
    # Re-reading 10k tokens twice -- down, and back up if it was wrong -- costs little.
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high", tokens=10_000)
    for _ in range(2):
        out = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.75))
    assert out["direction"] == "down" and out["apply"] is True
    assert out["action"].get("model") == "sonnet"


def test_an_effort_only_move_down_needs_only_a_fresh_classification():
    # The prompt cache survives an effort change, so the size of the context is moot.
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high", tokens=400_000)
    first = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.75))
    assert first["apply"] is False and "two prompts" in first["held"]
    second = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.75))
    assert second["apply"] is True and second["action"] == {"effort": "low"}


def test_down_hold_is_strict_only_for_a_model_switch_over_a_larger_context():
    fresh_only = "a move down needs a fresh classification"
    assert auto_seat.down_hold(False, 0.9, 0.1) == fresh_only
    assert auto_seat.down_hold(False, 0.9, 0.1, model_change=True) == fresh_only
    # An effort-only move down is taken even on a task reused from the last prompt.
    assert auto_seat.down_hold(False, None, 0.9, model_change=False) is None
    assert auto_seat.down_hold(True, None, 0.1, model_change=True) == fresh_only
    assert auto_seat.down_hold(True, 0.7, 0.9, model_change=False) is None
    assert auto_seat.down_hold(True, 0.7, 0.9, tokens=auto_seat.DOWN_CHEAP_TOKENS - 1) is None
    big = auto_seat.DOWN_CHEAP_TOKENS
    assert "confidence" in auto_seat.down_hold(True, 0.7, 0.1, tokens=big)
    assert "difficulty" in auto_seat.down_hold(True, 0.9, 0.5, tokens=big)
    assert auto_seat.down_hold(True, 0.9, 0.2, tokens=big) is None
    # An unknown context is priced as a large one.
    assert "difficulty" in auto_seat.down_hold(True, 0.9, 0.5)


def test_a_held_move_down_leaves_the_verdict_as_held():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high", tokens=100_000)
    for _ in range(2):
        out = auto_seat.plan("s1", LONG, classify=guess(confidence=0.75))
    assert out["apply"] is False
    assert manager.recommended_verdict("s1") == "held"


def test_a_task_reused_from_the_last_prompt_never_switches_the_model_down():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    manager.remember_task("s1", "boilerplate", 0.1)
    for _ in range(2):
        out = auto_seat.plan("s1", "yes, go ahead")
    assert out["task"]["source"] == "reused"
    assert "model" not in out["action"]


def test_a_reused_task_may_still_lower_the_effort():
    auto_seat.set_mode("on")
    sit("claude-sonnet-5-5", "medium")
    manager.remember_task("s1", "analyze", 0.27)
    for _ in range(2):
        out = auto_seat.plan("s1", "ok")
    assert out["task"]["source"] == "reused"
    assert out["apply"] is True and out["action"] == {"effort": "low"}


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
    out = auto_seat.plan("s1", "y" * prompt_context.MIN_PROMPT_CHARS, classify=guess("debug", 0.5, 0.9))
    assert out["task"]["source"] == "fresh"
    shorter = auto_seat.plan("s1", "y" * (prompt_context.MIN_PROMPT_CHARS - 1),
                             classify=guess("debug", 0.5, 0.9))
    assert shorter["task"]["source"] == "reused"
    assert prompt_context.MIN_PROMPT_CHARS == 60


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


def _line_one(monkeypatch, session="s1"):
    import time

    from ruti import statusline

    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    out = statusline.render({"session_id": session}, snapshot).split("\n")[0]
    return __import__("re").sub(r"\x1b\[[0-9;]*m", "", out), out


def test_a_move_the_manager_makes_itself_is_an_arrow_and_one_that_waits_is_yellow(monkeypatch):
    from ruti import modes

    modes.set_manager("s1", True)
    # Effort only: applied by the mod, cyan `->`.
    sit("claude-sonnet-5-5", "low")
    auto_seat.plan("s1", LONG, classify=guess("debug", 0.5, 0.9))
    plain, raw = _line_one(monkeypatch)
    assert "->sonnet/" in plain and "\x1b[36m->" in raw and "⇢" not in plain
    assert manager.recommended_verdict("s1") == "now"

    # A model switch with a big context: held back, yellow `⇢ ... /compact`.
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.ORANGE))  # no Opus here
    sit("claude-opus-5-5", "high", tokens=400_000)
    auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.9))
    plain, raw = _line_one(monkeypatch)
    assert manager.recommended_verdict("s1") == "boundary"
    assert "⇢" in plain and "/compact" in plain and "\x1b[33m⇢" in raw and "->" not in plain


def test_without_auto_seat_every_pending_move_is_yellow(monkeypatch):
    from ruti import modes

    modes.set_manager("s1", True)
    auto_seat.set_mode("shadow")
    sit("claude-sonnet-5-5", "low")
    auto_seat.plan("s1", LONG, classify=guess("debug", 0.5, 0.9))
    plain, raw = _line_one(monkeypatch)
    assert "⇢sonnet/" in plain and "/compact" not in plain and "\x1b[33m⇢" in raw


def test_the_context_a_model_switch_is_free_below_is_150k():
    assert manager.SWITCH_FREE_TOKENS == 150_000


def test_seat_plan_reads_stdin_as_utf8(monkeypatch):
    # The mod pipes UTF-8; decoding it with the console code page would double every
    # Cyrillic letter and hand the classifier mojibake.
    from click.testing import CliRunner

    from ruti import cli

    seen = {}
    monkeypatch.setattr(cli.auto_seat_mod, "plan",
                        lambda session_id, prompt: seen.setdefault("prompt", prompt) and {})
    text = "проверка второго хода мода после смены модели"
    CliRunner(charset="cp1251").invoke(cli.main, ["seat", "plan", "--session", "s1"],
                       input=text.encode("utf-8"), catch_exceptions=False)
    assert seen["prompt"] == text


def test_the_effort_left_over_from_an_applied_model_switch_is_not_held_as_a_move_down():
    # The switch to opus was applied with `effort: low` to follow at the end of the turn;
    # if that did not land, the session sits on opus/high and the leftover reads as a
    # move down, which a reused classification would otherwise hold.
    auto_seat.set_mode("on")
    sit("claude-sonnet-5-5", "medium")
    first = auto_seat.plan("s1", LONG, classify=guess("review", 0.5, 0.9))
    assert first["apply"] is True and first["action"] == {"model": "opus", "effort": "low"}
    sit("claude-opus-5-5", "high")
    reused = lambda *_a, **_k: None  # noqa: E731 -- nothing fresh: the task is reused
    second = auto_seat.plan("s1", "ok", classify=reused)
    assert second["held"] is None and second["completing"] is True
    assert second["action"] == {"effort": "low"} and second["apply"] is True


def test_an_unrelated_move_down_is_still_held_after_a_switch_was_applied():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    auto_seat._mark_applied("s1", "opus/low", 0.0)  # long expired
    out = auto_seat.plan("s1", LONG, classify=guess(), now=10_000.0)
    assert out["action"] == {} and "completing" not in out


def test_a_move_held_for_want_of_a_second_prompt_is_yellow_not_cyan():
    auto_seat.set_mode("on")
    sit("claude-opus-5-5", "high")
    out = auto_seat.plan("s1", LONG, classify=guess("implement", 0.5, 0.75))
    assert out["apply"] is False and "two prompts" in out["held"]
    assert manager.recommended_verdict("s1") == "held"


def test_cache_state_reports_the_hit_share_and_counts_the_hour_from_the_last_request():
    window = {"current_usage": {"input_tokens": 2, "cache_creation_input_tokens": 700,
                                "cache_read_input_tokens": 99_300}}
    manager.record_seat("s1", {"model": {"id": "claude-sonnet-5-5"}, "context_window": window})
    hit, left = manager.cache_state("s1", window)
    assert hit == 99 and 3590 < left <= 3600
    # The same usage on a later repaint is the same request: the clock keeps running.
    seen = manager._load()["s1"]["cache_at"]
    manager.record_seat("s1", {"model": {"id": "claude-sonnet-5-5"}, "context_window": window})
    assert manager._load()["s1"]["cache_at"] == seen
    assert manager.cache_state("s1", window, now=seen + 4000)[1] == 0.0
    assert manager.cache_state("s1", {}) is None


def test_hard_work_asks_for_more_effort_while_the_window_allows_it():
    def effort(band, difficulty):
        manager.quota.load = lambda: Snap(band)
        return manager.advise(kind="implement", difficulty=difficulty, session_id="s1").best.seat

    assert effort(quota.GREEN, 0.5).effort == "medium"
    assert effort(quota.GREEN, 0.75).label() == "opus/high"
    assert effort(quota.GREEN, 0.9).label() == "opus/xhigh"
    assert effort(quota.YELLOW, 0.8).label() == "opus/high"
    # ORANGE gets no Opus (wait mode off): the best it can do is Sonnet at high.
    assert effort(quota.ORANGE, 0.9).label() == "sonnet/high"


def test_eff_plus_raises_every_recommended_effort_by_one_level(monkeypatch):
    from ruti import modes

    def seat(band, difficulty, session="s1"):
        monkeypatch.setattr(manager.quota, "load", lambda: Snap(band))
        return manager.advise(kind="implement", difficulty=difficulty,
                              session_id=session).best.seat.label()

    assert seat(quota.GREEN, 0.5) == "opus/medium"
    modes.set_effplus("s1", True)
    assert seat(quota.GREEN, 0.5) == "opus/high"
    assert seat(quota.GREEN, 0.75) == "opus/xhigh"
    assert seat(quota.GREEN, 0.9) == "opus/max"
    # A seat the band does not allow is not bumped into: ORANGE caps Sonnet at high, and
    # with no Opus (wait mode off) nothing can replace it.
    assert seat(quota.ORANGE, 0.65) == "sonnet/high"
    # Where Opus is allowed, eff+ takes it over Sonnet: YELLOW caps Opus at high.
    assert seat(quota.YELLOW, 0.65) == "opus/high"
    # Only a Sonnet that is already at high: below that eff+ just adds a level.
    assert seat(quota.YELLOW, 0.5) == "sonnet/high"
    assert seat(quota.GREEN, 0.5, session="other") == "opus/medium"


def test_a_project_floor_moves_a_sonnet_seat_up_and_is_journalled(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".ruti").mkdir(parents=True)
    (project / ".ruti" / "seat-floor.json").write_text(json.dumps(
        {"floors": [{"model": "opus", "why": "migrations are Opus work",
                     "match": ["supabase/migrations"]}]}), encoding="utf-8")
    journal = tmp_path / "auto-seat.jsonl"
    monkeypatch.setattr(auto_seat, "_journal_file", lambda: journal)
    auto_seat.set_mode("on")
    manager.record_seat("s1", {**seated("claude-sonnet-5-5", "medium", 10_000),
                               "cwd": str(project)})
    # Classified as plain implementation work, which on its own is a Sonnet seat.
    out = auto_seat.plan("s1", LONG + " apply supabase/migrations/0082_x.sql",
                         classify=guess("implement", 0.3, 0.9))
    assert out["apply"] is True and out["action"]["model"] == "opus"
    assert out["floor"] == "opus: migrations are Opus work"
    row = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert row["classifier"]["kind"] == "implement"
    assert row["floor"] == "opus: migrations are Opus work"
    assert "context_tail" in row


def test_a_project_floor_judges_a_prompt_nothing_classified(tmp_path, monkeypatch):
    # The prompt hook falls back on the floor when the classifier is unsure and no task
    # is reused; the mod must too, or the hook recommends a seat the mod never applies.
    project = tmp_path / "proj"
    (project / ".ruti").mkdir(parents=True)
    (project / ".ruti" / "seat-floor.json").write_text(json.dumps(
        {"floors": [{"model": "opus", "why": "migrations are Opus work",
                     "match": ["supabase/migrations"]}]}), encoding="utf-8")
    monkeypatch.setattr(auto_seat, "_journal_file", lambda: tmp_path / "auto-seat.jsonl")
    auto_seat.set_mode("on")
    manager.record_seat("s2", {**seated("claude-sonnet-5-5", "medium", 10_000),
                               "cwd": str(project)})
    out = auto_seat.plan("s2", LONG + " apply supabase/migrations/0082_x.sql",
                         classify=guess("implement", 0.3, 0.4))
    assert out["held"] != "no task to judge: nothing classified and nothing reused"
    assert out["task"]["source"] == "floor"
    assert out["apply"] is True and out["action"]["model"] == "opus"

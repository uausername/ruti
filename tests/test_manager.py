"""Which seat the manager should be in, per band.

`advise` reads only `snapshot.band`, so a stand-in with one attribute is enough and
keeps every test off the clock and off disk. What is being pinned here is the policy --
which seats a band permits, and when moving off the current one is worth it -- because
that is what a change to any of the constants silently reverses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ruti import flow, manager, modes, quota, sessions
from ruti.hooks import user_prompt_submit as ups


class Snap:
    """`advise` reads one attribute off a snapshot; that is the whole contract."""

    def __init__(self, band):
        self.band = band


def advise(band, **kwargs):
    kwargs.setdefault("kind", "implement")
    kwargs.setdefault("difficulty", None)
    kwargs.setdefault("session_id", None)
    kwargs.setdefault("current", None)
    kwargs.setdefault("context_tokens", 0)
    return manager.advise(snapshot=Snap(band), **kwargs)


@pytest.fixture(autouse=True)
def no_advisor(tmp_path, monkeypatch):
    """The real user settings may name an advisor; no test should depend on that."""
    monkeypatch.setattr(manager, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    monkeypatch.delenv(manager.ADVISOR_ENV, raising=False)
    monkeypatch.delenv(manager.ADVISOR_KILL_ENV, raising=False)


def with_advisor(tmp_path, model="opus", flag="1"):
    import json
    env = {manager.ADVISOR_ENV: flag} if flag else {}
    (tmp_path / "settings.json").write_text(
        json.dumps({"advisorModel": model, "env": env}), encoding="utf-8")


def by_label(advice):
    return {entry.seat.label(): entry for entry in advice.ranked}


PAYLOAD = {
    "model": {"id": "claude-sonnet-5-5", "display_name": "Sonnet 5.5"},
    "effort": {"level": "medium"},
    "context_window": {
        "context_window_size": 1_000_000,
        "used_percentage": 5,
        "current_usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                          "cache_read_input_tokens": 40000},
    },
}


def seated(model_id, effort, tokens):
    """A status-line payload for a session on `model_id`/`effort` carrying `tokens`.

    `current_usage` is what the hook reads, so the window size is the only thing left to
    override -- which is why the sizes below are set on it and not on the percentage.
    """
    return {**PAYLOAD, "model": {"id": model_id}, "effort": {"level": effort},
            "context_window": {**PAYLOAD["context_window"],
                               "current_usage": {"input_tokens": tokens}}}


# ------------------------------------------------------------------ the ranking


def test_security_work_in_green_is_an_opus_seat_at_high_effort_or_above():
    advice = advise(quota.GREEN, kind="security", difficulty=None)
    assert advice.best is not None
    assert advice.best.seat.model == "opus"
    assert advice.best.seat.effort in ("high", "xhigh", "max")
    entries = by_label(advice)
    blocked = [label for label, entry in entries.items()
               if entry.seat.model in ("sonnet", "haiku")]
    assert blocked and all(not entries[label].eligible for label in blocked)


def test_boilerplate_in_yellow_lands_on_sonnet():
    assert advise(quota.YELLOW, kind="boilerplate").best.seat.model == "sonnet"


def test_ordinary_implementation_in_yellow_is_sonnet_at_medium():
    advice = advise(quota.YELLOW, kind="implement")
    assert advice.best.seat == manager.Seat("sonnet", "medium")


def test_yellow_caps_opus_at_high():
    entries = by_label(advise(quota.YELLOW))
    assert not entries["opus/xhigh"].eligible
    assert not entries["opus/max"].eligible
    assert entries["opus/high"].eligible


def test_orange_without_wait_mode_has_no_opus_manager():
    advice = advise(quota.ORANGE, session_id=None)
    assert all(not entry.eligible for entry in advice.ranked
               if entry.seat.model == "opus")


def test_orange_with_wait_mode_on_permits_opus_up_to_medium():
    modes.set_wait("s1", True)
    entries = by_label(advise(quota.ORANGE, session_id="s1"))
    assert entries["opus/low"].eligible
    assert entries["opus/medium"].eligible
    assert not entries["opus/high"].eligible


def test_red_leaves_only_coordination_seats():
    advice = advise(quota.RED, kind="implement")
    best = advice.best
    assert best.seat.model == "haiku" or (best.seat.model == "sonnet"
                                           and best.seat.effort in ("low", "medium"))
    assert not by_label(advice)["sonnet/high"].eligible


def test_critical_permits_nothing_at_all():
    advice = advise(quota.CRITICAL, kind="implement")
    assert advice.best is None
    assert advice.switch["verdict"] == "none"


def test_fable_needs_opting_in():
    default = by_label(advise(quota.GREEN, kind="implement"))
    assert not default["fable/high"].eligible
    allowed = by_label(advise(quota.GREEN, kind="implement", allow_fable=True))
    assert allowed["fable/high"].eligible


# ------------------------------------------------------------------ the verdicts


def test_verdicts_against_the_best_seat_in_yellow():
    seen = {
        "same seat": (manager.Seat("sonnet", "medium"), 100_000),
        "effort only": (manager.Seat("sonnet", "high"), 100_000),
        "cheap switch": (manager.Seat("opus", "high"), 10_000),
    }
    verdicts = {name: advise(quota.YELLOW, kind="implement", current=seat,
                             context_tokens=tokens).switch["verdict"]
                for name, (seat, tokens) in seen.items()}
    assert verdicts == {"same seat": "stay", "effort only": "now", "cheap switch": "now"}


def test_a_switch_that_survives_the_effort_preference_is_deferred_to_a_boundary():
    # The YELLOW/opus/100k case this used to cover is an effort change on opus now, not
    # a switch: opus/low is within the margin a model switch has to beat. The switch
    # survives where the current model has no seat the band permits for this task.
    advice = advise(quota.ORANGE, kind="implement", current=manager.Seat("opus", "high"),
                    context_tokens=100_000)
    assert advice.best.seat.model == "sonnet"
    assert advice.switch["verdict"] == "boundary"



def test_an_underpowered_seat_is_told_to_move_now():
    advice = advise(quota.GREEN, kind="debug", current=manager.Seat("haiku", None),
                    context_tokens=100_000)
    assert advice.switch["verdict"] == "now"
    assert "underpowered" in advice.switch["reason"]


# ------------------------------------------------------------------ effort first


def test_a_big_context_keeps_the_model_and_moves_the_effort():
    # sonnet/medium outscores every Opus seat here, but switching to it re-reads a
    # quarter of a million tokens uncached, so effort is what moves.
    advice = advise(quota.YELLOW, kind="implement", current=manager.Seat("opus", "xhigh"),
                    context_tokens=250_000)
    assert advice.best.seat.model == "opus"
    assert advice.best.seat.effort != "xhigh"
    assert advice.switch["verdict"] == "now"
    assert "cache is kept" in advice.switch["reason"]
    # The ranking itself is untouched: still pure score order, and the seat it ranked
    # first is the one the reason names as the alternative.
    assert advice.ranked[0].seat.model == "sonnet"
    assert "sonnet/medium" in advice.switch["reason"]


def test_a_small_context_makes_the_switch_cheap_again():
    advice = advise(quota.YELLOW, kind="implement", current=manager.Seat("opus", "xhigh"),
                    context_tokens=10_000)
    assert advice.best.seat.model == "sonnet"


def test_an_underpowered_model_still_switches_under_a_big_context():
    advice = advise(quota.GREEN, kind="debug", current=manager.Seat("haiku", None),
                    context_tokens=250_000)
    assert advice.best.seat.model in ("opus", "sonnet")
    assert advice.switch["verdict"] == "now"


def test_a_blocked_model_still_switches_under_a_big_context():
    advice = advise(quota.ORANGE, kind="implement",
                    current=manager.Seat("opus", "high"), context_tokens=250_000)
    assert advice.best.seat.model != "opus"


@pytest.mark.parametrize("tokens,margin", [(None, 0.0), (10_000, 0.0), (250_000, 0.18)])
def test_the_switch_margin_prices_the_context(tokens, margin):
    assert manager.switch_margin(tokens) == pytest.approx(margin)


# ------------------------------------------------------------------ the record


@pytest.mark.parametrize("given,expected", [
    ("claude-sonnet-5-5", "sonnet"),
    ("Opus 5.5", "opus"),
    (None, None),
    ("gpt-5", None),
])
def test_model_alias(given, expected):
    assert manager.model_alias(given) == expected


def test_a_recorded_seat_reads_back():
    manager.record_seat("s1", PAYLOAD)
    assert manager.current_seat("s1") == manager.Seat("sonnet", "medium")
    assert manager.context_tokens("s1") == 41010


def test_a_hostile_payload_records_nothing_and_does_not_raise():
    manager.record_seat("s1", {"model": 5})
    manager.record_seat(None, {})
    assert manager.current_seat("s1") is None
    assert manager.context_tokens("s1") is None


def test_a_repaint_leaves_the_recommendation_alone():
    manager.remember_recommendation("s1", manager.Seat("sonnet", "medium"))
    manager.record_seat("s1", PAYLOAD)
    assert manager.recommended("s1") == "sonnet/medium"


# ------------------------------------------------------------------ the task record


def test_a_classified_task_is_reused_by_the_next_short_prompt():
    manager.remember_task("s1", "implement", 0.5)
    assert manager.last_task("s1") == ("implement", 0.5)
    # A repaint and a new recommendation are other callers' writes: neither may drop it.
    manager.record_seat("s1", PAYLOAD)
    manager.remember_recommendation("s1", manager.Seat("sonnet", "medium"))
    assert manager.last_task("s1") == ("implement", 0.5)


def test_a_task_too_old_to_be_this_one_is_not_reused():
    manager.remember_task("s1", "implement", 0.5)
    assert manager.last_task("s1", max_age=-1.0) is None
    assert manager.last_task("other") is None


def test_remembering_a_task_never_raises():
    manager.remember_task(None, "implement", 0.5)
    manager.remember_task("s1", "implement", "not a number")
    assert manager.last_task("s1") is None


# ------------------------------------------------------------------ the hint


def test_the_hint_is_off_without_manager_mode():
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is None


def test_the_hint_names_the_seat_shows_it_once_and_then_repeats(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    note, shown = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    assert shown is not None
    assert "/model sonnet" in shown
    # The recommendation is recomputed on every prompt; only the user-facing half of it
    # is debounced, so the model is still told, and the status line still points at it.
    repeat = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    assert repeat is not None
    assert repeat[0] == note
    assert repeat[1] is None
    assert manager.recommended("s1") == "sonnet/medium"


def test_the_hint_stops_and_clears_the_arrow_once_the_seat_matches(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is not None
    manager.record_seat("s1", PAYLOAD)  # the user typed both commands
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is None
    assert manager.recommended("s1") is None
    # ...and the match is recorded, so the status line can say it was checked.
    assert manager.matched("s1") == "sonnet/medium"


def test_a_new_recommendation_supersedes_the_match_and_a_repaint_keeps_it(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", PAYLOAD)
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is None
    assert manager.matched("s1") == "sonnet/medium"
    manager.record_seat("s1", PAYLOAD)
    assert manager.matched("s1") == "sonnet/medium"
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is not None
    assert manager.matched("s1") is None


def test_the_status_line_marks_a_matched_seat_and_only_while_it_is_the_seat(monkeypatch):
    from ruti import statusline

    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    modes.set_manager("s1", True)
    manager.record_seat("s1", PAYLOAD)
    manager.prompt_hint("s1", "implement", 0.9, 0.5)
    assert "seat✓" in statusline.render({"session_id": "s1"}, snapshot)
    # The user moves off it: the mark must not claim a check that was for another seat.
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    assert "seat✓" not in statusline.render({"session_id": "s1"}, snapshot)


def test_the_commands_are_on_separate_lines_and_the_model_is_told_to_say_them(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", seated("claude-opus-5-5", "high", 10_000))
    note, shown = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    lines = [line.strip() for line in shown.splitlines()]
    # Pasted on one line they read as a model called "sonnet /effort low".
    assert "/model sonnet" in lines and any(line.startswith("/effort ") for line in lines)
    assert not any("/model" in line and "/effort" in line for line in lines)
    # Last line, not first: the answer comes before the recommendation or neither is read.
    assert "LAST line" in note
    assert "nothing after it" in note


def test_an_effort_only_note_asks_for_the_effort_alone(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", seated("claude-opus-5-5", "xhigh", 250_000))
    note, shown = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    assert "LAST line" in note
    assert "/effort low" in note
    # The model is already opus: asking for it would throw the cache away for nothing.
    assert "/model" not in note
    assert "/model" not in shown


# ------------------------------------------------------------------ the seat


def test_a_seat_without_an_effort_says_only_the_model():
    assert manager.Seat("haiku", None).cli_args() == ["--model", "haiku"]
    assert "--effort" not in manager.Seat("haiku", None).cli_args()


def test_a_seat_with_an_effort_names_both_commands():
    assert manager.Seat("opus", "high").commands() == ["/model opus", "/effort high"]


def test_asking_for_the_model_already_on_is_left_out():
    # `/model` for the model the session is on is a cache-busting no-op.
    assert manager.Seat("opus", "low").commands(manager.Seat("opus", "xhigh")) == [
        "/effort low"]
    # Already there: nothing to type.
    assert manager.Seat("opus", "low").commands(manager.Seat("opus", "low")) == []
    # Haiku takes no effort, so there is never an effort command to leave out.
    assert manager.Seat("haiku", None).commands(manager.Seat("haiku", None)) == []
    assert manager.Seat("sonnet", "low").commands(manager.Seat("opus", "high")) == [
        "/model sonnet", "/effort low"]


def test_the_launcher_carries_the_seat_into_the_next_session():
    script = flow.launcher_script(Path("h.md"), "C:/x", None, "claude",
                                  seat_args=["--model", "sonnet", "--effort", "medium"])
    assert "'--model' 'sonnet' '--effort' 'medium'" in script


# ------------------------------------------------------------------ the hook


class Guess:
    kind = "implement"
    kind_confidence = 0.9
    difficulty = 0.5
    trivial = False
    trivial_probability = 0.1


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "current_session_id", lambda: "s1")
    monkeypatch.setattr(ups, "MEMO_FILE", tmp_path / "memo.json")
    monkeypatch.setattr(quota, "load", lambda: quota.Quota(
        quota.Window(20.0, time.time() + 3600), None, time.time()))
    monkeypatch.setattr(ups, "_classify_prompt", lambda *_a, **_k: Guess())
    modes.set_manager("s1", True)
    return ups.build_output


def test_the_hook_shows_the_seat_hint_to_the_user(hook, monkeypatch):
    monkeypatch.setattr(ups.manager, "prompt_hint",
                        lambda *_a, **_k: ("note", "shown to user"))
    output = hook("x" * 200, None)
    assert output["systemMessage"] == "shown to user"
    # `suppressOutput` hid the message in a live CLI session.
    assert output["suppressOutput"] is False


def test_the_hook_says_nothing_when_there_is_no_hint(hook, monkeypatch):
    monkeypatch.setattr(ups.manager, "prompt_hint", lambda *_a, **_k: None)
    output = hook("x" * 200, None)
    assert "systemMessage" not in output
    assert output["suppressOutput"] is True


def test_the_hook_keeps_the_task_across_a_prompt_too_short_to_classify(hook, monkeypatch):
    # "yes, go ahead" is not a task to classify, but it is the same work as the prompt
    # above it -- so the seat advice keeps coming, from the task already stored.
    ups.manager.remember_task("s1", "implement", 0.5)
    monkeypatch.setattr(ups, "_classify_prompt", lambda *_a, **_k: None)
    seen: list[str | None] = []
    monkeypatch.setattr(ups.manager, "prompt_hint",
                        lambda _s, kind, *_a: seen.append(kind) or ("NOTE", None))
    output = hook("yes", None)
    assert seen == ["implement"]
    assert "NOTE" in output["hookSpecificOutput"]["additionalContext"]
    # Nothing new to show the user this time, so no visible message.
    assert "systemMessage" not in output
    assert output["suppressOutput"] is True


def test_the_hook_says_nothing_when_there_is_no_task_to_reuse(hook, monkeypatch):
    monkeypatch.setattr(ups, "_classify_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(ups.manager, "prompt_hint", lambda *_a, **_k: ("NOTE", "shown"))
    output = hook("yes", None)
    assert "systemMessage" not in output


def test_an_opus_advisor_lowers_what_implementation_requires(tmp_path):
    plain = advise(quota.GREEN).task["required"]
    with_advisor(tmp_path)
    advised = advise(quota.GREEN)
    assert advised.task["advisor"] == "opus"
    assert advised.task["required"] == pytest.approx(plain - manager.ADVISOR_RELIEF)


@pytest.mark.parametrize("kind", ["security", "review", "analyze"])
def test_an_advisor_gives_no_relief_outside_its_kinds(tmp_path, kind):
    plain = advise(quota.GREEN, kind=kind).task["required"]
    with_advisor(tmp_path)
    assert advise(quota.GREEN, kind=kind).task["required"] == plain


@pytest.mark.parametrize("band", [quota.ORANGE, quota.RED])
def test_an_advisor_gives_no_relief_past_yellow(tmp_path, band):
    plain = advise(band).task["required"]
    with_advisor(tmp_path)
    assert advise(band).task["advisor_relief"] == 0.0
    assert advise(band).task["required"] == plain


@pytest.mark.parametrize("model,flag", [("sonnet", "1"), ("opus", ""), ("opus", "0")])
def test_a_weak_or_disabled_advisor_does_not_count(tmp_path, model, flag):
    with_advisor(tmp_path, model=model, flag=flag)
    assert manager.advisor_model() is None


def test_the_kill_switch_wins(tmp_path, monkeypatch):
    with_advisor(tmp_path)
    monkeypatch.setenv(manager.ADVISOR_KILL_ENV, "1")
    assert manager.advisor_model() is None

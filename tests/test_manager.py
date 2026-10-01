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
    best = manager.Seat("sonnet", "medium")
    seen = {
        "same seat": (manager.Seat("sonnet", "medium"), 100_000),
        "effort only": (manager.Seat("sonnet", "high"), 100_000),
        "over budget": (manager.Seat("opus", "high"), 100_000),
        "cheap switch": (manager.Seat("opus", "high"), 10_000),
    }
    verdicts = {name: advise(quota.YELLOW, kind="implement", current=seat,
                             context_tokens=tokens).switch["verdict"]
               for name, (seat, tokens) in seen.items()}
    assert verdicts == {"same seat": "stay", "effort only": "now",
                        "over budget": "boundary", "cheap switch": "now"}


def test_an_underpowered_seat_is_told_to_move_now():
    advice = advise(quota.GREEN, kind="debug", current=manager.Seat("haiku", None),
                    context_tokens=100_000)
    assert advice.switch["verdict"] == "now"
    assert "underpowered" in advice.switch["reason"]


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


# ------------------------------------------------------------------ the hint


def test_the_hint_is_off_without_manager_mode():
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is None


def test_the_hint_names_the_seat_and_then_debounces(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", {**PAYLOAD, "model": {"id": "claude-opus-5-5"},
                               "effort": {"level": "high"}})
    hint = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    assert hint is not None
    assert "/model sonnet" in hint[1]
    assert manager.prompt_hint("s1", "implement", 0.9, 0.5) is None


def test_the_commands_are_on_separate_lines_and_the_model_is_told_to_say_them(monkeypatch):
    monkeypatch.setattr(manager.quota, "load", lambda: Snap(quota.YELLOW))
    modes.set_manager("s1", True)
    manager.record_seat("s1", {**PAYLOAD, "model": {"id": "claude-opus-5-5"},
                               "effort": {"level": "high"}})
    note, shown = manager.prompt_hint("s1", "implement", 0.9, 0.5)
    lines = [line.strip() for line in shown.splitlines()]
    # Pasted on one line they read as a model called "sonnet /effort low".
    assert "/model sonnet" in lines and any(line.startswith("/effort ") for line in lines)
    assert not any("/model" in line and "/effort" in line for line in lines)
    assert "Open your reply with this recommendation" in note


# ------------------------------------------------------------------ the seat


def test_a_seat_without_an_effort_says_only_the_model():
    assert manager.Seat("haiku", None).cli_args() == ["--model", "haiku"]
    assert "--effort" not in manager.Seat("haiku", None).cli_args()


def test_a_seat_with_an_effort_names_both_commands():
    assert manager.Seat("opus", "high").commands() == ["/model opus", "/effort high"]


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

"""The context-window reading, from the status line that sees it to the hook that says it.

The failure this closes: the global CLAUDE.md asks the manager to wrap up at 50% context,
and the manager had no number to act on -- only the status line had it, and the model
never sees the status line.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time

import pytest

from ruti import config, context_watch, quota, sessions, statusline
from ruti.hooks import user_prompt_submit

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def a_quota(used=20.0):
    return quota.Quota(
        five_hour=quota.Window(used, time.time() + 3600),
        seven_day=None,
        captured_at=time.time(),
    )


# ------------------------------------------------------------------------- store


def test_a_recorded_reading_reads_back():
    context_watch.record("s1", 42.0)
    assert context_watch.used("s1") == 42.0


@pytest.mark.parametrize("sid, value", [(None, 42.0), ("s1", None), ("s1", "junk")])
def test_nothing_usable_records_nothing(sid, value):
    context_watch.record(sid, value)
    assert context_watch.used("s1") is None


def test_one_sessions_reading_is_not_anothers():
    context_watch.record("s1", 70)
    assert context_watch.used("s2") is None
    assert context_watch.warning("s2") == ""


def test_old_sessions_are_pruned_on_the_next_write():
    stale = time.time() - context_watch.MAX_AGE_SECONDS - 60
    config.write_json(context_watch.CONTEXT_FILE, {"old": {"used_percentage": 90, "at": stale}})
    context_watch.record("s1", 10)
    assert context_watch.used("old") is None
    assert context_watch.used("s1") == 10


def test_a_corrupt_file_reads_as_unknown_and_is_overwritten():
    config.write_json(context_watch.CONTEXT_FILE, [1, 2])
    assert context_watch.used("s1") is None
    assert context_watch.warning("s1") == ""
    context_watch.record("s1", 10)
    assert context_watch.used("s1") == 10


def test_an_unchanged_reading_does_not_rewrite_the_file():
    context_watch.record("s1", 30)
    first = config.read_json(context_watch.CONTEXT_FILE)["s1"]["at"]
    context_watch.record("s1", 30)
    assert config.read_json(context_watch.CONTEXT_FILE)["s1"]["at"] == first


# ----------------------------------------------------------------------- warning


def test_no_warning_just_under_the_line():
    context_watch.record("s1", 49.9)
    assert context_watch.warning("s1") == ""


@pytest.mark.parametrize("pct", [50.0, 73])
def test_a_warning_at_and_past_the_line(pct):
    context_watch.record("s1", pct)
    note = context_watch.warning("s1")
    assert f"{pct:.0f}% of this conversation's context window" in note
    assert "/compact" in note


# ------------------------------------------------------------------- status line


@pytest.fixture
def no_live_facts(monkeypatch):
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None,
    })
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))


@pytest.mark.parametrize("pct, colour", [(40, "32"), (55, "33"), (90, "31")])
def test_the_ctx_segment_turns_amber_at_the_same_line(no_live_facts, pct, colour):
    line = statusline.render({"context_window": {"used_percentage": pct}}, a_quota())
    assert f"\x1b[{colour}m{pct}% ctx" in line


def test_the_status_line_records_the_reading(monkeypatch):
    payload = {"session_id": "s1", "context_window": {"used_percentage": 61}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    # The real `quota.json` is bound at import time and must not be written by a test.
    monkeypatch.setattr(quota, "capture", lambda _payload: quota.Quota(None, None, 0.0))
    monkeypatch.setattr(statusline, "render", lambda _payload, _snap: "x")
    assert statusline.main() == 0
    assert context_watch.used("s1") == 61


@pytest.mark.parametrize("raw", ["[1, 2]", '{"session_id": "s1", "context_window": "junk"}'])
def test_a_hostile_payload_does_not_break_the_status_line(monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(quota, "capture", lambda _payload: quota.Quota(None, None, 0.0))
    monkeypatch.setattr(statusline, "render", lambda _payload, _snap: "x")
    assert statusline.main() == 0
    assert capsys.readouterr().out == "x\n"


# ---------------------------------------------------------------------- the hook


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "current_session_id", lambda: "s1")
    monkeypatch.setattr(user_prompt_submit, "MEMO_FILE", tmp_path / "memo.json")
    monkeypatch.setattr(quota, "load", a_quota)
    return lambda: ANSI.sub("", user_prompt_submit.build_context("")[0])


def test_the_hook_is_silent_under_the_line(hook):
    context_watch.record("s1", 30)
    assert "ruti context" not in hook()


def test_the_hook_says_the_number_past_the_line(hook):
    context_watch.record("s1", 64)
    assert "64% of this conversation's context window" in hook()


def test_the_hook_still_says_it_when_ruti_is_off(hook):
    sessions.set_disabled("s1", True)
    context_watch.record("s1", 64)
    context = hook()
    assert "OFF for this session" in context
    assert "64% of this conversation's context window" in context

"""A reply is judged with the message it answers, and judged once per prompt."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ruti import jev, prompt_context


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks, sidechain=False):
    record = {"type": "assistant", "message": {"content": list(blocks)}}
    if sidechain:
        record["isSidechain"] = True
    return record


def text(value):
    return {"type": "text", "text": value}


def write(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")
    return str(path)


@pytest.fixture
def transcript(tmp_path):
    return write(tmp_path / "s1.jsonl", [
        user("first"),
        assistant(text("Proposal A"), {"type": "tool_use", "name": "Bash", "input": {}}),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        user("<command-name>/effort</command-name>"),
        assistant(text("subagent chatter"), sidechain=True),
        assistant(text("```py\nsecret_code()\n```\nShall I do X?\nRecommendation: `/effort low`")),
    ])


def test_the_previous_message_is_all_its_text_less_code_and_the_seat_line(transcript):
    reply = prompt_context.previous_reply(transcript, "да")
    assert "Proposal A" in reply and "Shall I do X?" in reply
    assert "[code omitted]" in reply and "secret_code" not in reply
    assert "Recommendation" not in reply
    assert "subagent chatter" not in reply and "ok" not in reply.split()


def test_the_message_is_the_same_once_the_prompt_itself_is_in_the_file(tmp_path, transcript):
    before = prompt_context.previous_reply(transcript, "да")
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(user("да"), ensure_ascii=False) + "\n")
    assert prompt_context.previous_reply(transcript, "да") == before


def test_no_transcript_no_message(tmp_path):
    assert prompt_context.previous_reply(str(tmp_path / "missing.jsonl"), "да") is None
    assert prompt_context.previous_reply(None, "да") is None


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, text, *, context=None, timeout=None):
        self.calls.append((text, context))
        return jev.Classification(kind="implement", kind_confidence=0.9, difficulty=0.5,
                                  difficulty_confidence=0.8, trivial=False,
                                  trivial_probability=0.2)


@pytest.fixture
def fake(monkeypatch):
    found = Fake()
    monkeypatch.setattr(jev, "classify", found)
    monkeypatch.setattr(jev, "configured", lambda *_a, **_k: True)
    return found


def test_a_short_reply_is_asked_with_the_message_and_only_once(transcript, fake):
    first = prompt_context.classify("s1", "да", transcript=transcript)
    again = prompt_context.classify("s1", "да", transcript=transcript)
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "да" and "Shall I do X?" in fake.calls[0][1]
    assert first.kind == again.kind == "implement"


def test_without_a_message_a_short_prompt_is_not_asked_and_a_long_one_is_alone(fake):
    assert prompt_context.classify("s1", "да") is None
    assert fake.calls == []
    prompt_context.classify("s1", "x" * prompt_context.MIN_PROMPT_CHARS)
    assert fake.calls == [("x" * prompt_context.MIN_PROMPT_CHARS, None)]


def test_a_callers_own_classifier_gets_its_own_answer(transcript, fake):
    mine = lambda *_a, **_k: SimpleNamespace(kind="debug")
    assert prompt_context.classify("s1", "да", transcript=transcript, classify=mine).kind == "debug"
    assert fake.calls == []


def test_the_classifier_switched_off_asks_nothing(transcript, fake, monkeypatch):
    monkeypatch.setattr(prompt_context.modes, "current", lambda _s: {"jev": False})
    assert prompt_context.classify("s1", "да", transcript=transcript) is None
    assert fake.calls == []


def test_the_transcript_given_once_is_found_again_and_otherwise_searched(tmp_path, monkeypatch):
    given = write(tmp_path / "given.jsonl", [user("hi")])
    assert prompt_context.transcript_for("s1", given) == given
    assert prompt_context.transcript_for("s1") == given

    root = tmp_path / "claude-config" / "projects" / "C--proj"
    root.mkdir(parents=True)
    found = write(root / "s2.jsonl", [user("hi")])
    assert prompt_context.transcript_for("s2") == found
    assert prompt_context.transcript_for("s3") is None

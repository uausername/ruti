"""Flow mode: the old session's name and Remote Control reach the next session.

Transcripts here are written by hand in the shapes Claude Code writes -- one JSON object
per line, plus a line that will not parse, because real ones have both -- and every launch
goes through a stubbed `spawn` and stubbed `shutil.which`, so nothing here starts a
process or writes outside tmp_path.
"""

from __future__ import annotations

import json

import pytest

from ruti import context_watch, flow, modes

SID = "s1"


def on(pct=None, sid=SID):
    modes.set_flow(sid, True)
    if pct is not None:
        context_watch.record(sid, pct)


class Spawn:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.fail and len(self.calls) == 1:
            raise self.fail


@pytest.fixture
def tools(monkeypatch):
    found = {"wt": "C:/bin/wt.exe", "pwsh": "C:/bin/pwsh.exe", "claude": "C:/bin/claude.exe"}
    monkeypatch.setattr(flow.shutil, "which", lambda name: found.get(name))
    return found


def transcript(tmp_path, *records, garbage=True):
    """A transcript: the records given, one per line, and a line Claude Code could not
    write as JSON. `ensure_ascii=False` so a Cyrillic title is on disk the way it was
    typed, not as escapes the reader has to be lucky about."""
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if garbage:
        lines.insert(1, "{this is not json")
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ the transcript


def test_a_renamed_session_outranks_the_auto_title(tmp_path):
    path = transcript(tmp_path,
                      {"type": "ai-title", "aiTitle": "Автоматический выбор",
                       "sessionId": SID},
                      {"type": "custom-title", "customTitle": "my-name", "sessionId": SID},
                      {"type": "ai-title", "aiTitle": "Auto again", "sessionId": SID},
                      {"type": "summary", "summary": "not a title record"})
    assert flow.transcript_facts(str(path)) == {"remote_control": False,
                                                "title": "my-name",
                                                "custom_title": "my-name"}


def test_the_last_custom_title_is_the_one(tmp_path):
    path = transcript(tmp_path, {"type": "custom-title", "customTitle": "first"},
                      {"type": "custom-title", "customTitle": "  second  "})
    assert flow.transcript_facts(str(path))["title"] == "second"


def test_only_an_auto_title_is_used_when_nothing_was_named(tmp_path):
    path = transcript(tmp_path, {"type": "ai-title", "aiTitle": "Auto title"})
    assert flow.transcript_facts(str(path)) == {"remote_control": False,
                                                "title": "Auto title",
                                                "custom_title": None}


def test_the_custom_title_is_kept_apart_from_the_generated_one(tmp_path):
    """`title` says what the session is called; `custom_title` says whether the user chose
    it, which is what decides whether it may be carried on to the next session."""
    named = transcript(tmp_path, {"type": "ai-title", "aiTitle": "Как выбрана модель"},
                       {"type": "custom-title", "customTitle": "fix flow names"})
    assert flow.transcript_facts(str(named))["custom_title"] == "fix flow names"
    generated = transcript(tmp_path, {"type": "ai-title", "aiTitle": "Как выбрана модель"})
    assert flow.transcript_facts(str(generated))["custom_title"] is None


def test_a_cyrillic_title_round_trips(tmp_path):
    path = transcript(tmp_path, {"type": "custom-title",
                                 "customTitle": "Разбор автоматических заголовков"})
    assert flow.transcript_facts(str(path))["title"] == "Разбор автоматических заголовков"


def test_a_bridge_session_alone_means_remote_control(tmp_path):
    path = transcript(tmp_path, {"type": "bridge-session", "sessionId": SID,
                                 "bridgeSessionId": "cse_1"},
                      {"type": "assistant", "message": {"role": "assistant"}})
    assert flow.transcript_facts(str(path))["remote_control"] is True


def test_a_disconnect_beats_the_markers_before_it(tmp_path):
    path = transcript(tmp_path,
                      {"type": "system", "subtype": "bridge_status",
                       "content": "/remote-control is active \u00b7 Continue here, phone"},
                      {"type": "bridge-session", "sessionId": SID, "bridgeSessionId": "cse_1"},
                      {"type": "system", "subtype": "bridge_status",
                       "content": "/remote-control disconnected"})
    assert flow.transcript_facts(str(path))["remote_control"] is False


def test_a_bridge_still_active_at_the_end(tmp_path):
    path = transcript(tmp_path,
                      {"type": "system", "subtype": "bridge_status",
                       "content": "/remote-control disconnected"},
                      {"type": "system", "subtype": "bridge_status",
                       "content": "/remote-control IS ACTIVE"})
    assert flow.transcript_facts(str(path))["remote_control"] is True


def test_an_ordinary_transcript_says_neither(tmp_path):
    path = transcript(tmp_path, {"type": "user", "message": {"role": "user"}},
                      {"type": "ai-title", "aiTitle": "Auto"})
    assert flow.transcript_facts(str(path))["remote_control"] is False


def test_nothing_to_read_is_not_an_error(tmp_path):
    empty = {"remote_control": False, "title": None, "custom_title": None}
    assert flow.transcript_facts(None) == empty
    assert flow.transcript_facts("") == empty
    assert flow.transcript_facts(str(tmp_path / "never-written.jsonl")) == empty
    assert flow.transcript_facts(str(tmp_path)) == empty  # a directory, not a transcript


# --------------------------------------------------------------------- the title


@pytest.mark.parametrize("text", [
    "Title: Fix X",
    "**Title:** Fix X",
    "## Title: Fix X",
    "- Title - Fix X",
    "# Handoff\n\nTitle – `Fix X`  \n",
    "> Title: \"Fix   X\"\n",
])
def test_the_title_line_of_the_handoff_is_read_in_any_markdown_shape(text):
    assert flow.handoff_title(text) == "Fix X"


def test_no_title_line_means_no_title_to_carry():
    assert flow.handoff_title("Goal: ship the parser\n") is None
    assert flow.handoff_title("") is None
    assert flow.handoff_title("Title:\n") is None  # the value has to be on the same line
    assert flow.handoff_title("Title\nGoal: ship the parser\n") is None


def test_a_word_that_merely_starts_with_title_is_not_one():
    assert flow.handoff_title("Titles are narrow, not the project.\n") is None
    assert flow.handoff_title("Title of the session: whatever it began as\n") is None


def test_the_next_session_is_named_for_the_work_the_handoff_names():
    assert flow.session_title("Title: fix flow names\nGoal: big project", 2) \
        == "fix flow names (flow 2/5)"


def test_the_goal_is_never_a_name():
    """The bug this chain replaced: a Goal is prose written as an instruction, and cut to
    a window title it ends mid-word ("...(answer in Russia… (flow 2/5)")."""
    goal = "Goal: Continue the HACCP task-graph work for the founder (answer in Russian)"
    assert flow.session_title(goal, 2) == "ruti flow (flow 2/5)"
    assert flow.session_title(goal, 2, folder="haccp") == "haccp (flow 2/5)"


def test_a_bad_legacy_title_is_skipped_rather_than_used():
    """A handoff written before there was a gate still gets a name -- just not that one."""
    text = "Title: Continue the HACCP task-graph work\nGoal: ship it"
    assert flow.session_title(text, 2, folder="haccp") == "haccp (flow 2/5)"
    assert flow.session_title("Title: oneword\nGoal: ship it", 2) == "ruti flow (flow 2/5)"


def test_a_long_handoff_title_is_cut_at_a_word():
    long = " ".join(["abcdefg"] * 8)  # 8 words, 63 characters: past the 60 a title gets
    assert flow.title_problem(long) is None
    result = flow.session_title(f"Title: {long}", 1)
    base = result[: -len(" (flow 1/5)")]
    assert base == " ".join(["abcdefg"] * 7)  # whole words only, no "abcd"
    assert len(base) <= flow.HANDOFF_TITLE_MAX


def test_the_folder_name_is_the_last_named_thing_before_the_generic_one():
    assert flow.session_title("Done: yesterday", 1, folder="haccp") == "haccp (flow 1/5)"
    assert flow.session_title("", 1, folder="  ") == "ruti flow (flow 1/5)"


def test_with_nothing_at_all_to_go_on():
    assert flow.session_title("", 3) == f"ruti flow (flow 3/{flow.MAX_HOPS})"
    assert flow.session_title(None, 3) == f"ruti flow (flow 3/{flow.MAX_HOPS})"


def test_a_name_the_user_chose_beats_the_handoff():
    assert flow.session_title("Title: fix flow names", 2,
                              custom="my own name") == "my own name (flow 2/5)"


def test_a_name_a_previous_hop_wrote_is_not_the_user_speaking():
    """`claude --name` wrote it on the way in, so it describes the work already handed off
    and must not stick to every hop after that."""
    assert flow.session_title("Title: fix flow names", 3,
                              custom="old work (flow 1/5)") == "fix flow names (flow 3/5)"


def test_a_very_long_name_is_cut_at_a_word_never_mid_word():
    long = " ".join(["alpha"] * 40)  # 199 characters
    result = flow.session_title("", 2, custom=long)
    base = result[: -len(" (flow 2/5)")]
    assert len(result) <= flow.TITLE_MAX
    assert result.endswith(f"(flow 2/{flow.MAX_HOPS})")
    assert base.endswith("\u2026")
    assert base[:-1] in long and long.startswith(base[:-1])  # whole words, nothing cut
    assert not base[:-1].endswith("alph")  # and no half of one


# ----------------------------------------------------------------- the launcher


def test_the_launcher_names_the_session_and_keeps_remote_control_on():
    script = flow.launcher_script(flow.FLOW_DIR / "h.md", "C:/x", None, "claude",
                                  name="Fix auth (flow 2/5)", remote_control=True)
    assert "'--name' 'Fix auth (flow 2/5)'" in script
    assert "'--remote-control' 'Fix auth (flow 2/5)'" in script
    # Both before the prompt, or claude would treat the prompt as their value.
    assert script.index("'--name'") < script.index("Continue the task")
    assert script.index("'--remote-control'") < script.index("Continue the task")


def test_a_remote_control_flag_is_never_bare():
    script = flow.launcher_script(flow.FLOW_DIR / "h.md", "C:/x", None, "claude",
                                  remote_control=True)
    assert "'--remote-control' 'ruti flow'" in script


def test_nothing_is_added_when_there_is_nothing_to_carry():
    script = flow.launcher_script(flow.FLOW_DIR / "h.md", "C:/x", None, "claude")
    assert "--remote-control" not in script and "--name" not in script


# ----------------------------------------------------------------------- stop


def test_stop_carries_both_across_from_the_transcript(tools, tmp_path):
    on()
    path = flow.write_handoff(SID, "Goal: ship the parser", "C:/work")
    # A custom title is the one kind of old name the handoff does not outrank: the user
    # chose it, so it is what the next session is called.
    transcript(tmp_path, {"type": "ai-title", "aiTitle": "Auto title"},
               {"type": "custom-title", "customTitle": "my-name"},
               {"type": "bridge-session", "sessionId": SID, "bridgeSessionId": "cse_1"})
    spawn = Spawn()
    result = flow.stop(SID, {"permission_mode": "auto", "cwd": "C:/work",
                             "transcript_path": str(tmp_path / "transcript.jsonl")},
                       spawn=spawn)
    hop = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["hop"]
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert "--remote-control" in script
    assert f"my-name (flow {hop}/{flow.MAX_HOPS})" in script
    assert "Remote Control" in result["systemMessage"]
    assert len(spawn.calls) == 1


def test_stop_names_the_next_session_for_the_work_the_handoff_names(tools, tmp_path):
    on()
    path = flow.write_handoff(SID, "Title: fix flow names\nGoal: ship the parser", "C:/work")
    transcript(tmp_path, {"type": "ai-title", "aiTitle": "Какая модель и усилие"})
    spawn = Spawn()
    result = flow.stop(SID, {"permission_mode": "auto",
                             "transcript_path": str(tmp_path / "transcript.jsonl")},
                       spawn=spawn)
    hop = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["hop"]
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert f"'--name' 'fix flow names (flow {hop}/{flow.MAX_HOPS})'" in script
    assert "Какая модель" not in script
    assert 'Named "fix flow names' in result["systemMessage"]


def test_stop_names_it_from_the_folder_when_the_handoff_has_no_usable_title(tools):
    """What a live handoff written before there was a gate gets: the folder, not its Goal
    cut at 60 characters."""
    on()
    path = flow.write_handoff(SID, "Goal: Continue the HACCP work (answer in Russian)",
                              "C:/work/haccp")
    flow.stop(SID, {"permission_mode": "auto"}, spawn=Spawn())
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert "'--name' 'haccp (flow 1/5)'" in script
    assert "HACCP" not in script


def test_stop_does_not_carry_the_previous_hop_s_name_on(tools, tmp_path):
    """Hop 2 of a chain was named by hop 1's `--name`; that is a description of the work
    already handed off, so hop 2 is named by its own handoff instead."""
    on(sid="s0")
    first = flow.write_handoff("s0", "Goal: ship the parser", "C:/work")
    flow.stop("s0", {"permission_mode": "auto"}, spawn=Spawn())
    # What opens the second window: the next session starts from hop 1's handoff.
    flow.session_start({"session_id": SID, "source": "startup"},
                       env={flow.HANDOFF_ENV: str(first)})
    transcript(tmp_path, {"type": "custom-title",
                          "customTitle": f"ship the parser (flow 1/{flow.MAX_HOPS})"})
    on()
    path = flow.write_handoff(SID, "Title: fix flow names", "C:/work")
    flow.stop(SID, {"permission_mode": "auto",
                    "transcript_path": str(tmp_path / "transcript.jsonl")}, spawn=Spawn())
    hop = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["hop"]
    assert hop == 2
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert f"'--name' 'fix flow names (flow 2/{flow.MAX_HOPS})'" in script
    assert f"ship the parser (flow 1/{flow.MAX_HOPS})" not in script


def test_stop_without_a_transcript_still_names_it_from_the_handoff(tools):
    on()
    path = flow.write_handoff(SID, "Title: fix flow names\nGoal: ship the parser", "C:/work")
    result = flow.stop(SID, {"permission_mode": "auto"}, spawn=Spawn())
    hop = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["hop"]
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert "--remote-control" not in script
    assert f"'--name' 'fix flow names (flow {hop}/{flow.MAX_HOPS})'" in script
    assert "Remote Control" not in result["systemMessage"]


def test_a_continuation_is_never_lost_to_a_naming_problem(tools, monkeypatch):
    """A transcript that cannot be read must not cost the user the next session."""
    on()
    path = flow.write_handoff(SID, "Goal: ship the parser", "C:/work")

    def explode(*args, **kwargs):
        raise MemoryError("transcript too big")

    monkeypatch.setattr(flow, "transcript_facts", explode)
    spawn = Spawn()
    result = flow.stop(SID, {"transcript_path": "C:/anywhere.jsonl"}, spawn=spawn)
    assert "handed off" in result["systemMessage"]
    assert len(spawn.calls) == 1
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert "--name" not in script and "--remote-control" not in script

"""Flow mode: a handoff without a title that can name the next session is refused.

The session name is read back out of the handoff the manager writes, and instructions go
stale inside a running session: one that wrote its handoff from the section list as it
read it hours earlier was named from its Goal, cut mid-word ("...(answer in Russia… (flow
2/5)"). So the gate is here, at the moment the text is written, and a refused handoff
keeps its text -- `ruti flow handoff --title "..."` then finishes it with nothing to
re-send. Nothing here spawns a process or writes outside tmp_path: `conftest.isolated_state`
moves `FLOW_DIR`, the session store and the context store.
"""

from __future__ import annotations

import re

import pytest
from click.testing import CliRunner

from ruti import cli, flow, modes, sessions

SID = "s1"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def tools(monkeypatch):
    found = {"wt": "C:/bin/wt.exe", "pwsh": "C:/bin/pwsh.exe", "claude": "C:/bin/claude.exe"}
    monkeypatch.setattr(flow.shutil, "which", lambda name: found.get(name))
    return found


@pytest.fixture
def writing(monkeypatch):
    """A session that is allowed to hand off, with a session id of its own."""
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    modes.set_flow(SID, True)
    return SID


def invoke(args, stdin=None):
    return CliRunner().invoke(cli.main, args, input=stdin)


def written():
    return modes.flow_state(SID).get("handoff")


# ------------------------------------------------------------------ the gate itself


@pytest.mark.parametrize("title", ["fix flow session names", "Исправить имена flow-сессий"])
def test_a_title_that_names_the_work_is_accepted(title):
    assert flow.title_problem(title) is None


@pytest.mark.parametrize("title, why", [
    ("", "there is no title"),
    ("   ", "there is no title"),
    (None, "there is no title"),
    ("parser", "2-10 words, this is 1"),
    ("one two three four five six seven eight nine ten eleven",
     "2-10 words, this is 11"),
    ("fix " + "x" * 67, "at most 70 characters"),
    ("Continue the HACCP work", "not an instruction to continue"),
    ("Продолжить работу над графом", "not an instruction to continue"),
    ("handoff notes for the founder", "not an instruction to continue"),
])
def test_a_title_that_cannot_name_a_window_is_refused_with_its_reason(title, why):
    problem = flow.title_problem(title)
    assert problem is not None and why in problem


def test_only_the_start_of_the_title_is_read_for_an_instruction():
    """`fix the handoff template` names work; "continue" on its own does not."""
    assert flow.title_problem("fix the handoff template") is None
    assert flow.title_problem("resume the parser rewrite") is not None


def test_the_bounds_are_what_the_module_says_they_are():
    assert (flow.TITLE_MIN_WORDS, flow.TITLE_MAX_WORDS) == (2, 10)
    assert flow.TITLE_MAX_CHARS == 70


# ------------------------------------------------------------------ preparing the text


def test_a_title_line_is_left_alone_and_not_duplicated():
    text = "Title: fix flow names\n\nGoal: ship it\nDone: yesterday"
    assert flow.prepare_handoff(text) == text


def test_a_title_line_anywhere_becomes_the_first_line():
    text = "# Handoff\n\n**Title:** fix flow names\n\nGoal: ship it"
    out = flow.prepare_handoff(text)
    assert out.splitlines()[0] == "Title: fix flow names"
    assert out.count("Title") == 1 and "Goal: ship it" in out


def test_the_argument_wins_over_the_line_in_the_text():
    text = "Title: Continue the project\n\nGoal: ship it"
    out = flow.prepare_handoff(text, "fix flow names")
    assert out.splitlines()[0] == "Title: fix flow names"
    assert "Continue the project" not in out
    assert "Goal: ship it" in out


def test_whitespace_in_a_title_is_collapsed_because_it_lands_in_a_window_title():
    out = flow.prepare_handoff("Goal: x", "  fix   flow\nnames  ")
    assert out.startswith("Title: fix flow names\n")


@pytest.mark.parametrize("text, title", [
    ("Goal: ship it", None),                       # neither: no line, no argument
    ("Title: Continue the work\nGoal: x", None),    # a line, but not a usable one
    ("Goal: ship it", "one"),                      # an argument, but too short
])
def test_neither_a_line_nor_an_argument_usable_raises(text, title):
    with pytest.raises(flow.TitleError) as excinfo:
        flow.prepare_handoff(text, title)
    assert str(excinfo.value)


# -------------------------------------------------------------------------- the CLI


def test_a_handoff_without_a_title_is_refused_and_its_text_kept(writing):
    result = invoke(["flow", "handoff"], "## Goal\nship the parser")
    assert result.exit_code != 0, result.output
    out = ANSI.sub("", result.output)
    assert "--title" in out and "kept" in out
    assert "2-10 words" in out  # what is wrong, in a sentence
    assert written() is None                     # nothing was written
    assert flow.load_draft(SID) == "## Goal\nship the parser"


def test_only_the_title_has_to_be_sent_to_finish_it(writing):
    invoke(["flow", "handoff"], "## Goal\nship the parser")
    result = invoke(["flow", "handoff", "--title", "fix flow names"])
    assert result.exit_code == 0, result.output
    text = open(written(), encoding="utf-8").read()
    assert text.splitlines()[:3] == ["Title: fix flow names", "", "## Goal"]
    assert text.rstrip().endswith("ship the parser")
    assert flow.load_draft(SID) is None           # the draft is not a second handoff
    assert "session name: fix flow names" in ANSI.sub("", result.output)


def test_a_title_on_the_first_line_is_enough_in_one_call(writing):
    result = invoke(["flow", "handoff"], "Title: fix flow names\n\n## Goal\nship it")
    assert result.exit_code == 0, result.output
    assert open(written(), encoding="utf-8").read().splitlines()[0] == "Title: fix flow names"
    assert flow.load_draft(SID) is None


def test_a_title_that_is_only_an_instruction_is_refused(writing):
    result = invoke(["flow", "handoff", "--title", "Continue the work"], "## Goal\nx")
    assert result.exit_code != 0, result.output
    out = ANSI.sub("", result.output)
    assert "not an instruction to continue" in out
    assert written() is None
    assert flow.load_draft(SID) == "## Goal\nx"  # the text is still kept, title or not


def test_a_handoff_from_a_file_is_gated_the_same_way(writing, tmp_path):
    path = tmp_path / "handoff.md"
    path.write_text("## Goal\nship it\n", encoding="utf-8")
    result = invoke(["flow", "handoff", "--file", str(path)])
    assert result.exit_code != 0, result.output
    assert written() is None
    assert invoke(["flow", "handoff", "--file", str(path),
                   "--title", "fix flow names"]).exit_code == 0


def test_nothing_sent_at_all_is_still_refused(writing):
    result = invoke(["flow", "handoff"])
    assert result.exit_code != 0, result.output
    assert "there is no title" in ANSI.sub("", result.output)
    assert written() is None


def test_only_the_first_title_line_is_replaced_and_the_rest_of_the_text_is_kept():
    text = "\n".join(["Title: old name here", "## Goal", "ship it",
                      "- Title: a task that is called this"])
    out = flow.prepare_handoff(text, "fix flow names")
    assert out.splitlines()[0] == "Title: fix flow names"
    assert "old name here" not in out
    assert "- Title: a task that is called this" in out

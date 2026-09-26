"""Flow mode: a session past half its context hands its task to a fresh one.

Nothing here spawns a real process or writes outside tmp_path: `spawn` is a stub, and
`conftest.isolated_state` moves `FLOW_DIR`, the session store and the context store.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time

import pytest
from click.testing import CliRunner

from ruti import cli, context_watch, flow, modes, quota, sessions, statusline, wait
from ruti.hooks import session_start, user_prompt_submit, wait_gate

SID = "s1"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEREDOC = "ruti flow handoff <<'EOF'\nGoal: x\nDone: y\nEOF"


def on(pct=None, sid=SID):
    modes.set_flow(sid, True)
    if pct is not None:
        context_watch.record(sid, pct)


def tool(name, **tool_input):
    return {"tool_name": name, "tool_input": tool_input}


def snap(used):
    return quota.Quota(five_hour=quota.Window(used, time.time() + 3600), seven_day=None,
                       captured_at=time.time())


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


# ---------------------------------------------------------------------- the notice


def test_the_notice_comes_once_past_the_line():
    on(55)
    first = flow.post_tool_use(SID)
    note = first["hookSpecificOutput"]["additionalContext"]
    assert "55%" in note and "ruti flow handoff" in note
    assert flow.post_tool_use(SID) is None


def test_no_notice_under_the_line():
    on(40)
    assert flow.post_tool_use(SID) is None


# -------------------------------------------------------------------- the gate


def test_nothing_is_refused_between_the_notice_and_the_force_line():
    on(55)
    assert flow.pre_tool_use(SID, tool("Edit", file_path="x")) is None


def denied(result):
    return result is not None and \
        result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_past_the_force_line_only_ruti_and_the_task_list_get_through():
    on(65)
    assert denied(flow.pre_tool_use(SID, tool("Edit", file_path="x")))
    assert denied(flow.pre_tool_use(SID, tool("Bash", command="git status")))
    assert flow.pre_tool_use(SID, tool("Bash", command="ruti status")) is None
    assert flow.pre_tool_use(SID, tool("TodoWrite", todos=[])) is None


def test_the_handoff_heredoc_itself_is_let_through():
    on(65)
    assert flow.pre_tool_use(SID, tool("Bash", command=HEREDOC)) is None
    pwsh = "@'\nGoal: x\n'@ | ruti flow handoff"
    assert flow.pre_tool_use(SID, tool("PowerShell", command=pwsh)) is None


def test_nothing_may_ride_after_the_heredoc():
    on(65)
    assert denied(flow.pre_tool_use(SID, tool("Bash", command=HEREDOC + "\nrm -rf build")))
    assert denied(flow.pre_tool_use(SID, tool("Bash", command=HEREDOC + " && git push")))


def test_once_handed_off_every_other_tool_is_refused_whatever_the_context():
    on(30)
    flow.write_handoff(SID, "Goal: x", "C:/work")
    result = flow.pre_tool_use(SID, tool("Edit", file_path="x"))
    assert denied(result)
    assert "handoff is written" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert flow.pre_tool_use(SID, tool("Bash", command="ruti mode flow off")) is None


# ------------------------------------------------------------------ the handoff


def test_the_handoff_carries_modes_hop_and_directory():
    on()
    modes.set_free(SID, "soft")
    path = flow.write_handoff(SID, "Goal: finish\nNext: test", "C:/work")
    assert path.read_text(encoding="utf-8").startswith("Goal: finish")
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["from_session"] == SID and meta["cwd"] == "C:/work" and meta["hop"] == 1
    assert meta["modes"]["flow"] is True and meta["modes"]["free"] == "soft"
    assert modes.flow_state(SID)["handoff"] == str(path)


def test_an_empty_handoff_is_refused():
    on()
    with pytest.raises(ValueError):
        flow.write_handoff(SID, "  \n ", "C:/work")


def test_no_second_handoff_after_launching(tools):
    on()
    flow.write_handoff(SID, "Goal: x", "C:/work")
    flow.stop(SID, {}, spawn=Spawn())
    with pytest.raises(ValueError):
        flow.write_handoff(SID, "Goal: again", "C:/work")


# --------------------------------------------------------------------- stop


def test_stop_opens_the_next_session_exactly_once(tools, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    on()
    path = flow.write_handoff(SID, "Goal: x", "C:/work")
    spawn = Spawn()
    result = flow.stop(SID, {"permission_mode": "auto"}, spawn=spawn)
    assert "handed off" in result["systemMessage"] and "hop 1" in result["systemMessage"]
    assert "decision" not in result
    [(argv, kwargs)] = spawn.calls
    assert argv[:3] == ["C:/bin/wt.exe", "-w", "new"]
    assert argv[-2:] == ["-File", str(path.with_suffix(".ps1"))]
    assert "CLAUDE_CODE_SESSION_ID" not in {k.upper() for k in kwargs["env"]}
    assert flow.stop(SID, {}, spawn=spawn) is None
    assert len(spawn.calls) == 1


def test_nothing_happens_at_stop_without_a_handoff(tools):
    on(70)
    spawn = Spawn()
    assert flow.stop(SID, {}, spawn=spawn) is None
    assert not spawn.calls


def test_the_launcher_script(tools):
    on()
    path = flow.write_handoff(SID, "Goal: x", "C:/it's here")
    flow.stop(SID, {"permission_mode": "bypassPermissions"}, spawn=Spawn())
    script = path.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert "Set-Location -LiteralPath 'C:/it''s here'" in script
    assert f"$env:RUTI_FLOW_HANDOFF = '{path}'" in script
    assert "'C:/bin/claude.exe' '--permission-mode' 'bypassPermissions'" in script


@pytest.mark.parametrize("mode", ["default", None, "sideways"])
def test_no_permission_flag_for_a_mode_the_cli_would_refuse(mode):
    script = flow.launcher_script(flow.FLOW_DIR / "h.md", "C:/w", mode, "claude")
    assert "--permission-mode" not in script


def test_without_windows_terminal_a_console_of_its_own(tools, monkeypatch):
    tools.pop("wt")
    on()
    flow.write_handoff(SID, "Goal: x", "C:/work")
    spawn = Spawn(fail=OSError("breakaway not permitted"))
    result = flow.stop(SID, {}, spawn=spawn)
    assert "handed off" in result["systemMessage"]
    assert len(spawn.calls) == 2  # retried without breaking away from the job
    assert spawn.calls[0][0][0] == "C:/bin/pwsh.exe"


def test_the_chain_stops_at_the_limit(tools):
    on()
    modes.set_flow_state(SID, {"hop": flow.MAX_HOPS})
    path = flow.write_handoff(SID, "Goal: x", "C:/work")
    spawn = Spawn()
    result = flow.stop(SID, {}, spawn=spawn)
    assert "chain limit" in result["systemMessage"] and str(path) in result["systemMessage"]
    assert not spawn.calls
    assert modes.flow_state(SID)["launched"] is True


def test_a_failed_launch_says_how_to_go_on_by_hand(tools):
    on()
    path = flow.write_handoff(SID, "Goal: x", "C:/work")
    result = flow.stop(SID, {}, spawn=Spawn(fail=RuntimeError("no terminal")))
    assert "could not open" in result["systemMessage"] and str(path) in result["systemMessage"]
    assert modes.flow_state(SID)["launched"] is True


# ------------------------------------------------------------ the new session


def handed_off():
    on(sid="old1")
    modes.set_coding("old1", True)
    modes.set_free("old1", "soft")
    return flow.write_handoff("old1", "Goal: ship it\nNext: run the tests", "C:/work")


def test_the_new_session_takes_over_modes_and_reads_the_handoff():
    path = handed_off()
    result = flow.session_start({"session_id": "new1", "source": "startup"},
                                env={flow.HANDOFF_ENV: str(path)})
    user, model = result
    assert "old1" in user and "hop 1" in user
    assert "Goal: ship it" in model and "Next: run the tests" in model
    state = modes.current("new1")
    assert state["flow"] is True and state["coding"] is True and state["free"] == "soft"
    assert modes.flow_state("new1") == {"hop": 1, "noticed": False, "handoff": None,
                                         "launched": False}


@pytest.mark.parametrize("source", ["clear", "compact", "resume"])
def test_no_reinjection_after_clear_compact_or_resume(source):
    path = handed_off()
    assert flow.session_start({"session_id": "new1", "source": source},
                              env={flow.HANDOFF_ENV: str(path)}) is None


def test_an_ordinary_session_start_is_left_alone():
    assert flow.session_start({"session_id": "new1", "source": "startup"}, env={}) is None
    assert flow.session_start({"session_id": "new1", "source": "startup"},
                              env={flow.HANDOFF_ENV: "C:/nowhere.md"}) is None


def test_the_session_start_hook_merges_flow_with_the_health_report(monkeypatch, capsys):
    monkeypatch.setattr(session_start, "_mark_start", lambda: None)
    monkeypatch.setattr(session_start, "build_report", lambda: ("doctor says", "doctor ctx"))
    monkeypatch.setattr(flow, "session_start", lambda payload: ("flow says", "flow ctx"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "startup"})))
    assert session_start.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["systemMessage"] == "flow says\n\ndoctor says"
    assert out["hookSpecificOutput"]["additionalContext"] == "flow ctx\n\ndoctor ctx"


# ---------------------------------------------------------------- dispatch


def test_neither_mode_on_means_nothing_at_all():
    context_watch.record(SID, 90)
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert wait_gate.handle({"session_id": SID, "hook_event_name": event,
                                 **tool("Edit")}) is None


def test_waits_refusal_wins_over_flows(monkeypatch):
    monkeypatch.setattr(quota, "load", lambda: snap(96))
    modes.set_wait(SID, True)
    on(65)
    result = wait_gate.handle({"session_id": SID, "hook_event_name": "PreToolUse",
                               **tool("Edit")})
    assert "wait mode" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_both_notices_arrive_together(monkeypatch):
    monkeypatch.setattr(quota, "load", lambda: snap(91))
    modes.set_wait(SID, True)
    on(55)
    result = wait_gate.handle({"session_id": SID, "hook_event_name": "PostToolUse"})
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "ruti wait mode" in text and "ruti flow" in text


def test_a_wait_resume_wins_over_a_handoff(monkeypatch, tools):
    monkeypatch.setattr(quota, "load", lambda: snap(10))
    monkeypatch.setattr(wait, "stop", lambda sid, s: {"decision": "block", "reason": "resume"})
    modes.set_wait(SID, True)
    on()
    flow.write_handoff(SID, "Goal: x", "C:/work")
    result = wait_gate.handle({"session_id": SID, "hook_event_name": "Stop"})
    assert result == {"decision": "block", "reason": "resume"}
    assert modes.flow_state(SID)["launched"] is False


def test_flow_alone_goes_through_the_gate(tools, monkeypatch):
    spawned = Spawn()
    monkeypatch.setattr(flow.subprocess, "Popen", spawned)
    on(65)
    result = wait_gate.handle({"session_id": SID, "hook_event_name": "PreToolUse",
                               **tool("Edit")})
    assert denied(result)
    flow.write_handoff(SID, "Goal: x", "C:/work")
    result = wait_gate.handle({"session_id": SID, "hook_event_name": "Stop"})
    assert "handed off" in result["systemMessage"] and len(spawned.calls) == 1


# ------------------------------------------------------------- the rest of ruti


def test_the_context_warning_says_hand_off_when_flow_is_on():
    context_watch.record(SID, 60)
    assert "/compact" in context_watch.warning(SID)
    on()
    note = context_watch.warning(SID)
    assert "ruti flow handoff" in note and "/compact" not in note


def test_a_default_can_turn_flow_on():
    modes.set_defaults({"flow": "on"})
    assert modes.current("any")["flow"] is True


def test_turning_flow_off_drops_a_handoff_not_yet_launched():
    on()
    flow.write_handoff(SID, "Goal: x", "C:/work")
    modes.set_flow(SID, False)
    assert modes.flow_state(SID)["handoff"] is None


def test_the_prompt_hook_names_the_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    monkeypatch.setattr(user_prompt_submit, "MEMO_FILE", tmp_path / "memo.json")
    monkeypatch.setattr(quota, "load", lambda: snap(20))
    on()
    context, _ = user_prompt_submit.build_context("")
    assert "flow mode is ON" in context


@pytest.fixture
def no_live_facts(monkeypatch):
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))


def test_the_status_line_shows_flow_and_then_the_arrow(no_live_facts):
    on()
    line = ANSI.sub("", statusline.render({"session_id": SID}, snap(10)))
    assert " flow " in f" {line} ".replace("·", " ")
    modes.set_flow_state(SID, {**modes.flow_state(SID), "launched": True})
    assert "flow→" in statusline.render({"session_id": SID}, snap(10))


def invoke(args, stdin=None):
    return CliRunner().invoke(cli.main, args, input=stdin)


def test_cli_mode_flow_and_handoff(monkeypatch):
    monkeypatch.setattr(sessions, "current_session_id", lambda: SID)
    result = invoke(["flow", "handoff"], "Goal: x")
    assert result.exit_code != 0 and "flow mode is off" in result.output
    assert invoke(["mode", "flow", "on"]).exit_code == 0
    result = invoke(["flow", "handoff"], "Цель: довести до конца\nДальше: тесты")
    assert result.exit_code == 0, result.output
    path = modes.flow_state(SID)["handoff"]
    assert "Цель: довести до конца" in open(path, encoding="utf-8").read()
    assert invoke(["mode", "flow", "off"]).exit_code == 0
    assert modes.flow_state(SID)["handoff"] is None

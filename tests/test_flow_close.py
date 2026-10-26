from ruti import flow, modes


def test_a_handed_off_session_with_nothing_running_may_close(monkeypatch):
    state = {}
    monkeypatch.setattr(modes, "flow_state", lambda sid: dict(state))
    monkeypatch.setattr(modes, "set_flow_state", lambda sid, st: state.update(st))
    assert flow.may_close("s")["close"] is False  # not handed off yet
    state.update({"handoff": "h.md", "launched": True})
    out = flow.stop("s", {"background_tasks": []})
    assert "closes itself" in out["stopReason"]
    assert flow.may_close("s") == {"close": True, "why": "handed off, nothing running"}


def test_background_work_keeps_the_window_open_and_is_named(monkeypatch):
    state = {"handoff": "h.md", "launched": True}
    monkeypatch.setattr(modes, "flow_state", lambda sid: dict(state))
    monkeypatch.setattr(modes, "set_flow_state", lambda sid, st: state.update(st))
    tasks = [{"id": "b1", "type": "shell", "status": "running",
              "description": "Keep WSL alive", "command": "wsl -d Debian -- sleep 86400"}]
    out = flow.stop("s", {"background_tasks": tasks})
    assert "stays open" in out["stopReason"] and "sleep 86400" in out["stopReason"]
    verdict = flow.may_close("s")
    assert verdict["close"] is False and verdict["busy"] == ["shell: wsl -d Debian -- sleep 86400"]


def test_an_inherited_no_color_is_not_handed_to_the_next_session():
    env = {"NO_COLOR": "1", "PATH": "x", "CLAUDE_CODE_CHILD_SESSION": "1"}
    assert "NO_COLOR" in flow.session_markers(env, persistent=set())
    assert "NO_COLOR" not in flow.session_markers(env, persistent={"NO_COLOR"})

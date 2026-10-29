import unicodedata

import pytest

from ruti import footer


def setup_state(monkeypatch, facts=None, jobs=None, disabled=False, modes=None):
    monkeypatch.setattr(footer.time, "time", lambda: 100)
    monkeypatch.setattr(footer, "read_json", lambda *a, **kw: {
        "at": 100, **(facts if facts is not None else {
            "proxy": True, "loaded": [], "gpu": {"free_mib": 5120},
        }),
    })
    monkeypatch.setattr(footer.sessions, "is_disabled", lambda _sid: disabled)
    monkeypatch.setattr(footer.modes, "current", lambda _sid: modes or {})
    monkeypatch.setattr(footer.runs, "active", lambda _marker: jobs or [])


def test_idle_footer_uses_local_facts_without_subscription_numbers(monkeypatch):
    setup_state(monkeypatch)
    assert footer.render("codex:one") == "Ruti on proxy up local:unknown GPU5.0G"


def test_offline_and_session_flags(monkeypatch):
    setup_state(monkeypatch, facts={"proxy": False, "loaded": []}, disabled=True,
                modes={"coding": True, "free": "hard"})
    assert footer.render("codex:one") == "Ruti OFF coding free:hard proxy down local:unknown GPU unknown"


def test_delegate_is_scoped_to_exact_host_and_session(monkeypatch):
    setup_state(monkeypatch, jobs=[
        {"session": "claude:one", "model": "other-claude"},
        {"session": "codex:two", "model": "other-codex"},
        {"session": None, "model": "unscoped"},
        {"session": "codex:one", "model": "ruti-router/gemini-flash", "started_at": 70},
    ])
    monkeypatch.setattr(footer.time, "time", lambda: 100)
    text = footer.render("codex:one")
    assert "gemini-flash 30s" in text
    assert "other" not in text and "unscoped" not in text
    assert "jobs" not in text


def test_multiple_delegates_are_counted_and_missing_identity_is_not_matched(monkeypatch):
    setup_state(monkeypatch, jobs=[
        {"session": "codex:one", "model": "a"},
        {"session": "codex:one", "model": "b"},
        {"session": None, "model": "unscoped"},
    ])
    assert "2 jobs" in footer.render("codex:one")
    monkeypatch.setattr(footer.sessions, "current_session_id", lambda: None)
    assert "jobs" not in footer.render()


def test_untrusted_names_cannot_inject_terminal_sequences(monkeypatch):
    setup_state(monkeypatch, facts={"proxy": True, "loaded": [
        {"id": "\x1b[31mMODEL\x1b[0m\n\u202eevil\x1b]0;BAD TITLE\x07"},
    ]})
    text = footer.render("codex:one")
    assert "MODEL" in text
    assert all(ch not in text for ch in ["\x1b", "\n", "\u202e", "\x07"])
    assert "BAD TITLE" not in text
    assert len(text) <= 120


def test_damaged_local_state_fails_without_traceback(monkeypatch):
    setup_state(monkeypatch)
    def broken(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(footer, "read_json", broken)
    assert footer.render("codex:one") == "Ruti unavailable"


@pytest.mark.parametrize("facts, expected", [
    ({}, "unknown"), ({"at": 0}, "stale"), ({"at": 70}, "stale"),
    ({"at": 101}, "unknown"), ({"at": float("nan")}, "unknown"),
    ({"at": True}, "unknown"),
])
def test_missing_and_expired_facts_never_look_healthy(monkeypatch, facts, expected):
    setup_state(monkeypatch)
    monkeypatch.setattr(footer, "read_json", lambda *a, **kw: {
        "proxy": True, "loaded": [{"id": "old-model"}], "gpu": {"free_mib": 5120},
        **facts,
    })
    text = footer.render("codex:one")
    assert f"proxy {expected}" in text
    assert f"local:{expected}" in text and f"GPU {expected}" in text
    assert "old-model" not in text and "5.0" not in text


def test_render_does_not_refresh_or_read_claude_quota(monkeypatch):
    from ruti import quota

    setup_state(monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("footer must not probe services or read Claude quota")
    monkeypatch.setattr(footer.statusline, "_refresh_facts", forbidden)
    monkeypatch.setattr(footer.statusline, "_get", forbidden)
    monkeypatch.setattr(quota, "load", forbidden)
    assert "5h" not in footer.render("codex:one")
    assert "7d" not in footer.render("codex:one")


def test_hook_modes_are_marked_and_output_is_bounded(monkeypatch):
    setup_state(monkeypatch, modes={key: True for key in
                ("wait", "flow", "manager", "timemachine", "effplus")})
    text = footer.render("codex:one")
    for label in ("wait*", "flow*", "manager*", "timemachine*", "eff+*"):
        assert label in text
    setup_state(monkeypatch, facts={"loaded": [{"id": "x" * 5000}]}, jobs=[{
        "session": "codex:one", "model": "\x1b[31m" + "y" * 5000 + "\r\n\x00\u202e",
    }])
    text = footer.render("codex:one")
    assert len(text) <= 120
    assert all(not unicodedata.category(ch).startswith("C") for ch in text)
    assert "unknown" in text  # missing delegate start time is not invented as 0s


def test_real_session_records_and_delegate_markers_are_isolated(monkeypatch, tmp_path):
    from ruti import modes, runs, sessions
    from ruti.config import write_json

    monkeypatch.setenv("RUTI_HOME", str(tmp_path))
    monkeypatch.setattr(sessions, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(modes, "SESSIONS_FILE", sessions.SESSIONS_FILE)
    monkeypatch.setattr(modes, "DEFAULTS_FILE", tmp_path / "defaults.json")
    monkeypatch.setattr(footer.delegate, "RUNNING_FILE", tmp_path / "running.json")
    monkeypatch.setattr(footer.time, "time", lambda: 100)
    monkeypatch.setattr(footer.statusline, "FACTS_FILE", tmp_path / "facts.json")
    write_json(sessions.SESSIONS_FILE, {
        "codex:one": {"disabled": True, "coding": True, "free": "hard", "wait": True},
        "codex:two": {"flow": True},
        "one": {"manager": True, "effplus": True},  # Claude raw ID
    })
    runs.start(footer.delegate.RUNNING_FILE, "only-one", 60, "a" * 8, "codex:one")
    runs.start(footer.delegate.RUNNING_FILE, "only-two", 60, "b" * 8, "codex:two")
    runs.start(footer.delegate.RUNNING_FILE, "only-claude", 60, "c" * 8, "one")
    first, second = footer.render("codex:one"), footer.render("codex:two")
    assert "Ruti OFF coding free:hard wait*" in first and "only-one" in first
    assert "flow*" not in first and "only-two" not in first
    assert "Ruti on flow*" in second and "only-two" in second
    assert all(value not in second for value in ("OFF", "coding", "free:hard", "wait*", "only-one"))
    for text in (first, second):
        assert all(value not in text for value in ("manager*", "eff+*", "only-claude"))
    monkeypatch.setenv("RUTI_HOST", "codex")
    monkeypatch.setenv("RUTI_SESSION_ID", "one")
    assert footer.render() == first


def test_absent_session_does_not_display_default_modes(monkeypatch):
    setup_state(monkeypatch, disabled=True, modes={"coding": True, "wait": True})
    monkeypatch.setattr(footer.sessions, "current_session_id", lambda: None)
    text = footer.render()
    assert text.startswith("Ruti unknown")
    assert "coding" not in text and "wait*" not in text


def test_successfully_observed_empty_model_list_means_none(monkeypatch):
    setup_state(monkeypatch, facts={"loaded": [], "loaded_ok": True})
    assert "local:none" in footer.render("codex:one")
    setup_state(monkeypatch, facts={"loaded": [], "loaded_ok": False})
    assert "local:unknown" in footer.render("codex:one")


def test_cli_requests_background_refresh_before_read_only_render(monkeypatch, capsys):
    import sys
    from ruti import footer_facts

    calls = []
    monkeypatch.setattr(sys, "argv", ["footer", "--session-id", "thread-one"])
    monkeypatch.setattr(footer_facts, "refresh_in_background", lambda: calls.append("request"))
    monkeypatch.setattr(footer, "render", lambda: calls.append("render") or "Ruti on")
    footer.main()
    assert calls == ["request", "render"]
    assert capsys.readouterr().out == "Ruti on\n"
    assert footer.sessions.current_session_id() == "codex:thread-one"

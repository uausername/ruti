from ruti import footer


def setup_state(monkeypatch, facts=None, jobs=None, disabled=False, modes=None):
    monkeypatch.setattr(footer.statusline, "_refresh_facts", lambda: facts or {
        "proxy": True, "loaded": [], "gpu": {"free_mib": 5120},
    })
    monkeypatch.setattr(footer.sessions, "is_disabled", lambda _sid: disabled)
    monkeypatch.setattr(footer.modes, "current", lambda _sid: modes or {})
    monkeypatch.setattr(footer.runs, "active", lambda _marker: jobs or [])


def test_idle_footer_uses_local_facts_without_subscription_numbers(monkeypatch):
    setup_state(monkeypatch)
    assert footer.render("codex:one") == "Ruti on proxy up local:none GPU5.0G"


def test_offline_and_session_flags(monkeypatch):
    setup_state(monkeypatch, facts={"proxy": False, "loaded": []}, disabled=True,
                modes={"coding": True, "free": "hard"})
    assert footer.render("codex:one") == "Ruti OFF coding free:hard proxy down local:none"


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
    def broken():
        raise OSError("unavailable")
    monkeypatch.setattr(footer.statusline, "_refresh_facts", broken)
    assert footer.render("codex:one") == "Ruti unavailable"

from click.testing import CliRunner

from ruti import cli, delegate, install, ledger, quota, router, sessions


def test_codex_session_does_not_reuse_claude(monkeypatch):
    monkeypatch.setenv("RUTI_HOST", "codex")
    monkeypatch.delenv("RUTI_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
    assert sessions.current_session_id() is None
    assert ledger.current_session() is None
    monkeypatch.setenv("RUTI_SESSION_ID", "same")
    assert sessions.current_session_id() == "codex:same"


def test_codex_ignores_claude_quota(monkeypatch):
    monkeypatch.setenv("RUTI_HOST", "codex")
    monkeypatch.setattr(quota, "read_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    snapshot = quota.load()
    assert snapshot.five_hour is None
    assert snapshot.seven_day is None
    assert router._anthropic_candidates(router.Task(), snapshot) == []
    own = router._self_candidate(router.Task(), snapshot)
    assert own.name == "codex:self"
    assert "Claude" not in own.pays_in


def test_claude_session_remains_compatible(monkeypatch):
    monkeypatch.delenv("RUTI_HOST", raising=False)
    monkeypatch.delenv("RUTI_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "original")
    assert sessions.current_session_id() == "original"


def test_codex_thread_automatically_selects_host_and_enables_session(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-one")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unrelated-claude")
    sessions.set_disabled("codex:thread-one", True)
    sessions.set_disabled("unrelated-claude", True)
    assert sessions.current_host() == "codex"
    assert sessions.current_session_id() == "codex:thread-one"
    result = CliRunner().invoke(cli.main, ["on"])
    assert result.exit_code == 0, result.output
    assert not sessions.is_disabled("codex:thread-one")
    assert sessions.is_disabled("unrelated-claude")


def test_explicit_claude_host_overrides_inherited_codex_thread(monkeypatch):
    monkeypatch.setenv("RUTI_HOST", "claude")
    monkeypatch.setenv("CODEX_THREAD_ID", "outer-codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inner-claude")
    assert sessions.current_host() == "claude"
    assert sessions.current_session_id() == "inner-claude"


def test_codex_install_preserves_user_guidance_and_is_idempotent(tmp_path, monkeypatch):
    target = tmp_path / "AGENTS.md"
    target.write_text("User rules\n", encoding="utf-8")
    monkeypatch.setattr(install, "CODEX_MEMORY", target)
    changes = install.codex_plan()
    assert target.read_text(encoding="utf-8") == "User rules\n"
    install.apply(changes)
    assert target.read_text(encoding="utf-8").startswith("User rules\n")
    assert install.codex_plan() == []


def test_codex_installer_preview_does_not_call_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "CODEX_MEMORY", tmp_path / "AGENTS.md")
    monkeypatch.setattr(install, "plan", lambda: (_ for _ in ()).throw(AssertionError()))
    result = CliRunner().invoke(cli.main, ["install", "--host", "codex"])
    assert result.exit_code == 0, result.output
    assert not install.CODEX_MEMORY.exists()


def test_snapshot_detects_content_change_with_same_git_status(tmp_path, monkeypatch):
    path = tmp_path / "existing.py"
    path.write_text("already dirty", encoding="utf-8")
    monkeypatch.setattr(delegate, "_git", lambda *a: "existing.py\0")
    before = delegate._file_snapshot(tmp_path)
    path.write_text("delegate edited it again", encoding="utf-8")
    after = delegate._file_snapshot(tmp_path)
    assert before.keys() == after.keys()
    assert before["existing.py"] != after["existing.py"]

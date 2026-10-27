import json
from pathlib import Path

import pytest

from click.testing import CliRunner

from ruti import cli, delegate, proc, usage


@pytest.mark.parametrize("times_out", [False, True])
def test_cli_worktree_round_trip_preserves_original_dirty_state(tmp_path, monkeypatch, times_out):
    real_run = proc.run
    source = tmp_path / "project with spaces"
    source.mkdir()
    real_run(["git", "init", "--quiet", str(source)]).check()
    (source / "existing.txt").write_text("baseline\n")
    real_run(["git", "add", "."], cwd=source).check()
    real_run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
              "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline"], cwd=source).check()
    (source / "existing.txt").write_text("user staged work\n")
    real_run(["git", "add", "existing.txt"], cwd=source).check()
    staged_before = real_run(["git", "diff", "--cached"], cwd=source).stdout
    monkeypatch.setattr(delegate, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(delegate, "_opencode_executable", lambda: "fake-opencode")
    monkeypatch.setattr(delegate, "probe", lambda *a: delegate.Probe("free"))
    monkeypatch.setattr(delegate, "_router_alias", lambda *a: False)
    monkeypatch.setattr(usage, "collect", lambda alias, *a, **k: usage.Usage(alias=alias))

    def fake_opencode(argv, **kwargs):
        if argv[0] != "fake-opencode":
            return real_run(argv, **kwargs)
        child = Path(kwargs["cwd"])
        config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["permission"]["external_directory"] == "deny"
        assert config["agent"]["build"]["permission"]["external_directory"] == "deny"
        assert argv[argv.index("--agent") + 1] == "build"
        assert child != source
        assert (child / "existing.txt").read_text() == "baseline\n"
        assert "Uncommitted source changes are excluded" in argv[-1]
        (child / "new.txt").write_text("delegate result\n")
        if times_out:
            raise proc.ToolTimeout("test timeout with partial result")
        return proc.Result(argv, 0, "done", "", 0)

    monkeypatch.setattr(proc, "run", fake_opencode)
    result = CliRunner().invoke(cli.main, ["delegate", "--model", "free", "--task", "implement",
                                          "--dir", str(source), "--worktree", "--json"])
    assert result.exit_code == (1 if times_out else 0), result.output
    outcome = json.loads(result.stdout)
    assert outcome["timed_out"] == times_out
    assert outcome["worktree"]["source_dirty_excluded"]
    assert Path(outcome["worktree"]["patch"]).is_file()
    assert outcome["files_changed"] == ["new.txt"]
    assert not (source / "new.txt").exists()
    imported = CliRunner().invoke(cli.main, ["worktree", "apply", "--run", outcome["run_id"],
                                            "--dir", str(source), "--json"])
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.stdout)["applied"]
    assert (source / "new.txt").read_text() == "delegate result\n"
    assert (source / "existing.txt").read_text() == "user staged work\n"
    assert real_run(["git", "diff", "--cached"], cwd=source).stdout == staged_before


def test_cli_worktree_rejects_path_like_run_ids():
    result = CliRunner().invoke(cli.main, ["worktree", "apply", "--run", "../../elsewhere"])
    assert result.exit_code == 1 and "invalid run ID" in result.output

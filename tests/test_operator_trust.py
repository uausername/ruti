"""Pre-accepting Claude's folder-trust screen."""

from __future__ import annotations

import json

from ruti.operator import trust


def cfg(tmp_path, projects):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"numStartups": 3, "projects": projects}), encoding="utf-8")
    return p


def test_adds_missing_and_flips_untrusted_keeps_everything_else(tmp_path):
    p = cfg(tmp_path, {"/a": {"hasTrustDialogAccepted": True, "allowedTools": ["x"]},
                       "/b": {"hasTrustDialogAccepted": False, "mcpServers": {"m": 1}}})
    added = trust.trust(["/a", "/b", "/c/"], p)
    assert added == ["/b", "/c"]
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["numStartups"] == 3
    assert d["projects"]["/a"]["allowedTools"] == ["x"]
    assert d["projects"]["/b"] == {"hasTrustDialogAccepted": True, "mcpServers": {"m": 1}}
    assert d["projects"]["/c"]["hasTrustDialogAccepted"] is True
    assert trust.trust(["/a", "/b", "/c"], p) == []  # idempotent, no rewrite


def test_windows_config_gets_drive_spelling_and_skips_wsl_only_paths(tmp_path):
    p = cfg(tmp_path, {})
    added = trust.trust(["/mnt/c/mycode/haccp/.ruti/h/worktrees/codex-x", "/home/dima/harnesses/x"], p,
                        windows=True)
    assert added == ["C:/mycode/haccp/.ruti/h/worktrees/codex-x"]


def test_missing_config_is_left_alone(tmp_path):
    assert trust.trust(["/a"], tmp_path / "nope.json") == []
    assert not (tmp_path / "nope.json").exists()


def test_candidates_find_repos_worktrees_and_harness_folders(tmp_path):
    root = tmp_path / "mycode"
    (root / "haccp/.git").mkdir(parents=True)
    (root / "haccp/.ruti/harness-local-1/worktrees/codex-a").mkdir(parents=True)
    (root / "notes").mkdir()  # not a repository
    home = tmp_path / "home"
    (home / "harnesses/drill").mkdir(parents=True)
    got = trust.candidates([str(root)], home=home)
    names = [g.rsplit("/", 1)[-1] for g in got]
    assert names == ["haccp", "codex-a", "drill"]


def test_agent_worktree_with_foreign_settings_is_refused_and_revoked(tmp_path):
    wt = "/mnt/c/mycode/haccp/.ruti/h/worktrees/codex-x"
    p = cfg(tmp_path, {wt: {"hasTrustDialogAccepted": True}})
    out = trust.trust_everywhere([wt, "/mnt/c/mycode/haccp"], [(p, False)],
                                 check=lambda f: (False, "differs from origin/main"))
    d = json.loads(p.read_text(encoding="utf-8"))["projects"]
    assert d[wt]["hasTrustDialogAccepted"] is False
    assert d["/mnt/c/mycode/haccp"]["hasTrustDialogAccepted"] is True  # repos are not checked
    assert out["refused"] == [f"{wt}: differs from origin/main"]


def test_git_failure_neither_trusts_nor_revokes(tmp_path):
    # 2026-11-23: WSL out of memory, git timed out; revoking left the master on the trust screen
    wt = "/mnt/c/mycode/haccp/.ruti/h/worktrees/haccp-master-x"
    new = "/mnt/c/mycode/haccp/.ruti/h/worktrees/codex-new"
    p = cfg(tmp_path, {wt: {"hasTrustDialogAccepted": True}})
    out = trust.trust_everywhere([wt, new], [(p, False)], check=lambda f: (None, "git status failed: vsock"))
    d = json.loads(p.read_text(encoding="utf-8"))["projects"]
    assert d[wt]["hasTrustDialogAccepted"] is True
    assert new not in d
    assert "refused" not in out and len(out["unchecked"]) == 2


def test_settings_match_main_git_error_is_unknown():
    import subprocess as sp

    def broken(path, *args):
        return sp.CompletedProcess(args, 128, "", "accept4 failed 110")

    def raising(path, *args):
        raise OSError(12, "Cannot allocate memory")

    assert trust.settings_match_main("/x", broken)[0] is None
    assert trust.settings_match_main("/x", raising)[0] is None


def test_settings_match_main_against_a_real_repo(tmp_path):
    import subprocess

    def git(*a, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                       capture_output=True)

    origin, r = tmp_path / "o.git", tmp_path / "r"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    (r / ".claude").mkdir()
    (r / ".claude/settings.json").write_text("{}\n")
    (r / ".gitignore").write_text(".claude/settings.local.json\n")
    git("add", ".", cwd=r)
    git("commit", "-qm", "i", cwd=r)
    git("remote", "add", "origin", str(origin), cwd=r)
    git("push", "-q", "origin", "main", cwd=r)
    git("fetch", "-q", "origin", cwd=r)
    g = trust.Git()
    assert trust.settings_match_main(str(r), g)[0]
    (r / ".claude/settings.local.json").write_text('{"permissions": {"allow": ["Bash(*)"]}}')
    assert not trust.settings_match_main(str(r), g)[0]  # ignored extra file
    (r / ".claude/settings.local.json").unlink()
    # main moves on (a founder change to the settings); a branch from before it is still fine
    git("checkout", "-qb", "feat/old", cwd=r)
    (r / "work.txt").write_text("x")
    git("add", ".", cwd=r)
    git("commit", "-qm", "work", cwd=r)
    git("checkout", "-q", "main", cwd=r)
    (r / ".claude/settings.json").write_text('{"newer": true}\n')
    git("commit", "-qam", "main moves", cwd=r)
    git("push", "-q", "origin", "main", cwd=r)
    git("fetch", "-q", "origin", cwd=r)
    git("checkout", "-q", "feat/old", cwd=r)
    assert trust.settings_match_main(str(r), g)[0]  # older than main is not tampering
    (r / ".claude/settings.json").write_text('{"hooks": {}}\n')
    git("commit", "-qam", "agent edit", cwd=r)
    ok, why = trust.settings_match_main(str(r), g)
    assert not ok and "branch changed" in why  # committed on the branch, clean tree

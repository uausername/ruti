"""gc: only clean worktrees whose HEAD is safe elsewhere are removable."""

from __future__ import annotations

import subprocess

import pytest

from ruti.operator import gc


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, check=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    r = tmp_path / "haccp"
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    (r / ".gitignore").write_text("/.ruti/\n")
    git("add", ".", cwd=r)
    git("commit", "-qm", "init", cwd=r)
    git("remote", "add", "origin", str(origin), cwd=r)
    git("push", "-q", "origin", "main", cwd=r)
    return r


def add(repo, name, *extra):
    path = repo / ".ruti/h/worktrees" / name
    git("worktree", "add", "-q", *extra, str(path), "main", cwd=repo)
    return path


def entries(repo, **kw):
    kw.setdefault("prs", {})
    return {e.path.replace("\\", "/").rsplit("/", 1)[-1]: e for e in gc.plan(str(repo), sizes=False, **kw)}


def test_clean_worktree_on_main_is_removable(repo):
    add(repo, "codex-done", "--detach")
    e = entries(repo)["codex-done"]
    assert e.removable and e.safe == "in origin/main"


def test_dirty_or_untracked_worktree_stays(repo):
    p = add(repo, "codex-dirty", "--detach")
    (p / "new.txt").write_text("x")
    e = entries(repo)["codex-dirty"]
    assert not e.removable and "uncommitted" in e.keep[0]


def test_unpushed_commit_stays(repo):
    p = add(repo, "codex-local", "-b", "feat/local")
    (p / "f.txt").write_text("x")
    git("add", ".", cwd=p)
    git("commit", "-qm", "local only", cwd=p)
    e = entries(repo)["codex-local"]
    assert not e.removable and "no remote branch" in " ".join(e.keep)


def test_squash_merged_pr_head_is_removable(repo):
    p = add(repo, "codex-pr", "-b", "feat/pr")
    (p / "f.txt").write_text("x")
    git("add", ".", cwd=p)
    git("commit", "-qm", "pr work", cwd=p)
    head = git("rev-parse", "HEAD", cwd=p)
    e = entries(repo, prs={head: {"number": 7, "state": "MERGED"}})["codex-pr"]
    assert e.removable and "PR #7" in e.safe


def test_pushed_branch_is_removable(repo):
    p = add(repo, "codex-pushed", "-b", "feat/pushed")
    (p / "f.txt").write_text("x")
    git("add", ".", cwd=p)
    git("commit", "-qm", "pushed", cwd=p)
    git("push", "-q", "origin", "feat/pushed", cwd=p)
    assert entries(repo)["codex-pushed"].removable


def test_live_session_and_keep_stay(repo):
    p = add(repo, "codex-live", "--detach")
    add(repo, "haccp-master-1", "--detach")
    es = entries(repo, live={str(p)}, keep=["haccp-master"])
    assert not es["codex-live"].removable and not es["haccp-master-1"].removable


def test_path_spellings():
    assert gc.to_windows("/mnt/c/mycode/haccp") == "C:/mycode/haccp"
    assert gc.to_wsl("C:/mycode/haccp/.ruti/x") == "/mnt/c/mycode/haccp/.ruti/x"
    assert gc.to_wsl("/tmp/x") == "/tmp/x" and gc.to_windows("/tmp/x") == "/tmp/x"


def test_windows_git_from_wsl_converts_paths_and_never_prunes():
    calls = []

    def run(argv):
        calls.append(argv)
        out = ""
        if "list" in argv:
            out = ("worktree C:/mycode/haccp\nHEAD a\n\n"
                   "worktree C:/mycode/haccp/.ruti/h/worktrees/codex-x\nHEAD b\n")
        return subprocess.CompletedProcess(argv, 0, out, "")

    g = gc.Git(run, "git.exe", windows=True)
    es = gc.worktrees("/mnt/c/mycode/haccp", g)
    assert es[1].path == "/mnt/c/mycode/haccp/.ruti/h/worktrees/codex-x"
    e = gc.Entry(path=es[1].path, safe="in origin/main")
    gc.apply("/mnt/c/mycode/haccp", [e], git=g)
    assert calls[-1] == ["git.exe", "-C", "C:/mycode/haccp", "worktree", "remove",
                         "C:/mycode/haccp/.ruti/h/worktrees/codex-x"]
    assert not any("prune" in c for c in calls)


def test_apply_removes_only_removable(repo):
    add(repo, "codex-done", "--detach")
    p = add(repo, "codex-dirty", "--detach")
    (p / "new.txt").write_text("x")
    plan = gc.plan(str(repo), prs={}, sizes=False)
    res = gc.apply(str(repo), plan)
    assert [r["removed"] for r in res] == [True]
    assert not (repo / ".ruti/h/worktrees/codex-done").exists() and p.exists()

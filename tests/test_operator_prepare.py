"""prepare: a clean LF worktree with a relative gitdir, a profile and a receipt."""

from __future__ import annotations

import json
import subprocess

import pytest

from ruti.operator import prepare as prep
from ruti.operator.prepare import PrepareError

NOW = 1_800_000_000.0
OK_GUARD = {"ok": True, "commit": "abc", "ref": "main", "synced": True, "intact": True,
            "current": True, "hook_installed": True}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "haccp"
    (r / ".ruti/harness-local-20261109/worktrees").mkdir(parents=True)
    (r / ".ruti/harness-local-20261103").mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=r)
    (r / ".gitignore").write_text("/.ruti/\n")
    (r / "a.txt").write_bytes(b"one\ntwo\n")
    git("add", ".", cwd=r)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=r)
    return r


OK_QUOTA = {"ok": True, "reasons": [], "summary": {"claude": {}, "codex": {}}}


def run(repo, name="codex-abc", **kw):
    kw.setdefault("quota_gate", lambda: OK_QUOTA)
    kw.setdefault("guard_status", lambda: OK_GUARD)
    return prep.prepare(name, repo=str(repo), git_exe=None, **kw)


def test_prepares_clean_worktree_with_relative_gitdir_profile_and_receipt(repo):
    r = run(repo, model="gpt-6.1-sol", effort="high")
    wt = repo / ".ruti/harness-local-20261109/worktrees/codex-abc"
    assert r["worktree"] == str(wt)
    pointer = (wt / ".git").read_text().strip()
    assert pointer == "gitdir: ../../../../.git/worktrees/codex-abc"
    assert git("status", "--porcelain", cwd=wt) == ""
    assert (wt / ".codex/config.toml").read_text() == 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n'
    receipt = json.loads((repo / ".ruti/harness-local-20261109/codex-runs/abc/PREPARED.json").read_text())
    assert receipt["head"] == git("rev-parse", "main", cwd=repo).strip()
    assert receipt["guard"]["commit"] == "abc" and receipt["profile"]["model"] == "gpt-6.1-sol"
    assert (wt / "a.txt").read_bytes() == b"one\ntwo\n"


def test_branch_option_creates_a_branch(repo):
    run(repo, branch="feature/x")
    wt = repo / ".ruti/harness-local-20261109/worktrees/codex-abc"
    assert git("branch", "--show-current", cwd=wt).strip() == "feature/x"


def test_exclude_is_added_once(repo):
    run(repo, name="codex-a1", model="m")
    run(repo, name="codex-a2", model="m")
    lines = (repo / ".git/info/exclude").read_text().splitlines()
    assert lines.count("/.codex/config.toml") == 1


@pytest.mark.parametrize("name", ["claude-x", "codex-", "codex-A", "codex-x/../y", "codex-" + "a" * 40])
def test_bad_names_are_refused(repo, name):
    with pytest.raises(PrepareError):
        run(repo, name=name)


def test_existing_worktree_is_refused(repo):
    run(repo)
    with pytest.raises(PrepareError, match="exists"):
        run(repo)


def test_spent_quota_refuses_unless_overridden(repo):
    q = {"ok": False, "reasons": ["Codex quota at its limit"], "summary": {}}
    with pytest.raises(PrepareError, match="Codex quota"):
        run(repo, quota_gate=lambda: q)
    assert not (repo / ".ruti/harness-local-20261109/worktrees/codex-abc").exists()
    run(repo, quota_gate=lambda: q, ignore_quota=True)


def test_guard_not_ready_refuses_unless_overridden(repo):
    bad = {**OK_GUARD, "ok": False, "hook_installed": False}
    with pytest.raises(PrepareError, match="guard"):
        run(repo, guard_status=lambda: bad)
    run(repo, guard_status=lambda: bad, allow_no_guard=True)

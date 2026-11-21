"""The trusted guard copy: sync from git, the shim, and the user-level hook entry."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ruti.operator import guard

FAKE_GUARD = b'''from pathlib import Path
import sys, types
rules_path = Path(__file__).with_name("protect_rules.py")
rules = types.ModuleType("r"); exec(compile(rules_path.read_bytes(), str(rules_path), "exec"), rules.__dict__)

def reason(payload, root):
    target = payload["tool_input"]["file_path"]
    return rules.judge(Path(root), target)
'''
FAKE_RULES = b'''def judge(root, target):
    if "PROTECTED" in target:
        return "HACCP guard: protected file " + target + " in " + root.name
    return None
'''


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "haccp"
    (r / "scripts/agent-pilot").mkdir(parents=True)
    (r / "scripts/agent-pilot/codex_guard.py").write_bytes(FAKE_GUARD)
    (r / "scripts/agent-pilot/protect_rules.py").write_bytes(FAKE_RULES)
    git("init", "-q", "-b", "main", cwd=r)
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".", cwd=r)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "guard", cwd=r)
    return r


def run_shim(target: Path, payload) -> dict | None:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    out = subprocess.run([sys.executable, "-I", str(target / "shim.py")], input=raw,
                         capture_output=True, timeout=30).stdout.decode().strip()
    return json.loads(out) if out else None


def edit(cwd, path):
    return {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(cwd),
            "tool_input": {"file_path": path}}


def test_sync_copies_committed_bytes_and_records_hashes(repo, tmp_path):
    target = tmp_path / "trusted"
    m = guard.sync(str(repo), "main", target=target)
    assert (target / "codex_guard.py").read_bytes() == FAKE_GUARD
    assert m["commit"] == git("rev-parse", "main", cwd=repo).strip()
    assert set(m["files"]) == {"codex_guard.py", "protect_rules.py"}


def test_sync_ignores_uncommitted_changes_in_the_checkout(repo, tmp_path):
    (repo / "scripts/agent-pilot/protect_rules.py").write_bytes(b"def judge(r, t):\n    return None\n")
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    assert (target / "protect_rules.py").read_bytes() == FAKE_RULES


def test_shim_denies_protected_edit_in_a_worktree_under_the_repo(repo, tmp_path):
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    wt = repo / ".ruti/harness-local-1/worktrees/codex-x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../../../../.git/worktrees/codex-x\n")
    out = run_shim(target, edit(wt / "app", "PROTECTED.sql"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "codex-x" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert run_shim(target, edit(wt, "app/lib/x.dart")) is None


def test_worktree_copy_of_the_guard_is_not_what_runs(repo, tmp_path):
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    wt = repo / ".ruti/harness-local-1/worktrees/codex-x"
    (wt / "scripts/agent-pilot").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    (wt / "scripts/agent-pilot/protect_rules.py").write_bytes(b"def judge(r, t):\n    return None\n")
    assert run_shim(target, edit(wt, "PROTECTED"))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shim_leaves_other_repositories_alone(repo, tmp_path):
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    assert run_shim(target, edit(other, "PROTECTED")) is None


@pytest.mark.parametrize("payload", [b"not json", {"hook_event_name": "PreToolUse", "cwd": "relative"}])
def test_shim_fails_closed(repo, tmp_path, payload):
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    assert run_shim(target, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shim_with_broken_guard_copy_denies(repo, tmp_path):
    target = tmp_path / "trusted"
    guard.sync(str(repo), "main", target=target)
    (target / "codex_guard.py").chmod(0o644)
    (target / "codex_guard.py").write_bytes(b"raise RuntimeError('broken')\n")
    out = run_shim(target, edit(repo, "app/x.dart"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_install_hook_puts_guard_first_and_is_idempotent(tmp_path):
    hooks = tmp_path / "hooks.json"
    ruti_cmd = "/venv/python -I -X utf8 -m ruti.hooks.codex"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": ruti_cmd}]}],
                                           "Stop": [{"hooks": [{"type": "command", "command": ruti_cmd}]}]}}))
    first = guard.install_hook(hooks, command="/usr/bin/python3 -I shim.py")
    data = json.loads(hooks.read_text())
    pre = data["hooks"]["PreToolUse"]
    assert first["changed"] and first["backup"]
    assert pre[0]["matcher"] == guard.MATCHER and pre[0]["hooks"][0]["command"] == "/usr/bin/python3 -I shim.py"
    assert pre[1]["hooks"][0]["command"] == ruti_cmd and data["hooks"]["Stop"]
    assert guard.install_hook(hooks, command="/usr/bin/python3 -I shim.py")["changed"] is False


def test_status_reports_stale_tampered_and_missing_hook(repo, tmp_path):
    target, hooks = tmp_path / "trusted", tmp_path / "hooks.json"
    guard.sync(str(repo), "main", target=target)
    s = guard.status(target=target, hooks=hooks)
    assert s["intact"] and s["current"] and not s["hook_installed"] and not s["ok"]
    guard.install_hook(hooks, command=guard.hook_command(target))
    assert guard.status(target=target, hooks=hooks)["ok"]
    (repo / "scripts/agent-pilot/protect_rules.py").write_bytes(FAKE_RULES + b"# v2\n")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2", cwd=repo)
    assert guard.status(target=target, hooks=hooks)["current"] is False
    (target / "shim.py").chmod(0o644)
    (target / "shim.py").write_text("# tampered")
    assert guard.status(target=target, hooks=hooks)["intact"] is False

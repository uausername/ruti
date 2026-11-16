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

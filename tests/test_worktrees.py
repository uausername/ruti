import json
import os
import subprocess
from pathlib import Path

import pytest
from ruti import worktrees


# Helper function to run git commands
def run_git(cwd: Path, *args: str):
    """Run a git command in the specified directory."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "LC_ALL": "C"}
    cmd = ["git", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def init_repo(path: Path):
    """Initialize a git repository and make an initial commit."""
    run_git(path, "init", "-b", "main")
    (path / "initial.txt").write_text("initial content")
    run_git(path, "add", "initial.txt")
    run_git(path, "commit", "-m", "Initial commit")
    return run_git(path, "rev-parse", "HEAD").stdout.strip()


def make_dirty(path: Path, filename: str = "test.txt", content: str = "dirty content"):
    """Create a dirty working tree state."""
    (path / filename).write_text(content)
    return path / filename


def make_staged(path: Path, filename: str = "staged.txt", content: str = "staged content"):
    """Create a staged change."""
    (path / filename).write_text(content)
    run_git(path, "add", filename)
    return path / filename


def make_untracked(path: Path, filename: str = "untracked.txt", content: str = "untracked content"):
    """Create an untracked file."""
    (path / filename).write_text(content)
    return path / filename


def get_head_commit(path: Path):
    """Get the current HEAD commit hash."""
    return run_git(path, "rev-parse", "HEAD").stdout.strip()


# Test cases
def test_prepare_worktree_basic(tmp_path: Path):
    source_path = tmp_path / "Source Repo"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree"
    worktree = worktrees.prepare(source_path, worktree_path)

    assert worktree.source == source_path.resolve()
    assert worktree.directory == worktree_path.resolve()
    assert worktree.base_commit == get_head_commit(source_path)
    assert not worktree.source_dirty
    assert worktree_path.is_dir()
    assert worktree_path.exists()
    assert (worktree_path / "initial.txt").exists()


def test_prepare_worktree_source_dirty_preserved(tmp_path: Path):
    source_path = tmp_path / "Source Repo with spaces"
    source_path.mkdir()
    init_repo(source_path)
    (source_path / "initial.txt").write_text("user unstaged edit")
    make_dirty(source_path, "dirty_file.txt")
    make_staged(source_path, "staged_file.txt")
    make_untracked(source_path, "untracked_file.txt")

    worktree_path = tmp_path / "Work Tree with spaces"
    worktree = worktrees.prepare(source_path, worktree_path)

    assert worktree.source_dirty  # Source was dirty
    assert (source_path / "initial.txt").read_text() == "user unstaged edit"
    assert (worktree_path / "initial.txt").read_text() == "initial content"
    assert not (worktree_path / "dirty_file.txt").exists()
    assert not (worktree_path / "staged_file.txt").exists()
    assert not (worktree_path / "untracked_file.txt").exists()
    assert (source_path / "dirty_file.txt").exists()
    assert (source_path / "staged_file.txt").exists()
    assert (source_path / "untracked_file.txt").exists()
    assert "dirty_file.txt" in run_git(source_path, "status", "--porcelain").stdout
    assert "staged_file.txt" in run_git(source_path, "status", "--porcelain").stdout
    assert "untracked_file.txt" in run_git(source_path, "status", "--porcelain").stdout


def test_export_and_apply_text_modification(tmp_path: Path):
    source_path = tmp_path / "Source Text"
    source_path.mkdir()
    initial_commit = init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Text"
    worktree = worktrees.prepare(source_path, worktree_path)

    # Modify a file in the worktree
    (worktree_path / "new_file.txt").write_text("added content in worktree")
    (worktree_path / "initial.txt").write_text("modified content in worktree")

    patch_path = tmp_path / "patches" / "text_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)

    assert patch_path.is_file()
    assert "sha256" in exported_manifest
    assert not exported_manifest["applied"]
    assert Path(exported_manifest["patch"]) == patch_path
    assert Path(exported_manifest["source"]) == source_path.resolve()

    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)
    assert manifest_json_path.is_file()

    # Apply the patch to the source
    assert not (source_path / "new_file.txt").exists()
    assert (source_path / "initial.txt").read_text() == "initial content"

    assert worktrees.apply_saved(manifest_json_path, source_path)
    assert (source_path / "new_file.txt").read_text() == "added content in worktree"
    assert (source_path / "initial.txt").read_text() == "modified content in worktree"

    # Check that source index is not changed (e.g., changes are in working directory but not staged)
    status_output = run_git(source_path, "status", "--porcelain").stdout
    assert "new_file.txt" in status_output
    assert "initial.txt" in status_output
    assert "nothing to commit" not in run_git(source_path, "status").stdout
    assert "Changes to be committed" not in run_git(source_path, "status").stdout


def test_export_and_apply_new_binary_file(tmp_path: Path):
    source_path = tmp_path / "Source Binary"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Binary"
    worktree = worktrees.prepare(source_path, worktree_path)

    # Create a new binary file in the worktree
    binary_content = b"\x00\x01\x02\x03\xff\xfe\xfd\xfc"
    (worktree_path / "binary_file.bin").write_bytes(binary_content)

    patch_path = tmp_path / "patches" / "binary_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)

    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    assert worktrees.apply_saved(manifest_json_path, source_path)
    assert (source_path / "binary_file.bin").read_bytes() == binary_content
    status_output = run_git(source_path, "status", "--porcelain").stdout
    assert "binary_file.bin" in status_output
    assert "nothing to commit" not in run_git(source_path, "status").stdout
    assert "Changes to be committed" not in run_git(source_path, "status").stdout
    assert "Untracked files" in run_git(source_path, "status").stdout


def test_apply_conflicting_dirty_source_rejected(tmp_path: Path):
    source_path = tmp_path / "Source Conflict"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Conflict"
    worktree = worktrees.prepare(source_path, worktree_path)

    # Modify a file in the worktree
    (worktree_path / "initial.txt").write_text("worktree modified content")
    (worktree_path / "new_result.txt").write_text("must not be partially imported")

    patch_path = tmp_path / "patches" / "conflict_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)
    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    # Make source dirty with conflicting changes
    (source_path / "initial.txt").write_text("source modified content causing conflict")

    with pytest.raises(RuntimeError):
        worktrees.apply_saved(manifest_json_path, source_path)

    # Ensure source is still dirty and no partial apply
    assert (source_path / "initial.txt").read_text() == "source modified content causing conflict"
    assert not (source_path / "new_result.txt").exists()
    assert "initial.txt" in run_git(source_path, "status", "--porcelain").stdout


def test_apply_patch_tampering_refused(tmp_path: Path):
    source_path = tmp_path / "Source Tamper"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Tamper"
    worktree = worktrees.prepare(source_path, worktree_path)
    (worktree_path / "new_file.txt").write_text("original content")

    patch_path = tmp_path / "patches" / "tamper_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)
    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    # Tamper with the patch file
    patch_path.write_text("tampered content", encoding="utf-8")

    with pytest.raises(ValueError, match="patch changed since export"):
        worktrees.apply_saved(manifest_json_path, source_path)


def test_apply_changed_source_head_refused(tmp_path: Path):
    source_path = tmp_path / "Source Head Change"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Head Change"
    worktree = worktrees.prepare(source_path, worktree_path)
    (worktree_path / "new_file.txt").write_text("content")

    patch_path = tmp_path / "patches" / "head_change_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)
    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    # Make a new commit in the source
    (source_path / "another_file.txt").write_text("another content")
    run_git(source_path, "add", "another_file.txt")
    run_git(source_path, "commit", "-m", "Another commit")

    with pytest.raises(ValueError, match="source HEAD changed since delegation"):
        worktrees.apply_saved(manifest_json_path, source_path)


def test_apply_wrong_source_refused(tmp_path: Path):
    source_path = tmp_path / "Source Original"
    source_path.mkdir()
    init_repo(source_path)

    wrong_source_path = tmp_path / "Wrong Source"
    wrong_source_path.mkdir()
    init_repo(wrong_source_path)

    worktree_path = tmp_path / "Work Tree Original"
    worktree = worktrees.prepare(source_path, worktree_path)
    (worktree_path / "new_file.txt").write_text("content")

    patch_path = tmp_path / "patches" / "wrong_source_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)
    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    with pytest.raises(ValueError, match="--dir does not match this run's original source checkout"):
        worktrees.apply_saved(manifest_json_path, wrong_source_path)


def test_apply_repeated_application_refused(tmp_path: Path):
    source_path = tmp_path / "Source Repeated"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path = tmp_path / "Work Tree Repeated"
    worktree = worktrees.prepare(source_path, worktree_path)
    (worktree_path / "new_file.txt").write_text("content")

    patch_path = tmp_path / "patches" / "repeated_patch.patch"
    exported_manifest = worktrees.export(worktree, patch_path)
    manifest_json_path = patch_path.parent / f"{patch_path.stem}.json"
    worktrees.write_json(manifest_json_path, exported_manifest)

    assert worktrees.apply_saved(manifest_json_path, source_path)

    # Attempt to apply again
    with pytest.raises(ValueError, match="this patch was already applied"):
        worktrees.apply_saved(manifest_json_path, source_path)


def test_two_worktrees_independent(tmp_path: Path):
    source_path = tmp_path / "Source Independent"
    source_path.mkdir()
    init_repo(source_path)

    worktree_path_1 = tmp_path / "Work Tree 1"
    worktree_1 = worktrees.prepare(source_path, worktree_path_1)
    (worktree_path_1 / "file_1.txt").write_text("content 1")

    worktree_path_2 = tmp_path / "Work Tree 2"
    worktree_2 = worktrees.prepare(source_path, worktree_path_2)
    (worktree_path_2 / "file_2.txt").write_text("content 2")

    # Export and apply patch 1
    patch_path_1 = tmp_path / "patches" / "independent_patch_1.patch"
    exported_manifest_1 = worktrees.export(worktree_1, patch_path_1)
    manifest_json_path_1 = patch_path_1.parent / f"{patch_path_1.stem}.json"
    worktrees.write_json(manifest_json_path_1, exported_manifest_1)
    assert worktrees.apply_saved(manifest_json_path_1, source_path)
    assert (source_path / "file_1.txt").read_text() == "content 1"
    assert not (source_path / "file_2.txt").exists()

    patch_path_2 = tmp_path / "patches" / "independent_patch_2.patch"
    exported_manifest_2 = worktrees.export(worktree_2, patch_path_2)
    manifest_json_path_2 = patch_path_2.parent / f"{patch_path_2.stem}.json"
    worktrees.write_json(manifest_json_path_2, exported_manifest_2)
    assert worktrees.apply_saved(manifest_json_path_2, source_path)
    assert (source_path / "file_2.txt").read_text() == "content 2"
    assert (source_path / "file_1.txt").read_text() == "content 1"


def test_export_includes_commits_made_by_delegate(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    workspace = worktrees.prepare(source, tmp_path / "child")
    (workspace.directory / "committed.txt").write_text("delegate committed result")
    run_git(workspace.directory, "add", ".")
    run_git(workspace.directory, "commit", "-m", "delegate commit")
    patch = tmp_path / "result.patch"
    data = worktrees.export(workspace, patch)
    worktrees.write_json(tmp_path / "result.json", data)
    assert worktrees.apply_saved(tmp_path / "result.json", source)
    assert (source / "committed.txt").read_text() == "delegate committed result"


def test_changed_git_pointer_cannot_stage_into_source_index(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    workspace = worktrees.prepare(source, tmp_path / "child")
    (workspace.directory / "new.txt").write_text("delegate result")
    source_git = run_git(source, "rev-parse", "--absolute-git-dir").stdout.strip()
    # Git marks this file hidden on Windows; open-existing avoids CREATE_ALWAYS.
    with (workspace.directory / ".git").open("r+", encoding="utf-8") as stream:
        stream.write(f"gitdir: {source_git}\n")
        stream.truncate()
    with pytest.raises(ValueError, match="Git identity changed"):
        worktrees.export(workspace, tmp_path / "result.patch")
    assert run_git(source, "diff", "--cached", "--name-only").stdout == ""

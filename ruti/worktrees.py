"""Isolated HEAD checkouts and reviewable patches; never stash or reset the source."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import read_json, write_json


def git(source: Path, *args: str) -> str:
    return proc.run(["git", *args], cwd=source, timeout=120, strip_progress=False).check().stdout.strip()


@dataclass(frozen=True)
class Worktree:
    source: Path
    directory: Path
    base_commit: str
    source_dirty: bool
    git_directory: Path

    def summary(self) -> dict:
        return {"source": str(self.source), "directory": str(self.directory),
                "base_commit": self.base_commit, "source_dirty_excluded": self.source_dirty,
                "git_directory": str(self.git_directory)}


def prepare(source: Path, directory: Path) -> Worktree:
    source, directory = source.resolve(), directory.resolve()
    root = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
    if source != root:
        raise ValueError("--worktree requires --dir to point to the Git repository root")
    if directory.is_relative_to(source) or directory.exists():
        raise ValueError("worktree destination must be new and outside the source checkout")
    base = git(source, "rev-parse", "HEAD")
    dirty = bool(git(source, "status", "--porcelain"))
    directory.parent.mkdir(parents=True, exist_ok=True)
    git(source, "worktree", "add", "--detach", str(directory), base)
    git_directory = Path(git(directory, "rev-parse", "--absolute-git-dir")).resolve()
    source_git_directory = Path(git(source, "rev-parse", "--absolute-git-dir")).resolve()
    if git_directory == source_git_directory:
        raise ValueError("isolated checkout does not have its own Git index")
    return Worktree(source, directory, base, dirty, git_directory)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export(workspace: Worktree, patch: Path) -> dict:
    patch = patch.resolve()
    if patch.is_relative_to(workspace.directory):
        raise ValueError("patch must be stored outside the delegated checkout")
    current_git = Path(git(workspace.directory, "rev-parse", "--absolute-git-dir")).resolve()
    if current_git != workspace.git_directory or workspace.directory.resolve() != workspace.directory:
        raise ValueError("delegated checkout Git identity changed; refusing to stage files")
    patch.parent.mkdir(parents=True, exist_ok=True)
    # Only the disposable worktree's index is staged, including new visible files.
    # Comparing against the initial base also includes any commits the child made.
    git(workspace.directory, "add", "--all", "--", ".")
    git(workspace.directory, "diff", "--cached", "--binary", "--full-index",
        "--no-ext-diff", "--no-textconv", f"--output={patch}", workspace.base_commit, "--")
    return {**workspace.summary(), "patch": str(patch), "sha256": digest(patch), "applied": False}


def apply_saved(manifest: Path, source: Path) -> bool:
    data = read_json(manifest, default=None)
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) and data[key] for key in
                                            ("source", "patch", "sha256", "base_commit")):
        raise ValueError("no exported worktree patch found for this run")
    source = source.resolve()
    if Path(data["source"]).resolve() != source:
        raise ValueError("--dir does not match this run's original source checkout")
    if data.get("applied"):
        raise ValueError("this patch was already applied")
    patch = Path(data["patch"]).resolve()
    if patch.parent != manifest.resolve().parent or patch.stem != manifest.stem:
        raise ValueError("patch artifact does not match the run manifest")
    if digest(patch) != data["sha256"]:
        raise ValueError("patch changed since export; review and export it again")
    if git(source, "rev-parse", "HEAD") != data["base_commit"]:
        raise ValueError("source HEAD changed since delegation; review the patch manually")
    if not patch.stat().st_size:
        return False
    # No --reject, --3way, --index or --unsafe-paths: conflicts fail without a
    # partial apply, the source index is untouched, and Git rejects unsafe paths.
    git(source, "apply", "--check", "--", str(patch))
    git(source, "apply", "--", str(patch))
    write_json(manifest, {**data, "applied": True})
    return True

"""Clean up agent worktrees whose work is finished (`ruti operator gc`, dry run by default).

A worktree is removable only when *all* hold:

* it is a linked worktree under ``.ruti/`` of the repository (never the main checkout);
* no live Harness session works in it (``harness ps``) and it is not ``--keep``-listed;
* ``git status --porcelain`` is empty (no changes, no untracked files);
* its HEAD is already safe elsewhere: reachable from ``origin/main``, or it is the head of
  a merged or closed pull request (squash merges leave no ancestry), or reachable from
  some remote branch.

Everything else is listed with the reason it stays. ``--apply`` removes the listed ones
with ``git worktree remove`` (without ``--force``: git itself refuses a dirty tree).
Branches are not deleted.

Under WSL every git call goes through Windows ``git.exe``. The worktrees were made by
Windows git, so their admin ``gitdir`` files hold ``C:/...`` paths; WSL git cannot see
those, lists every such worktree as *prunable*, and a ``git worktree prune`` run from WSL
would wipe the metadata of all of them. For the same reason ``gc`` never runs
``worktree prune`` at all: ``worktree remove`` cleans up after itself.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def to_windows(p: str) -> str:
    s = str(p).replace("\\", "/")
    if s.startswith("/mnt/") and len(s) > 6 and s[6:7] in ("/", ""):
        return s[5].upper() + ":" + (s[6:] or "/")
    return s


def to_wsl(p: str) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        return "/mnt/" + s[0].lower() + s[2:]
    return s


class Git:
    """``git -C <path> ...`` with the right binary and path spelling for this side."""

    def __init__(self, run: Runner = _run, exe: str = "git", windows: bool = False):
        self.run, self.exe, self.windows = run, exe, windows

    @classmethod
    def auto(cls, repo: str, run: Runner = _run) -> "Git":
        if os.name != "nt" and str(repo).startswith("/mnt/"):
            from .prepare import windows_git

            exe = windows_git()
            if exe:
                return cls(run, exe, windows=True)
        return cls(run)

    def __call__(self, path: str, *args: str) -> subprocess.CompletedProcess:
        return self.run([self.exe, "-C", to_windows(path) if self.windows else path, *args])

    def local(self, path: str) -> str:
        return to_wsl(path) if self.windows else path


@dataclass
class Entry:
    path: str
    head: str = ""
    branch: str | None = None
    keep: list[str] = field(default_factory=list)
    safe: str | None = None
    size_mb: float | None = None

    @property
    def removable(self) -> bool:
        return not self.keep and self.safe is not None


def worktrees(repo: str, git: Git) -> list[Entry]:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    items, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = Entry(path=git.local(line[9:]))
            items.append(cur)
        elif cur and line.startswith("HEAD "):
            cur.head = line[5:]
        elif cur and line.startswith("branch "):
            cur.branch = line[7:].removeprefix("refs/heads/")
    return items


def _norm(p: str) -> str:
    """One spelling for Windows (C:\\x, C:/x) and WSL (/mnt/c/x) paths."""
    s = str(p).replace("\\", "/").rstrip("/")
    if s.startswith("/mnt/") and len(s) > 6 and s[6:7] in ("/", ""):
        s = s[5] + ":" + s[6:]
    return s.lower()


def live_folders(run: Runner = _run) -> set[str] | None:
    """Folders of Harness sessions that are not closed; None when harness cannot be asked."""
    import os
    import shutil

    argv = ["harness", "ps", "--json"]
    if os.name == "nt" or not shutil.which("harness"):
        argv = ["wsl", "-d", "Debian", "--", "bash", "-lc", "harness ps --json"]
    try:
        rows = json.loads(run(argv).stdout or "{}").get("harnesses") or []
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    return {r["folder"] for r in rows if r.get("folder") and r.get("state") not in ("closed", "stopped")}


def _prs(repo: str, git: Git) -> dict[str, dict]:
    """headRefOid -> PR (merged/closed), from gh; {} when gh is unavailable."""
    try:
        done = git.run(["gh", "pr", "list", "--repo", _slug(repo, git), "--state", "all", "--limit", "1000",
                        "--json", "number,state,headRefOid,headRefName"])
        rows = json.loads(done.stdout or "[]")
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}
    return {r["headRefOid"]: r for r in rows if r.get("state") in ("MERGED", "CLOSED")}


def _slug(repo: str, git: Git) -> str:
    url = git(repo, "remote", "get-url", "origin").stdout.strip()
    tail = url.split("github.com", 1)[-1].lstrip(":/")
    return tail.removesuffix(".git")


def _size_mb(path: str) -> float | None:
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return None
    return round(total / 1e6, 1)


def plan(repo: str, *, live: set[str] | None = None, keep: list[str] | None = None,
         run: Runner = _run, prs: dict | None = None, sizes: bool = True,
         git: Git | None = None) -> list[Entry]:
    git = git or Git.auto(repo, run)
    git(repo, "fetch", "--quiet", "--prune", "origin")
    entries = worktrees(repo, git)
    root = _norm(repo)
    live = {_norm(p) for p in (live or set())}
    keep = [_norm(k) for k in (keep or [])]
    prs = _prs(repo, git) if prs is None else prs
    out = []
    for e in entries:
        p = _norm(e.path)
        if p == root:
            continue  # the main checkout
        if f"{root}/.ruti/" not in p + "/":
            e.keep.append("outside .ruti/")
        if any(p == l or p.startswith(l + "/") or l.startswith(p + "/") for l in live):
            e.keep.append("a live Harness session works here")
        if any(k and k in p for k in keep):
            e.keep.append("--keep")
        if not Path(e.path).exists():
            e.keep.append("missing on disk (left for a person to look at)")
            out.append(e)
            continue
        status = git(e.path, "status", "--porcelain")
        if status.returncode:
            e.keep.append("git status failed: " + status.stderr.strip()[:120])
        elif status.stdout.strip():
            n = len(status.stdout.strip().splitlines())
            e.keep.append(f"{n} uncommitted/untracked path(s)")
        if git(repo, "merge-base", "--is-ancestor", e.head, "origin/main").returncode == 0:
            e.safe = "in origin/main"
        elif e.head in prs:
            pr = prs[e.head]
            e.safe = f"head of PR #{pr['number']} ({pr['state'].lower()})"
        else:
            remote = git(repo, "branch", "-r", "--contains", e.head).stdout.split()
            if remote:
                e.safe = "on " + remote[0]
            else:
                e.keep.append("HEAD is on no remote branch and in no merged/closed PR")
        if sizes and e.removable:
            e.size_mb = _size_mb(e.path)
        out.append(e)
    return out


def apply(repo: str, entries: list[Entry], run: Runner = _run, git: Git | None = None) -> list[dict]:
    git = git or Git.auto(repo, run)
    results = []
    for e in entries:
        if not e.removable:
            continue
        done = git(repo, "worktree", "remove", to_windows(e.path) if git.windows else e.path)
        results.append({"path": e.path, "removed": done.returncode == 0,
                        "error": done.stderr.strip()[:200] if done.returncode else None})
    return results  # no `worktree prune`: from WSL it would drop every Windows-made worktree

"""Prepare a Codex worker worktree completely before Codex starts in it.

`ruti operator prepare codex-<short> --model ... --effort ...`:

1. refuses when `ruti operator quota` says no new worker may start (Claude spent, Codex
   at 70%/85%, or ``max_parallel`` Codex harnesses already running) and
   while the trusted guard copy is missing, tampered, stale against the base, or not
   wired into ``~/.codex/hooks.json`` (``ruti operator guard status``);
2. creates the worktree with Windows git when it is reachable (the HACCP checkout lives on
   C:, and its other worktrees point back with ``C:/`` paths) and ``core.autocrlf=false``,
   so files are LF exactly as committed and CI sees them;
3. rewrites the worktree's ``.git`` file to a *relative* ``gitdir`` -- Windows git and
   WSL git both read it, where ``gitdir: C:/...`` broke every git command Codex ran in WSL;
4. writes ``.codex/config.toml`` with the chosen model and effort (kept out of
   ``git status`` through the repository's ``info/exclude``);
5. checks that ``git status`` is clean from this side, and leaves the receipt
   ``codex-runs/<short>/PREPARED.json`` (root, base, HEAD, profile, guard commit).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import guard as guard_mod
from . import quota as quota_mod
from . import trust as trust_mod

Runner = Callable[[list[str]], str]
NAME = re.compile(r"codex-[a-z0-9][a-z0-9-]{0,30}")
WINDOWS_GIT = "/mnt/c/Program Files/Git/cmd/git.exe"
EXCLUDE = "/.codex/config.toml"


class PrepareError(RuntimeError):
    pass


def _run(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if done.returncode:
        raise PrepareError(f"{' '.join(argv[:4])}… failed: {done.stderr.strip()[-400:]}")
    return done.stdout


def latest_harness_dir(repo: Path) -> Path:
    dirs = sorted(p for p in (repo / ".ruti").glob("harness-local-*") if p.is_dir())
    if not dirs:
        raise PrepareError(f"no .ruti/harness-local-* under {repo}")
    return dirs[-1]


def windows_git() -> str | None:
    if shutil.which("git.exe"):
        return "git.exe"
    return WINDOWS_GIT if Path(WINDOWS_GIT).exists() else None


def prepare(name: str, *, repo: str = "/mnt/c/mycode/haccp", harness_dir: str | None = None,
            base: str = "main", branch: str | None = None, model: str | None = None,
            effort: str | None = None, ignore_quota: bool = False, allow_no_guard: bool = False,
            run: Runner = _run, quota_gate: Callable[[], dict] | None = None,
            max_parallel: int = quota_mod.MAX_PARALLEL,
            guard_status: Callable[[], dict] | None = None,
            git_exe: str | None = "auto", to_windows: Callable[[str], str] | None = None,
            trust: Callable[[list[str]], object] | None = None) -> dict:
    if not NAME.fullmatch(name):
        raise PrepareError(f"worktree name must look like codex-<short>, got {name!r}")
    root = Path(repo)
    hdir = Path(harness_dir) if harness_dir else latest_harness_dir(root)
    wt = hdir / "worktrees" / name
    if wt.exists():
        raise PrepareError(f"{wt} already exists")

    q = (quota_gate or (lambda: quota_mod.gate(live_codex=quota_mod.live_codex_count(),
                                               max_parallel=max_parallel)))()
    if not q["ok"] and not ignore_quota:
        raise PrepareError("start no new worker: " + "; ".join(q["reasons"]))

    g = (guard_status or (lambda: guard_mod.status(repo, base)))()
    if not g.get("ok") and not allow_no_guard:
        raise PrepareError("trusted guard is not ready: " + json.dumps(
            {k: g.get(k) for k in ("synced", "intact", "current", "hook_installed")})
            + " -- run `ruti operator guard sync` / `guard install-hook`, then trust it in Codex /hooks")

    exe = windows_git() if git_exe == "auto" else git_exe
    conv = to_windows or (lambda p: run(["wslpath", "-w", p]).strip())
    if exe:
        add = [exe, "-C", conv(str(root)), "-c", "core.autocrlf=false", "worktree", "add"]
        target = conv(str(wt))
    else:
        add = ["git", "-C", str(root), "-c", "core.autocrlf=false", "worktree", "add"]
        target = str(wt)
    add += (["-b", branch] if branch else ["--detach"]) + [target, base]
    run(add)

    dotgit = wt / ".git"
    pointer = dotgit.read_text(encoding="utf-8").strip()
    leaf = re.split(r"[\\/]", pointer.split("gitdir:", 1)[1].strip().rstrip("/\\"))[-1]
    admin = root / ".git" / "worktrees" / leaf
    # r+ and truncate: Windows git marks .git hidden, and "w" cannot open a hidden file there
    with open(dotgit, "r+", encoding="utf-8", newline="\n") as fh:
        fh.seek(0)
        fh.write("gitdir: " + os.path.relpath(admin, wt).replace(os.sep, "/") + "\n")
        fh.truncate()

    exclude = root / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if EXCLUDE not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as fh:
            fh.write(("" if not lines or lines[-1] == "" else "\n") + EXCLUDE + "\n")

    profile = {k: v for k, v in (("model", model), ("model_reasoning_effort", effort)) if v}
    if profile:
        (wt / ".codex").mkdir(exist_ok=True)
        (wt / ".codex" / "config.toml").write_text(
            "".join(f'{k} = "{v}"\n' for k, v in profile.items()), encoding="utf-8")

    head = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).strip()
    dirty = run(["git", "-C", str(wt), "status", "--porcelain"]).strip()
    if dirty:
        raise PrepareError(f"{wt} is not clean right after creation:\n{dirty[:400]}")

    receipt = {"worktree": str(wt), "name": name, "base": base, "branch": branch, "head": head,
               "gitdir": dotgit.read_text(encoding="utf-8").strip(), "profile": profile,
               "guard": {k: g.get(k) for k in ("commit", "ref", "ok", "dir")},
               "quota": q["summary"],
               "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    runs = hdir / "codex-runs" / name[len("codex-"):]
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "PREPARED.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    receipt["receipt"] = str(runs / "PREPARED.json")
    # A Claude session started here (reviewer, master) must not stop on the trust screen.
    try:
        receipt["claude_trusted"] = bool((trust or trust_mod.trust_everywhere)([str(wt)]))
    except Exception as exc:  # noqa: BLE001 -- trust is a convenience, not a gate
        receipt["claude_trusted"] = f"error: {exc!r}"
    return receipt

"""Accept Claude Code's folder-trust screen ahead of time (`ruti operator trust`).

A Claude session started in a folder nobody has trusted stops on "Is this a project you
created or one you trust?" and does nothing until someone answers. The person running
this machine trusts every folder (2026-11-16), so agent folders are marked trusted in
``~/.claude.json`` before a session is started there.

Claude looks for trust from the folder upward, but only up to the folder's git root.
Every worktree is its own git root, so trusting ``/mnt/c/mycode/haccp`` does not cover
``.ruti/.../worktrees/codex-x``; each folder needs its own entry. ``sweep`` finds them:
the repositories under the roots, their ``.ruti/*/worktrees/*`` and ``~/harnesses/*``.

Under WSL the Windows configs (``/mnt/c/Users/*/.claude.json``, keys spelled ``C:/...``)
are updated too, for sessions started from Windows in the same folders.

Trust makes Claude apply the folder's own ``.claude/`` settings (hooks, pre-approved
tools) and ``.mcp.json`` without asking. In an agent worktree (``.ruti/*/worktrees/*``)
agents write those files, so such a folder is trusted only while ``.claude/`` and
``.mcp.json`` are exactly ``origin/main``'s -- no changes, no untracked or ignored extras
such as ``settings.local.json``. One that drifts loses its trust: Claude then asks again,
and the waker reports it in ``needs_admin``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import re
from typing import Callable

from .gc import Git, to_windows

DEFAULT_ROOTS = ("/mnt/c/mycode",)
CONFIG_PATHS = (".claude", ".mcp.json")
MAIN = "origin/main"
_AGENT_WT = re.compile(r"/\.ruti/[^/]+/worktrees/[^/]+$")


def is_agent_worktree(folder: str) -> bool:
    return bool(_AGENT_WT.search(str(folder).replace("\\", "/").rstrip("/")))


def settings_match_main(folder: str, git: Git | None = None) -> tuple[bool, str]:
    """Whether the folder's Claude settings are byte-for-byte origin/main's."""
    git = git or Git.auto(folder)
    st = git(folder, "status", "--porcelain", "--ignored", "--", *CONFIG_PATHS)
    if st.returncode:
        return False, "git status failed: " + (st.stderr or "").strip()[:120]
    if st.stdout.strip():
        return False, "changed/untracked: " + " ".join(l[3:] for l in st.stdout.splitlines())[:200]
    d = git(folder, "diff", "--quiet", MAIN, "--", *CONFIG_PATHS)
    if d.returncode == 1:
        return False, f"differs from {MAIN}"
    if d.returncode:
        return False, "git diff failed: " + (d.stderr or "").strip()[:120]
    return True, "same as " + MAIN


def vet(folders: list[str], check: Callable[[str], tuple[bool, str]] = settings_match_main
        ) -> tuple[list[str], dict[str, str]]:
    """(folders to trust, {folder: why not}) -- only agent worktrees are checked."""
    ok, bad = [], {}
    for f in folders:
        if is_agent_worktree(f):
            good, why = check(f)
            if not good:
                bad[f] = why
                continue
        ok.append(f)
    return ok, bad
# Claude Code's own defaults for a project entry.
ENTRY = {"allowedTools": [], "mcpContextUris": [], "mcpServers": {}, "enabledMcpjsonServers": [],
         "disabledMcpjsonServers": [], "hasTrustDialogAccepted": True,
         "hasClaudeMdExternalIncludesApproved": False, "hasClaudeMdExternalIncludesWarningShown": False}


def configs() -> list[tuple[Path, bool]]:
    """(config path, keys spelled the Windows way)."""
    if os.name == "nt":
        return [(Path.home() / ".claude.json", True)]
    out = [(Path.home() / ".claude.json", False)]
    out += [(p, True) for p in sorted(Path("/mnt/c/Users").glob("*/.claude.json"))]
    return out


def candidates(roots: tuple[str, ...] | list[str] = DEFAULT_ROOTS, home: Path | None = None) -> list[str]:
    found: list[Path] = []
    for root in map(Path, roots):
        if not root.is_dir():
            continue
        for repo in sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists()):
            found.append(repo)
            found += sorted(p for p in repo.glob(".ruti/*/worktrees/*") if p.is_dir())
    harnesses = (home or Path.home()) / "harnesses"
    if harnesses.is_dir():
        found += sorted(p for p in harnesses.iterdir() if p.is_dir())
    return [str(p).replace("\\", "/").rstrip("/") for p in found]


def _key(folder: str, windows: bool) -> str:
    s = str(folder).replace("\\", "/").rstrip("/")
    return to_windows(s) if windows else s


def trust(folders: list[str], config: Path, windows: bool = False,
          revoke: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Mark folders trusted (and `revoke` ones untrusted) in one config; returns the keys
    that changed, revoked ones prefixed with "-". Atomic write."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    projects = data.setdefault("projects", {})
    added = []
    for folder in revoke:
        key = _key(folder, windows)
        if (projects.get(key) or {}).get("hasTrustDialogAccepted") is True:
            projects[key]["hasTrustDialogAccepted"] = False
            added.append("-" + key)
    for folder in folders:
        key = _key(folder, windows)
        if windows and not (len(key) > 1 and key[1] == ":"):
            continue  # a WSL-only path means nothing to Windows Claude
        entry = projects.get(key)
        if entry is None:
            projects[key] = dict(ENTRY)
        elif entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
        else:
            continue
        added.append(key)
    if added:
        fd, tmp = tempfile.mkstemp(dir=config.parent, prefix=".claude.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, config.stat().st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, config)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return added


def trust_everywhere(folders: list[str], cfgs: list[tuple[Path, bool]] | None = None,
                     check: Callable[[str], tuple[bool, str]] = settings_match_main) -> dict[str, list[str]]:
    """Trust the folders that pass `vet` and revoke the agent worktrees that do not.
    Refused folders are reported under the key "refused" as "<folder>: <why>"."""
    ok, bad = vet(folders, check)
    out: dict[str, list[str]] = {}
    for config, windows in (cfgs if cfgs is not None else configs()):
        try:
            changed = trust(ok, config, windows, revoke=list(bad))
        except (OSError, ValueError) as exc:
            out[str(config)] = [f"error: {exc}"]
            continue
        if changed:
            out[str(config)] = changed
    if bad:
        out["refused"] = [f"{f}: {why}" for f, why in bad.items()]
    return out


def sweep(roots=DEFAULT_ROOTS, cfgs=None, check=settings_match_main) -> dict[str, list[str]]:
    return trust_everywhere(candidates(roots), cfgs, check)

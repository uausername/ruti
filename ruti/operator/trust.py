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
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .gc import to_windows

DEFAULT_ROOTS = ("/mnt/c/mycode",)
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


def trust(folders: list[str], config: Path, windows: bool = False) -> list[str]:
    """Mark folders trusted in one config; returns the keys that were added. Atomic write."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    projects = data.setdefault("projects", {})
    added = []
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


def trust_everywhere(folders: list[str], cfgs: list[tuple[Path, bool]] | None = None) -> dict[str, list[str]]:
    out = {}
    for config, windows in (cfgs if cfgs is not None else configs()):
        try:
            added = trust(folders, config, windows)
        except (OSError, ValueError) as exc:
            out[str(config)] = [f"error: {exc}"]
            continue
        if added:
            out[str(config)] = added
    return out


def sweep(roots=DEFAULT_ROOTS, cfgs=None) -> dict[str, list[str]]:
    return trust_everywhere(candidates(roots), cfgs)

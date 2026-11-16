"""One trusted copy of the HACCP Codex edit guard, outside every agent worktree.

Before: each worker worktree carried its own ``.codex/hooks.json`` pointing at its own
``scripts/agent-pilot/codex_guard.py``. Codex trusts a hook by its definition and path,
so every new worktree needed a person to trust it again; the helper script that did it
compared files against sha256 constants taken from a CRLF checkout; and the guard sat
inside the very worktree the agent could rewrite through the shell.

Now:

* ``sync`` copies ``codex_guard.py`` and ``protect_rules.py`` byte for byte from a git
  ref (``git show <commit>:<path>`` -- the committed LF bytes, no constants) into
  ``~/.local/share/ruti/codex-guard`` with a manifest, plus a small ``shim.py``;
* ``install_hook`` adds one ``PreToolUse`` entry to the *user* ``~/.codex/hooks.json``
  that runs the shim. A person trusts it once in Codex ``/hooks``; nothing here touches
  trust state;
* the shim finds the checkout from the hook payload's ``cwd`` and runs the trusted guard
  copy against it -- only for checkouts under the manifest's ``applies_to`` roots, so
  Codex sessions in other repositories are unaffected. Any failure denies the edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

GUARD_FILES = ("codex_guard.py", "protect_rules.py")
SOURCE_DIR = "scripts/agent-pilot/"
MATCHER = "^(apply_patch|Write|Edit)$"
LABEL = "HACCP: protected file guard (ruti operator, trusted copy)"

GitRunner = Callable[[list[str]], bytes]

SHIM = r'''"""ruti operator: run the trusted HACCP Codex guard copy against the session's checkout.

Written by `ruti operator guard sync`; do not edit. Fails closed: any error denies the edit.
"""
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_INPUT = 1024 * 1024


def deny(message):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": message}}))


def checkout(cwd):
    path = Path(cwd)
    for folder in [path, *path.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def under(path, roots):
    real = os.path.realpath(str(path))
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
        roots = [os.path.realpath(r) for r in manifest["applies_to"]]
        if len(raw) > MAX_INPUT:
            deny("HACCP guard: oversized editing request; split the patch.")
            return 0
        payload = json.loads(raw or b"{}")
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            deny("HACCP guard (shim): the hook payload has no absolute cwd; refused.")
            return 0
        root = checkout(cwd)
        if root is None or not under(root, roots):
            if under(cwd, roots):
                deny("HACCP guard (shim): no git checkout found for this session; refused.")
            return 0
        module = types.ModuleType("haccp_codex_guard")
        module.__file__ = str(HERE / "codex_guard.py")
        exec(compile((HERE / "codex_guard.py").read_bytes(), module.__file__, "exec"), module.__dict__)
        message = module.reason(payload, root=root)
        if message:
            deny(message)
    except BaseException as error:  # noqa: BLE001 -- fail closed
        deny(f"HACCP guard (shim) failed: {type(error).__name__}; refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def guard_dir() -> Path:
    override = os.environ.get("RUTI_GUARD_DIR")
    return Path(override) if override else Path.home() / ".local" / "share" / "ruti" / "codex-guard"


def _git(argv: list[str]) -> bytes:
    return subprocess.run(argv, capture_output=True, timeout=60, check=True).stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, 0o444)


def source(repo: str, ref: str, git: GitRunner = _git) -> tuple[str, dict[str, bytes]]:
    commit = git(["git", "-C", repo, "rev-parse", "--verify", ref + "^{commit}"]).decode().strip()
    files = {name: git(["git", "-C", repo, "show", f"{commit}:{SOURCE_DIR}{name}"]) for name in GUARD_FILES}
    return commit, files


def sync(repo: str, ref: str = "main", *, target: Path | None = None, git: GitRunner = _git) -> dict:
    """Copy the guard from ``repo@ref`` into the trusted folder; return the manifest."""
    target = target or guard_dir()
    target.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(target, 0o700)
    commit, files = source(repo, ref, git)
    for name, data in files.items():
        _write(target / name, data)
    shim = SHIM.encode("utf-8")
    _write(target / "shim.py", shim)
    manifest = {"repo": repo, "ref": ref, "commit": commit,
                "files": {name: _sha(data) for name, data in files.items()},
                "shim": _sha(shim), "applies_to": [os.path.realpath(repo)],
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _write(target / "manifest.json", json.dumps(manifest, indent=1).encode("utf-8"))
    return manifest


def hook_command(target: Path | None = None, python: str = "/usr/bin/python3") -> str:
    return shlex.join([python, "-I", "-X", "utf8", str((target or guard_dir()) / "shim.py")])


def codex_hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def plan_hook(current: dict, command: str) -> tuple[dict, bool]:
    """``current`` with the shim as PreToolUse group 0 (ruti's installer keeps foreign
    groups first and appends its own, so index 0 stays put across its reinstalls)."""
    hooks = current.setdefault("hooks", {})
    groups = hooks.setdefault("PreToolUse", [])
    if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
        return current, False
    groups.insert(0, {"matcher": MATCHER, "hooks": [
        {"type": "command", "command": command, "timeout": 5, "statusMessage": LABEL}]})
    return current, True


def install_hook(path: Path | None = None, command: str | None = None) -> dict:
    path = path or codex_hooks_path()
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    current = json.loads(before) if before.strip() else {}
    after, changed = plan_hook(current, command or hook_command())
    backup = None
    if changed:
        if before:
            backup = path.with_name(path.name + ".backup-" + time.strftime("%Y%m%dT%H%M%S"))
            backup.write_text(before, encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(after, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": str(path), "changed": changed, "backup": str(backup) if backup else None}


def status(repo: str | None = None, ref: str | None = None, *, target: Path | None = None,
           hooks: Path | None = None, git: GitRunner = _git) -> dict:
    """Is the trusted copy intact, current with ``ref``, and wired into Codex?"""
    target = target or guard_dir()
    out: dict = {"dir": str(target), "synced": False, "intact": False, "current": None,
                 "hook_installed": False}
    try:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out.update(synced=True, commit=manifest.get("commit"), ref=manifest.get("ref"))
    try:
        intact = all(_sha((target / n).read_bytes()) == h for n, h in manifest["files"].items())
        intact = intact and _sha((target / "shim.py").read_bytes()) == manifest["shim"]
    except (OSError, KeyError):
        intact = False
    out["intact"] = intact
    repo, ref = repo or manifest.get("repo"), ref or manifest.get("ref")
    try:
        _, files = source(repo, ref, git)
        out["current"] = {n: _sha(d) for n, d in files.items()} == manifest.get("files")
    except (subprocess.SubprocessError, OSError, TypeError):
        out["current"] = None
    try:
        data = json.loads((hooks or codex_hooks_path()).read_text(encoding="utf-8"))
        command = hook_command(target)
        out["hook_installed"] = any(h.get("command") == command
                                    for g in data.get("hooks", {}).get("PreToolUse", [])
                                    for h in g.get("hooks", []))
    except (OSError, ValueError):
        pass
    out["ok"] = bool(out["intact"] and out["current"] and out["hook_installed"])
    return out

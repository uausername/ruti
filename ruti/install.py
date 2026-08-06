"""Wiring ruti into Claude Code, reversibly.

The risky part is `settings.json`: corrupting it breaks Claude Code, which breaks the
manager, which stops all work -- precisely the failure this project exists to avoid.
So every change is previewed as a diff, the previous file is kept with a timestamp, and
existing keys (notably any hooks already registered) are merged rather than replaced.

The CLAUDE.md section is delimited by markers so a reinstall replaces it in place
instead of stacking another copy on top of the last one.
"""

from __future__ import annotations

import difflib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .config import REPO_ROOT

CLAUDE_HOME = Path.home() / ".claude"
SETTINGS = CLAUDE_HOME / "settings.json"
GLOBAL_MEMORY = CLAUDE_HOME / "CLAUDE.md"
AGENTS_DIR = CLAUDE_HOME / "agents"

REPO_AGENTS = REPO_ROOT / "claude" / "agents"
REPO_MEMORY = REPO_ROOT / "claude" / "CLAUDE.md"
REPO_OPENCODE = REPO_ROOT / "opencode" / "opencode.json"
LIVE_OPENCODE = Path.home() / ".config" / "opencode" / "opencode.json"

BEGIN = "<!-- ruti:begin -- managed by `ruti install`; edits here are overwritten -->"
END = "<!-- ruti:end -->"

# The heading the pre-2.0 setup used, so an upgrade replaces it instead of leaving a
# stale copy that contradicts the new one.
LEGACY_HEADING = "## Delegate implementation-heavy work to conserve Pro subscription quota"


def _python() -> str:
    """The interpreter to run hooks with.

    Recorded absolutely at install time: a hook inherits neither the shell's PATH nor
    any virtualenv, so `python` alone would resolve differently, or not at all.
    """
    return sys.executable


def _exec_hook(module: str, timeout: int) -> dict[str, Any]:
    # Exec form rather than a shell string: this machine's home directory contains a
    # space, and exec form removes the entire class of quoting bugs.
    return {
        "hooks": [
            {"type": "command", "command": _python(), "args": ["-m", module], "timeout": timeout}
        ]
    }


def desired_settings(current: dict[str, Any]) -> dict[str, Any]:
    updated = json.loads(json.dumps(current))  # deep copy

    updated["statusLine"] = {
        "type": "command",
        "command": _python(),
        "args": ["-m", "ruti.statusline"],
        "refreshInterval": 5,
    }

    hooks = updated.setdefault("hooks", {})
    # Replace only ruti's own entries; anything else registered here stays.
    for event, module, timeout in (
        ("UserPromptSubmit", "ruti.hooks.user_prompt_submit", 10),
        ("SessionStart", "ruti.hooks.session_start", 25),
    ):
        others = [
            entry for entry in hooks.get(event, [])
            if not any("ruti." in str(h.get("args", "")) for h in entry.get("hooks", []))
        ]
        hooks[event] = others + [_exec_hook(module, timeout)]
    return updated


def render_memory_section() -> str:
    body = REPO_MEMORY.read_text(encoding="utf-8")
    # Drop the install instructions at the top of the shipped file.
    lines = [line for line in body.splitlines() if not line.startswith("# Append to")
             and not line.startswith("# Without this")]
    return f"{BEGIN}\n" + "\n".join(lines).strip() + f"\n{END}\n"


def desired_memory(current: str) -> str:
    section = render_memory_section()

    if BEGIN in current and END in current:
        head, _, rest = current.partition(BEGIN)
        _, _, tail = rest.partition(END)
        return head + section + tail.lstrip("\n")

    # First install over the pre-2.0 text: cut the old section out by its heading so
    # the two do not sit side by side giving contradictory instructions.
    if LEGACY_HEADING in current:
        head, _, rest = current.partition(LEGACY_HEADING)
        remaining = rest.splitlines()
        cut = len(remaining)
        for index, line in enumerate(remaining):
            # The next top-level or sibling heading ends the legacy block.
            if line.startswith("## ") or line.startswith("# "):
                cut = index
                break
        tail = "\n".join(remaining[cut:])
        current = head.rstrip() + ("\n\n" + tail.lstrip() if tail.strip() else "\n")

    separator = "" if current.endswith("\n\n") or not current.strip() else "\n"
    return current.rstrip() + "\n\n" + section if current.strip() else section


def diff(label: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"{label} (current)", tofile=f"{label} (after install)", n=2,
        )
    )


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = path.with_name(f"{path.name}.ruti-backup-{stamp}")
    shutil.copyfile(path, destination)
    return destination


def plan() -> list[tuple[str, Path, str, str]]:
    """(label, path, current, desired) for everything install would touch."""
    changes: list[tuple[str, Path, str, str]] = []

    current_settings = SETTINGS.read_text(encoding="utf-8") if SETTINGS.exists() else "{}"
    try:
        parsed = json.loads(current_settings)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{SETTINGS} is not valid JSON ({exc}); refusing to touch it. Fix it by hand first."
        ) from exc
    after = json.dumps(desired_settings(parsed), indent=2, ensure_ascii=False) + "\n"
    before = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
    if before != after:
        changes.append(("settings.json", SETTINGS, before, after))

    memory_before = GLOBAL_MEMORY.read_text(encoding="utf-8") if GLOBAL_MEMORY.exists() else ""
    memory_after = desired_memory(memory_before)
    if memory_before != memory_after:
        changes.append(("CLAUDE.md", GLOBAL_MEMORY, memory_before, memory_after))

    for source in sorted(REPO_AGENTS.glob("*.md")):
        target = AGENTS_DIR / source.name
        target_text = target.read_text(encoding="utf-8") if target.exists() else ""
        source_text = source.read_text(encoding="utf-8")
        if target_text != source_text:
            changes.append((f"agents/{source.name}", target, target_text, source_text))

    live_text = LIVE_OPENCODE.read_text(encoding="utf-8") if LIVE_OPENCODE.exists() else ""
    repo_text = REPO_OPENCODE.read_text(encoding="utf-8")
    if live_text != repo_text:
        changes.append(("opencode.json", LIVE_OPENCODE, live_text, repo_text))

    return changes


def apply(changes: list[tuple[str, Path, str, str]]) -> list[str]:
    notes = []
    for label, path, _, after in changes:
        saved = backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
        notes.append(f"{label} -> {path}" + (f" (previous kept as {saved.name})" if saved else ""))
    return notes

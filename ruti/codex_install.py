"""Pure planning for Codex native hooks; writes belong to install.apply."""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
from pathlib import Path
import re
import shlex
import sys

_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_ARGS = " -I -X utf8 -m ruti.hooks.codex"


def _command(interpreter: str, platform: str) -> str:
    if not interpreter or any(c in interpreter for c in "\r\n\0"):
        raise ValueError("Interpreter must be an absolute, single-line path")
    if platform == "win32":
        # cmd expands percent variables even inside quotes. Reject shell syntax
        # rather than attempting to escape a path through several parsing layers.
        if any(c in interpreter for c in '\"% !&|<>^`\t'.replace(' ', '')):
            raise ValueError("Unsafe Windows interpreter path")
        path = interpreter.replace("\\", "/")
        if not (Path(path).is_absolute() or (ntpath.isabs(path) and ntpath.splitdrive(path)[0])):
            raise ValueError("Interpreter must be absolute")
        if path.startswith("//") or any(part in (".", "..") for part in path.split("/")):
            raise ValueError("Unsafe Windows interpreter path")
        return f'"{path}"{_ARGS}'
    if not posixpath.isabs(interpreter):
        raise ValueError("Interpreter must be absolute")
    return shlex.quote(interpreter) + _ARGS


def _managed(handler: dict, event: str) -> bool:
    if handler.get("type") != "command" or handler.get("statusMessage") != f"Ruti Codex: {event}":
        return False
    command = handler.get("command")
    if not isinstance(command, str) or not command.endswith(_ARGS):
        return False
    executable = command[:-len(_ARGS)]
    # Recognize only complete invocations in one of the formats we emit. In
    # particular, an echo, wrapper, or appended shell command is never owned.
    try:
        tokens = shlex.split(executable)
        if len(tokens) == 1 and _command(tokens[0], "linux") == command:
            return True
    except ValueError:
        pass
    if re.fullmatch(r'"[^"\r\n]+"', executable):
        try:
            return _command(executable[1:-1], "win32") == command
        except ValueError:
            pass
    return False


def _validate(current: object) -> None:
    if not isinstance(current, dict):
        raise ValueError("Codex hooks JSON must be an object")
    hooks = current.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"{event} groups must be a list")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"{event} group must contain a hooks list")
            if "matcher" in group and not isinstance(group["matcher"], str):
                raise ValueError(f"{event} matcher must be a string")
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
                    raise ValueError(f"{event} handler must be an object with a type")
                if handler["type"] == "command" and not isinstance(handler.get("command"), str):
                    raise ValueError(f"{event} command must be a string")


def plan(hooks_path=None, interpreter=None, platform=None) -> list[tuple[str, Path, str, str]]:
    """Return installer changes without creating directories or writing files."""
    path = Path(hooks_path) if hooks_path is not None else Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ) / "hooks.json"
    command = _command(str(sys.executable if interpreter is None else interpreter),
                       sys.platform if platform is None else platform)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    current = json.loads(before) if before else {}
    if before == "" and path.exists():
        raise ValueError("Codex hooks file is empty, not valid JSON")
    _validate(current)
    hooks = current.setdefault("hooks", {})
    for event in _EVENTS:
        groups = []
        for group in hooks.get(event, []):
            remaining = [h for h in group["hooks"] if not _managed(h, event)]
            if remaining or not group["hooks"]:
                groups.append({**group, "hooks": remaining})
        groups.append({"hooks": [{
            "type": "command", "command": command,
            "timeout": 19800 if event == "Stop" else 5,
            "statusMessage": f"Ruti Codex: {event}", "additionalContextLimit": 5000,
        }]})
        hooks[event] = groups
    after = json.dumps(current, indent=2, ensure_ascii=False) + "\n"
    return [("Codex hooks.json", path, before, after)] if before != after else []

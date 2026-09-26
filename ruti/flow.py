"""Flow mode: hand a long task to a fresh session before this one's context fills.

A conversation past half its context window still works, but the room left to act on
anything shrinks, and a summary written at 90% under pressure is worse than one written
with room to spare. `/compact` keeps the session and drops whatever the summariser chose
not to keep; flow keeps exactly what the manager itself writes down, and starts the next
session from that.

Hook-driven, like wait mode, so none of it depends on the manager remembering a rule it
read an hour ago:

* **50%** -- `PostToolUse` injects one notice: finish the step in hand, write a handoff
  with `ruti flow handoff`, end the turn.
* **60% without a handoff** -- `PreToolUse` refuses every tool except `ruti` and the task
  list, so the notice cannot be scrolled past indefinitely. Once a handoff exists every
  other tool is refused too: the one thing left to do is end the turn, and two sessions
  must never work the same task at once.
* **Stop** -- the hook writes a PowerShell launcher and opens it in a new Windows
  Terminal window: `claude` in the same directory, in the same permission mode, with
  `RUTI_FLOW_HANDOFF` naming the handoff. The old window stays open, idle.
* **SessionStart** in the new process sees the variable, gives the new session the old
  one's modes, and puts the handoff into its context.

The permission mode is carried over because the user chose that: a long autonomous task
should not stall at the first prompt of the window it moved to. `MAX_HOPS` bounds the
chain -- a flow that keeps handing off without finishing is a loop, not progress, and
every hop spends the same five-hour window.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import context_watch, modes, wait
from .config import STATE_ROOT, read_json, write_json

FLOW_AT = context_watch.WARN_PERCENT
FORCE_AT = 60.0
MAX_HOPS = 5

FLOW_DIR = STATE_ROOT / "flow"
HANDOFF_ENV = "RUTI_FLOW_HANDOFF"
KEEP_SECONDS = 14 * 86400

# What `claude --permission-mode` accepts, checked against `claude --help` on this
# machine. Anything else -- "default", or a mode a later version adds -- is left out
# rather than passed to a CLI that would refuse to start over it.
PERMISSION_MODES = frozenset({"acceptEdits", "auto", "bypassPermissions", "manual",
                              "dontAsk", "plan"})

SECTIONS = ("Goal; Done; In progress (exactly where you stopped); Next steps, in order; "
            "Key files, commands and state; Decisions and constraints; Instructions to "
            "your next self")

# The handoff has to get through the same gate that refuses everything else, and a
# heredoc is exactly what `wait._ruti_only` rejects as chaining. So this one shape is let
# through whole: `ruti flow handoff`, one quoted heredoc, and nothing after its end.
_BASH_HANDOFF = re.compile(
    r"ruti flow handoff\s*<<-?\s*'(?P<tag>\w+)'\n.*\n(?P=tag)\s*", re.DOTALL)
_PWSH_HANDOFF = re.compile(r"@'\r?\n.*\r?\n'@\s*\|\s*ruti flow handoff\s*", re.DOTALL)


def _notice(used: float) -> str:
    return (
        f"ruti flow: {used:.0f}% of this conversation's context window is used. Finish "
        "the step you are on (start nothing large), then hand off -- `ruti flow handoff "
        "<<'EOF'` ... `EOF` -- with these sections: "
        f"{SECTIONS}. Then end your turn: a fresh session opens in a new window with the "
        "handoff and this session's modes, and continues."
    )


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allowed(payload: dict[str, Any]) -> bool:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if wait.tool_allowed_while_paused(tool_name, tool_input):
        return True
    if tool_name in wait.SHELL_TOOLS and isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "").strip()
        return bool(_BASH_HANDOFF.fullmatch(command) or _PWSH_HANDOFF.fullmatch(command))
    return False


def post_tool_use(session_id: str) -> dict[str, Any] | None:
    """Once per session, at the line: finish the step, hand off, end the turn."""
    used = context_watch.used(session_id)
    if used is None or used < FLOW_AT:
        return None
    state = modes.flow_state(session_id)
    if state.get("noticed") or state.get("handoff"):
        return None
    modes.set_flow_state(session_id, {**state, "noticed": True})
    return {
        "hookSpecificOutput": {"hookEventName": "PostToolUse",
                               "additionalContext": _notice(used)}
    }


def pre_tool_use(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse tools once handed off, or past the force line with no handoff yet."""
    if modes.flow_state(session_id).get("handoff"):
        reason = ("ruti flow: the handoff is written -- end your turn now; the next "
                  "session continues from it. (`ruti mode flow off` releases this "
                  "session and drops a handoff not yet launched.)")
    else:
        used = context_watch.used(session_id)
        if used is None or used < FORCE_AT:
            return None
        reason = (_notice(used) + f" Past {FORCE_AT:.0f}%, tools other than `ruti` are "
                  "refused until the handoff is written.")
    if _allowed(payload):
        return None
    return _deny(reason)


def _prune(now: float) -> None:
    try:
        for path in FLOW_DIR.iterdir():
            if now - path.stat().st_mtime > KEEP_SECONDS:
                path.unlink()
    except OSError:
        pass


def write_handoff(session_id: str, text: str, cwd: str) -> Path:
    """Save the handoff and what the next session needs besides it."""
    text = (text or "").strip()
    if not text:
        raise ValueError("the handoff is empty -- nothing for the next session to go on")
    state = modes.flow_state(session_id)
    if state.get("launched"):
        raise ValueError("this session already handed off to a new one -- "
                         "`ruti mode flow off`, then on, to start over")

    now = time.time()
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    _prune(now)
    path = FLOW_DIR / f"{session_id}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.md"
    path.write_text(text + "\n", encoding="utf-8")
    write_json(path.with_suffix(".json"), {
        "from_session": session_id,
        "cwd": cwd,
        "hop": int(state.get("hop") or 0) + 1,
        "modes": modes.current(session_id),
        "written_at": now,
    })
    modes.set_flow_state(session_id, {**state, "handoff": str(path), "launched": False})
    return path


def stop(session_id: str, payload: dict[str, Any], *,
         spawn: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Open the next session once a handoff exists. Never blocks the stop."""
    state = modes.flow_state(session_id)
    handoff = state.get("handoff")
    if not handoff or state.get("launched"):
        return None
    path = Path(handoff)
    meta = read_json(path.with_suffix(".json"), default=None)
    meta = meta if isinstance(meta, dict) else {}
    hop = int(meta.get("hop") or 1)

    # Marked before launching, not after: a launch that half-worked and a retry on the
    # next stop would put two sessions on the same task.
    modes.set_flow_state(session_id, {**state, "launched": True})
    if hop > MAX_HOPS:
        return {"systemMessage": f"ruti flow: chain limit ({MAX_HOPS} sessions) reached -- "
                                 f"not opening another. The handoff is at {path}."}

    cwd = str(meta.get("cwd") or payload.get("cwd") or os.getcwd())
    try:
        launch(path, cwd, payload.get("permission_mode"), spawn=spawn or subprocess.Popen)
    except Exception as exc:
        return {"systemMessage": f"ruti flow: could not open a new session ({exc}). The "
                                 f"handoff is at {path} -- start `claude` in {cwd} and ask "
                                 "it to continue from that file."}
    return {"systemMessage": f"ruti flow: handed off -- a new session is opening in a new "
                             f"window (hop {hop} of {MAX_HOPS}). This window can be closed."}


def _ps_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def launcher_script(handoff: Path, cwd: str, permission_mode: str | None,
                    claude: str) -> str:
    args = ["--permission-mode", permission_mode] if permission_mode in PERMISSION_MODES else []
    prompt = f"Continue the task from the ruti flow handoff in your context (file: {handoff})."
    return "\r\n".join([
        f"Set-Location -LiteralPath {_ps_quote(cwd)}",
        f"$env:{HANDOFF_ENV} = {_ps_quote(handoff)}",
        # Belt and braces: a new window does not inherit it, but a child of this hook
        # would, and a second session must never act under the first one's id.
        "Remove-Item Env:CLAUDE_CODE_SESSION_ID -ErrorAction SilentlyContinue",
        "& " + " ".join(_ps_quote(a) for a in [claude, *args, prompt]),
    ]) + "\r\n"


def launch(handoff: Path, cwd: str, permission_mode: str | None, *,
           spawn: Callable[..., Any] = subprocess.Popen) -> list[str]:
    """Open `claude` on the handoff in a new window; returns the argv it spawned."""
    script = handoff.with_suffix(".ps1")
    # With a BOM: Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page,
    # and a Cyrillic directory name would arrive mangled.
    script.write_text(
        launcher_script(handoff, cwd, permission_mode, shutil.which("claude") or "claude"),
        encoding="utf-8-sig",
    )
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    shell_argv = [shell, "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    common: dict[str, Any] = {
        "env": {k: v for k, v in os.environ.items() if k.upper() != "CLAUDE_CODE_SESSION_ID"},
        "cwd": cwd if os.path.isdir(cwd) else None,
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True,
    }

    terminal = shutil.which("wt")
    if terminal:
        argv = [terminal, "-w", "new", "--title", "claude flow", *shell_argv]
        spawn(argv, **common)
        return argv

    # No Windows Terminal: a console of its own. Breaking away from the hook's job keeps
    # the window alive after the hook exits; a job that forbids it raises, so retry
    # without rather than not opening anything.
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        spawn(shell_argv, creationflags=new_console | breakaway, **common)
    except OSError:
        spawn(shell_argv, creationflags=new_console, **common)
    return shell_argv


def session_start(payload: dict[str, Any],
                  env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """In the new session: take over the old one's modes and read its handoff.

    Only on `startup`. The variable lives as long as the process, and SessionStart fires
    again on /clear, compaction and resume -- none of which is a new hop.
    """
    env = os.environ if env is None else env
    session_id = payload.get("session_id")
    handoff = env.get(HANDOFF_ENV)
    if payload.get("source") != "startup" or not session_id or not handoff:
        return None
    path = Path(handoff)
    meta = read_json(path.with_suffix(".json"), default=None)
    if not path.is_file() or not isinstance(meta, dict):
        return None
    text = path.read_text(encoding="utf-8").strip()
    hop = int(meta.get("hop") or 1)

    saved = meta.get("modes")
    modes.apply(session_id, saved if isinstance(saved, dict) else {})
    modes.set_flow_state(session_id, {"hop": hop, "noticed": False, "handoff": None,
                                      "launched": False})
    origin = str(meta.get("from_session") or "?")[:8]
    return (
        f"ruti flow: continuing from session {origin} (hop {hop} of {MAX_HOPS})",
        f"ruti flow: this session continues a task handed off by a previous session (hop "
        f"{hop} of {MAX_HOPS}). The handoff follows -- treat it as your own notes, re-read "
        "the files it names before editing them, and carry on with the next step:\n\n"
        + text,
    )


def prompt_note(session_id: str | None) -> str:
    """The line the prompt hook adds while flow mode is on."""
    state = modes.flow_state(session_id)
    if state.get("handoff"):
        return (f"ruti flow: this session has handed off ({state['handoff']}) -- tools are "
                "refused here; `ruti mode flow off` releases it.")
    hop = int(state.get("hop") or 0)
    return (f"ruti flow mode is ON (hop {hop} of {MAX_HOPS}): at {FLOW_AT:.0f}% context you "
            "will be asked to hand off with `ruti flow handoff`; a fresh session then "
            "continues in a new window.")

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
every hop spends the same five-hour window. In manager mode the model and effort go with
it, which is the one moment in ruti where a model switch costs nothing: the context has
not been read yet.

The new session is named after the work it is about to do, which the handoff says in
its own first section: the manager writes `Title:` knowing what comes next. The old
session's title cannot say that -- it is generated from how that session began -- but a
name the user set themselves with `/rename` still wins, and that name is read out of the
old session's transcript, the JSONL file whose path the Stop hook is handed in
`transcript_path`; so is Remote Control, which the user turned on with
`/remote-control`. A fresh session would otherwise take its name from the continuation
prompt, so every flow session would be called the same thing, and Remote Control would
silently be off in the window that takes the task over.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import context_watch, manager, modes, wait
from .config import STATE_ROOT, read_json, write_json

FLOW_AT = context_watch.WARN_PERCENT
FORCE_AT = 60.0
MAX_HOPS = 5

FLOW_DIR = STATE_ROOT / "flow"
HANDOFF_ENV = "RUTI_FLOW_HANDOFF"
KEEP_SECONDS = 14 * 86400

# Where Claude Code keeps per-folder state, trust among it.
CLAUDE_CONFIG = Path.home() / ".claude.json"

# Claude Code's per-process markers outside the `CLAUDE_CODE_` prefix, as found in the
# environment of its children on this machine.
_MARKER_NAMES = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})

# What `claude --permission-mode` accepts, checked against `claude --help` on this
# machine. Anything else -- "default", or a mode a later version adds -- is left out
# rather than passed to a CLI that would refuse to start over it.
PERMISSION_MODES = frozenset({"acceptEdits", "auto", "bypassPermissions", "manual",
                              "dontAsk", "plan"})

TITLE_MAX = 80
# Stripped from a carried-over title before the next hop's own suffix goes on, so hop 3
# of a chain does not end up called "fix auth (flow 2/5) (flow 3/5)".
_FLOW_SUFFIX = re.compile(r"\s*\(flow \d+/\d+\)\s*$")
# A title from the handoff is meant to be one narrow phrase, so it is cut here before the
# suffix goes on rather than being left to the overall cap with a half-word at the end.
HANDOFF_TITLE_MAX = 60

# Markdown punctuation at the edge of a heading or list item, which says nothing about
# what the line is for.
_MD_MARKERS = "#*_>`- \t"
_WHITESPACE = re.compile(r"\s+")
_GOAL_HEAD = re.compile(r"goal(?![0-9a-z])", re.IGNORECASE)
# What a heading may put between its word and its text: "**Goal:** Ship it", "-- Goal --".
# Dashes count as separators because a goal is prose, not a number.
_GOAL_GAP = ":\u2013\u2014- \t"
# The word boundary is what keeps "Titles are ..." from being read as a title, and the
# separator is required: "Title of the session: ..." is prose, not the title itself.
_TITLE_HEAD = re.compile(r"title(?![0-9a-z])[ \t]*[:\u2013\u2014-]", re.IGNORECASE)
# What a title may be wrapped in, which is the model's styling rather than its name.
_WRAPPERS = "\"'`\u2018\u2019\u201c\u201d \t"
_LEAD_MD = "*_`# \t"

# Title first, because it is read back out of the written handoff and becomes the name of
# the session that continues: what that session will do, which the old session's own title
# cannot say -- it says how this one began.
SECTIONS = ("Title (4-8 words naming the specific next piece of work -- narrow, not the "
            "project, not how this session began; e.g. `Title: fix flow session names`); "
            "Goal; Done; In progress (exactly where you stopped); Next steps, in order; "
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


def transcript_facts(path: str | None) -> dict[str, Any]:
    """{"remote_control": bool, "title": str | None, "custom_title": str | None}. Never
    raises; a missing or unreadable file gives all three at their empty values.

    Claude Code records these facts in the session's own transcript, one JSON object per
    line, and a transcript is written continuously and can be several MB -- so it is read
    line by line, and any line that will not parse (Claude Code writes those too) is
    passed over. The last title wins because `/rename` writes a new record, and the last
    bridge status wins because a disconnect is logged as one.

    `title` is what the session is called: the user's own name if there is one, else the
    generated one, which describes how the session began. `custom_title` is only that
    user's own name, kept apart because a name a previous flow hop put on with `--name`
    is not the user's -- it is the last hop's handoff title, which says nothing about the
    work this one is about to do.
    """
    facts: dict[str, Any] = {"remote_control": False, "title": None, "custom_title": None}
    if not path:
        return facts
    custom = auto = None
    bridged = False
    status: bool | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "custom-title":
                    # Only a name that is there: a record with an empty one says the
                    # session was renamed back to nothing, not that the earlier name goes.
                    named = str(record.get("customTitle") or "").strip()
                    if named:
                        custom = named
                elif kind == "ai-title":
                    auto = record.get("aiTitle")
                elif kind == "bridge-session":
                    # Written every turn while the bridge is up: proof of it, but no proof
                    # that it is still up.
                    bridged = True
                elif kind == "system" and record.get("subtype") == "bridge_status":
                    # A disconnect is a status too, so the last one decides; a file that
                    # only ever got per-turn markers falls back to them.
                    status = "is active" in str(record.get("content") or "").lower()
    except OSError:
        return facts

    facts["custom_title"] = custom
    for candidate in (custom, auto):
        title = str(candidate or "").strip()
        if title:
            facts["title"] = title
            break
    facts["remote_control"] = bridged if status is None else status
    return facts


def handoff_goal(text: str) -> str | None:
    """The handoff's Goal, read out of the markdown the model wrote. None without one.

    A title has to be one line, and the handoff is free prose under seven headings, so
    this is a heading scan rather than a parse: the first line that says "goal" and the
    text after it, or the first thing under the heading when the heading stands alone.
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        plain = line.strip().strip(_MD_MARKERS)
        head = _GOAL_HEAD.match(plain)
        if not head:
            continue
        rest = plain[head.end():]
        goal = _WHITESPACE.sub(" ", rest.lstrip(_GOAL_GAP).lstrip(_LEAD_MD)).strip()
        if not goal:
            for follow in lines[index + 1:]:
                goal = _WHITESPACE.sub(" ", follow.strip(_MD_MARKERS)).strip()
                if goal:
                    break
        return goal or None
    return None


def handoff_title(text: str) -> str | None:
    """The handoff's Title line, read out of the markdown the model wrote. None without one.

    Like `handoff_goal`, a heading scan rather than a parse: markdown markers off both
    ends, the word "title" with a separator after it, and the rest of that same line.
    Unlike the goal, there is no look-ahead -- a title has to be on the line that says so
    or the next heading's text would be read as the name of the session.
    """
    for line in str(text or "").splitlines():
        plain = line.strip().strip(_MD_MARKERS)
        head = _TITLE_HEAD.match(plain)
        if not head:
            continue
        rest = _WHITESPACE.sub(" ", plain[head.end():].lstrip(_LEAD_MD)).strip()
        return rest.strip(_WRAPPERS) or None
    return None


def _word_cut(value: str, limit: int) -> str:
    """`value` shortened to `limit` at a whole word, not mid-word."""
    if len(value) <= limit:
        return value
    head = value[:limit]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip()


def session_title(previous: str | None, handoff_text: str, hop: int,
                  custom: str | None = None) -> str:
    """What `--name` to open the next session under: the narrowest description of the work
    there will be, from the handoff's Title, its Goal, or the old session's own name.

    What the next session will do comes first, because that is what a name is for, and the
    manager writing the handoff is the only one who knows it. The old session's title is
    last before the fallback: it is generated from how that session *began*, so it
    describes work already done. A custom title is the user's own and outranks all of it
    -- unless it carries a hop's suffix, which means the previous hop put it there with
    `--name` and it would stick to every hop after that, naming none of them.

    The hop number is in the title because the window says nothing else about where it
    sits in the chain, and the previous session's own suffix is removed first so the
    count cannot stack across hops.
    """
    base = ""
    if custom:
        own = _WHITESPACE.sub(" ", str(custom)).strip()
        # A name with a hop's suffix on it was written by this machine, not by the user.
        if own and not _FLOW_SUFFIX.search(own):
            base = own
    if not base:
        title = handoff_title(handoff_text)
        if title:
            base = _word_cut(title, HANDOFF_TITLE_MAX)
    if not base:
        base = handoff_goal(handoff_text) or ""
    if not base and previous:
        base = _WHITESPACE.sub(" ", _FLOW_SUFFIX.sub("", str(previous))).strip()
    if not base:
        base = "ruti flow"
    suffix = f" (flow {hop}/{MAX_HOPS})"
    if len(base) + len(suffix) > TITLE_MAX:
        base = base[: TITLE_MAX - len(suffix) - 1].rstrip() + "\u2026"
    return base + suffix


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
    # The one place the next session's seat is chosen without asking: a handoff is the
    # clearest description of the work there will be, and a new session is the one
    # moment a model switch is free. Anything that goes wrong -- mode off, no
    # classifier, a band with nothing eligible -- launches exactly as it did before.
    seat = _seat_for(session_id, path)
    # Remote Control, and any name the user set themselves, are what was set up in the
    # session being left, so reading them is part of opening the continuation; the rest of
    # the name comes from the handoff, which describes the work better. A naming problem
    # must not cost the continuation itself: anything unreadable here just launches
    # unnamed, without Remote Control, exactly as it did before.
    title: str | None = None
    remote_control = False
    try:
        facts = transcript_facts(payload.get("transcript_path"))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        title = session_title(facts.get("title"), text, hop,
                              custom=facts.get("custom_title"))
        remote_control = bool(facts.get("remote_control"))
    except Exception:
        title, remote_control = None, False
    try:
        launch(path, cwd, payload.get("permission_mode"), spawn=spawn or subprocess.Popen,
               seat_args=seat.cli_args() if seat else None, name=title,
               remote_control=remote_control)
    except Exception as exc:
        return {"systemMessage": f"ruti flow: could not open a new session ({exc}). The "
                                 f"handoff is at {path} -- start `claude` in {cwd} and ask "
                                 "it to continue from that file."}
    message = (f"ruti flow: handed off -- a new session is opening in a new window (hop "
               f"{hop} of {MAX_HOPS}). This window can be closed.")
    if seat is not None:
        message += f" on {seat.label()}"
    if title:
        message += f' Named "{title}".'
    if remote_control:
        message += " Remote Control is on there too."
    if trusted(cwd) is False:
        message += " " + untrusted_note(cwd)
    return {"systemMessage": message}


def _seat_for(session_id: str, handoff: Path) -> manager.Seat | None:
    """The seat the handoff says the next session should open on, or None.

    Never raises: a launch that failed over a seat recommendation would cost the user
    the continuation itself, which is the one thing this hook exists to hand over.
    """
    try:
        if not modes.current(session_id).get("manager"):
            return None
        return manager.seat_for_handoff(handoff.read_text(encoding="utf-8"), session_id)
    except Exception:
        return None


def trusted(cwd: str) -> bool | None:
    """Has Claude Code recorded `cwd` as trusted? None when it cannot tell.

    An untrusted folder does not stop the launch -- the new window opens on Claude
    Code's trust question, and SessionStart, so the handoff, waits behind it. Measured
    on this machine: the home folder is recorded with the question unanswered, however
    often it has been accepted, while project folders keep the answer.
    """
    data = read_json(CLAUDE_CONFIG, default=None)
    if not isinstance(data, dict):
        return None
    want = os.path.normcase(os.path.normpath(cwd))
    for path, project in (data.get("projects") or {}).items():
        if isinstance(project, dict) and os.path.normcase(os.path.normpath(path)) == want:
            return bool(project.get("hasTrustDialogAccepted"))
    return False


def untrusted_note(cwd: str) -> str:
    return (f"Claude Code has not recorded {cwd} as trusted, so the new window first asks "
            "whether to trust the folder -- answer there, or the handoff will not start.")


def session_markers(env: Mapping[str, str] | None = None,
                    persistent: set[str] | None = None) -> list[str]:
    """What Claude Code set in this process for its own children, to leave behind.

    The hook that opens the next session is such a child, and whatever it hands down
    makes the new session believe it is nested inside the old one: seen on this
    machine, an inherited `CLAUDE_CODE_CHILD_SESSION` switched the new session's
    transcript off, so `/resume` would never have found it. `CLAUDE_CODE_MESSAGING_TOKEN`
    is the old session's credential and has no business in another process at all.
    A `CLAUDE_CODE_*` variable the user set in Windows itself is configuration, not a
    marker, and is kept.
    """
    env = os.environ if env is None else env
    keep = _persistent_env_names() if persistent is None else {n.upper() for n in persistent}
    return sorted(
        name for name in env
        if (name.upper() in _MARKER_NAMES or name.upper().startswith("CLAUDE_CODE_"))
        and name.upper() not in keep
    )


def _persistent_env_names() -> set[str]:
    try:
        import winreg
    except ImportError:  # not Windows: nothing set "in Windows itself" to keep
        return set()
    names: set[str] = set()
    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as key:
                index = 0
                while True:
                    try:
                        names.add(winreg.EnumValue(key, index)[0].upper())
                    except OSError:
                        break
                    index += 1
        except OSError:
            continue
    return names


def _ps_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def launcher_script(handoff: Path, cwd: str, permission_mode: str | None,
                    claude: str, drop: list[str] | None = None,
                    seat_args: list[str] | None = None,
                    name: str | None = None, remote_control: bool = False) -> str:
    args = ["--permission-mode", permission_mode] if permission_mode in PERMISSION_MODES else []
    # After the permission mode, because it is the same kind of thing -- what the user
    # chose for this session, carried into the next one -- and the seat is chosen for it.
    args += list(seat_args or [])
    if name:
        args += ["--name", name]
    if remote_control:
        # Always with a value: `--remote-control` takes an optional one, so bare it would
        # make claude read the prompt that follows as the Remote Control session's name.
        args += ["--remote-control", name or "ruti flow"]
    prompt = f"Continue the task from the ruti flow handoff in your context (file: {handoff})."
    # Removed here as well as from the spawn's environment: a window Windows Terminal
    # opens is not guaranteed to take the environment it was asked with.
    names = sorted({"CLAUDE_CODE_SESSION_ID", *(drop or [])})
    return "\n".join([
        f"Set-Location -LiteralPath {_ps_quote(cwd)}",
        f"$env:{HANDOFF_ENV} = {_ps_quote(handoff)}",
        *(f"Remove-Item Env:{name} -ErrorAction SilentlyContinue" for name in names),
        "& " + " ".join(_ps_quote(a) for a in [claude, *args, prompt]),
        # claude's own exit code, so the terminal can tell a finished session (window
        # closes) from a failed start (window stays, showing the code).
        "exit $LASTEXITCODE",
    ]) + "\n"


def launch(handoff: Path, cwd: str, permission_mode: str | None, *,
           spawn: Callable[..., Any] = subprocess.Popen,
           seat_args: list[str] | None = None,
           name: str | None = None, remote_control: bool = False) -> list[str]:
    """Open `claude` on the handoff in a new window; returns the argv it spawned."""
    script = handoff.with_suffix(".ps1")
    markers = session_markers()
    # With a BOM: Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page,
    # and a Cyrillic directory name would arrive mangled.
    script.write_text(
        launcher_script(handoff, cwd, permission_mode, shutil.which("claude") or "claude",
                        drop=markers, seat_args=seat_args, name=name,
                        remote_control=remote_control),
        encoding="utf-8-sig", newline="\r\n",
    )
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    # No -NoExit: with it every finished continuation left a dead shell behind, one
    # window per hop. Windows Terminal closes a tab whose process exits 0 and keeps one
    # that failed, so a launch that went wrong still stays readable.
    shell_argv = [shell, "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    dropped = {name.upper() for name in [*markers, "CLAUDE_CODE_SESSION_ID"]}
    common: dict[str, Any] = {
        "env": {k: v for k, v in os.environ.items() if k.upper() not in dropped},
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

"""The only place ruti spawns an external process.

Everything funnels through `run()` so three disciplines are structural rather than
remembered at each call site:

* **stdin is always closed.** `lms unload` with no identifier and a number of loaded
  models other than one drops into an interactive prompt and waits forever; a hook or
  status line that blocks freezes the Claude Code UI. A child that reads EOF dies
  instead of hanging.
* **a timeout is always set.** `opencode run` has no timeout flag of its own, so the
  parent has to impose one or a stuck delegate wedges the whole production loop.
* **executables are resolved once, absolutely.** PATH lookups differ between the
  scheduled task, the Claude Code hook environment, and an interactive shell.

Output is decoded defensively: this runs on a Windows box whose console codepage is
cp1251 while most of these tools emit UTF-8.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60.0

# Progress spinners and ANSI colour survive redirection in several of these tools and
# turn a two-line result into hundreds of lines. Strip them before anything sees them.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_SPINNER = re.compile(r"[⠀-⣿]")

# Windows: don't flash a console window when spawned from a GUI-less context.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ToolNotFound(RuntimeError):
    """A required external executable is not installed or not on PATH."""


class ToolTimeout(RuntimeError):
    """The child process outlived its timeout and was killed.

    Carries whatever the child had written before it was killed: on Windows,
    `subprocess.run` drains the pipes after killing the process and attaches them to
    the exception, so this is real output, not a guess -- and it is often the only clue
    to what the process was doing (e.g. still indexing the repo) when it was cut off.
    """

    def __init__(self, message: str, *, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "Result":
        if not self.ok:
            detail = (self.stderr or self.stdout).strip()
            raise RuntimeError(
                f"{self.argv[0]} exited {self.returncode}: {detail[:400] or '(no output)'}"
            )
        return self


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "cp866"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def clean(text: str) -> str:
    """Strip ANSI escapes, spinner frames, and carriage-return progress rewrites."""
    text = _ANSI.sub("", text)
    text = _SPINNER.sub("", text)
    # Normalise CRLF first. Otherwise every line on Windows ends in a bare \r and the
    # progress-rewrite handling below discards all of them as superseded.
    text = text.replace("\r\n", "\n")
    # A progress bar rewrites one line with \r; only the final state is meaningful.
    lines = [segment.split("\r")[-1] for segment in text.split("\n")]
    return "\n".join(lines)


_resolved: dict[str, str] = {}


def resolve(exe: str, extra_dirs: tuple[Path, ...] = ()) -> str:
    """Find an executable once and cache it. Raises ToolNotFound if absent."""
    if exe in _resolved:
        return _resolved[exe]

    found = shutil.which(exe)
    if not found:
        for directory in extra_dirs:
            for suffix in ("", ".exe", ".cmd", ".bat"):
                candidate = directory / f"{exe}{suffix}"
                if candidate.is_file():
                    found = str(candidate)
                    break
            if found:
                break
    if not found:
        raise ToolNotFound(f"{exe!r} is not installed or not on PATH")

    _resolved[exe] = found
    return found


def _kill_tree(process: subprocess.Popen) -> tuple[bytes, bytes]:
    """Kill a process and everything it spawned, then drain what it had written.

    `Popen.kill()` only signals the direct child. `opencode` spawns its own
    subprocesses (node, in practice), and those keep running after the parent dies --
    still holding the stdout/stderr pipe open, which is exactly what made the
    follow-up read hang forever waiting for a pipe that would never close, defeating
    the timeout this function exists to enforce. `taskkill /T /F` kills the whole tree
    in one call, which is what actually lets the pipe close.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True, timeout=10,
        )
    else:
        process.kill()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        # Everything should already be dead and its pipes closed; a second hang here
        # means something this function cannot fix. Give up on the output rather than
        # block the caller indefinitely a second time.
        return b"", b""


def run(
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    extra_dirs: tuple[Path, ...] = (),
    strip_progress: bool = True,
) -> Result:
    """Run a command with stdin closed and a mandatory timeout.

    `env`, when given, is merged over the current environment rather than replacing
    it -- these tools need PATH and (on Windows) SYSTEMROOT to function at all.

    Uses `Popen` directly rather than `subprocess.run(timeout=...)`: the latter's own
    timeout handling only kills the direct child, which is not enough here -- see
    `_kill_tree`.
    """
    import time

    argv = [resolve(argv[0], extra_dirs), *(str(a) for a in argv[1:])]
    merged = {**os.environ, **(env or {})}
    # LiteLLM's banner and several tool outputs are non-ASCII; a cp1251 console would
    # otherwise raise UnicodeEncodeError inside the child.
    merged.setdefault("PYTHONIOENCODING", "utf-8")

    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=merged,
        creationflags=_NO_WINDOW,
    )
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        raw_out, raw_err = _kill_tree(process)
        partial_out = clean(_decode(raw_out)) if strip_progress else _decode(raw_out)
        partial_err = clean(_decode(raw_err)) if strip_progress else _decode(raw_err)
        raise ToolTimeout(
            f"{argv[0]} did not finish within {timeout:g}s and was killed",
            stdout=partial_out, stderr=partial_err,
        )

    out, err = _decode(raw_out), _decode(raw_err)
    if strip_progress:
        out, err = clean(out), clean(err)

    return Result(
        argv=argv,
        returncode=returncode,
        stdout=out,
        stderr=err,
        duration_s=time.monotonic() - started,
    )

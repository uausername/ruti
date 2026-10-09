"""The OpenRouter credit balance, for the status line.

The status line repaints every few seconds and cannot wait on a remote HTTPS call, so it
only ever reads a cache. The cache is refreshed by a detached `python -m ruti.balance`
that the prompt hook starts when the file has gone stale -- the same arrangement as the
doctor badge (`doctor.refresh_in_background`).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import read_json, write_json

URL = "https://openrouter.ai/api/v1/credits"

# Long enough that most prompts cost nothing, short enough that the number is not stale
# by the time it is read for reassurance.
REFRESH_AFTER = 600       # seconds; an older cache is fetched again
LOCK_TTL = 120            # a lock older than this is a crashed run, not a running one
STALE_AFTER = 6 * 3600    # past this the figure is shown dimmed, with a `~`

LOW = 5.0
CRITICAL = 1.0


def _credits_file() -> Path:
    # Resolved here rather than at import so a test's patched STATE_ROOT applies.
    from .config import STATE_ROOT

    return STATE_ROOT / "openrouter-credits.json"


def _lock_file() -> Path:
    from .config import STATE_ROOT

    return STATE_ROOT / "openrouter-credits.lock"


def fetch(key: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """OpenRouter's own account figures, or None when they cannot be had. Never raises."""
    request = urllib.request.Request(URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload["data"]
        return {"total": float(data["total_credits"]), "used": float(data["total_usage"]),
                "at": time.time()}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cached() -> dict[str, Any] | None:
    """The last figures `refresh` wrote, or None -- no file, or one that is not ours."""
    data = read_json(_credits_file(), default=None)
    if isinstance(data, dict) and all(_number(data.get(k)) for k in ("total", "used", "at")):
        return data
    return None


def remaining(data: dict[str, Any]) -> float:
    return float(data["total"]) - float(data["used"])


def segment(data: dict[str, Any], now: float | None = None) -> tuple[str, str]:
    """(`or:$12.56`, ANSI colour code): dimmed and marked `~` once the figure is old."""
    left = max(0.0, remaining(data))
    moment = time.time() if now is None else now
    if moment - float(data["at"]) > STALE_AFTER:
        return f"~or:${left:.2f}", "90"
    colour = "31" if left < CRITICAL else "33" if left < LOW else "32"
    return f"or:${left:.2f}", colour


def refresh() -> bool:
    """Fetch the balance with the account's OpenRouter key and cache it."""
    from . import usage

    key = usage.openrouter_key("free")
    if not key:
        return False
    data = fetch(key)
    if data is None:
        return False
    write_json(_credits_file(), data)
    return True


def refresh_in_background(*, now: float | None = None, spawn=subprocess.Popen) -> bool:
    """Start a detached `python -m ruti.balance` when the cache has gone stale.

    True when a run was started; False when there was nothing to do, another run holds
    the lock, or the spawn failed. Never raises: it is on the prompt hook's path.
    """
    try:
        moment = time.time() if now is None else now
        data = cached()
        if data and moment - float(data["at"]) < REFRESH_AFTER:
            return False

        lock = _lock_file()
        if lock.exists() and moment - lock.stat().st_mtime < LOCK_TTL:
            return False
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(moment), encoding="utf-8")

        # pythonw.exe never opens a console, and launching through a shell is what the
        # antivirus on this machine quarantines -- so the interpreter directly.
        interpreter = sys.executable
        windowless = Path(interpreter).with_name("pythonw.exe")
        if windowless.exists():
            interpreter = str(windowless)
        spawn(
            [interpreter, "-m", "ruti.balance"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(Path.home()),
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        )
        return True
    except Exception:
        return False


def main() -> int:
    try:
        refresh()
    finally:
        try:
            _lock_file().unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

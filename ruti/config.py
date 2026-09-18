"""Where ruti keeps its state, and how it writes there safely.

None of this lives in the repository: `quota.json` is rewritten many times a minute
by the status line, and nothing here is shareable between machines.

Two disciplines apply to every write:

* **atomic replace** -- write a sibling temp file and `os.replace()` it, which is
  atomic on NTFS. A status line that dies mid-write must never leave a truncated
  `quota.json` behind, because the router reads it to decide how much budget is left.
* **an advisory lock** around read-modify-write cycles. Two Claude Code sessions can
  call `ruti model use` at the same moment; without a lock they interleave loads and
  unloads and thrash the GPU.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent

LITELLM_DIR = REPO_ROOT / "litellm"
LITELLM_CONFIG = LITELLM_DIR / "config.yaml"
# Two generated files, not one: `ruti models sync` rewrites the local list wholesale
# from what LM Studio has on disk, and would erase any provider added by the wizard if
# they shared a file.
LITELLM_GENERATED = LITELLM_DIR / "models.generated.yaml"
LITELLM_PROVIDERS = LITELLM_DIR / "providers.generated.yaml"
LITELLM_ENV = LITELLM_DIR / ".env"
LITELLM_START_SCRIPT = LITELLM_DIR / "start-litellm.ps1"

PROXY_BASE = "http://127.0.0.1:4000"
LMSTUDIO_BASE = "http://127.0.0.1:1234"
SCHEDULED_TASK = "RutiLiteLLM"


def _state_root() -> Path:
    override = os.environ.get("RUTI_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "ruti"
    return Path.home() / ".local" / "state" / "ruti"


STATE_ROOT = _state_root()
STATE_FILE = STATE_ROOT / "state.json"
PROVIDERS_FILE = STATE_ROOT / "providers.json"
QUOTA_FILE = STATE_ROOT / "quota.json"
CALIBRATION_FILE = STATE_ROOT / "calibration.json"
POLICY_FILE = STATE_ROOT / "policy.toml"
CA_BUNDLE = STATE_ROOT / "ca-bundle.pem"
LOG_DIR = STATE_ROOT / "logs"
LOCK_DIR = STATE_ROOT / "locks"


def ensure_dirs() -> None:
    for directory in (STATE_ROOT, LOG_DIR, LOCK_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, treating a missing *or corrupt* file as absent.

    Corruption is recoverable for every file ruti owns -- they are caches and
    beliefs, never the source of truth -- so refusing to start over a bad byte
    would be worse than rebuilding.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


# A Windows sharing violation on the target lasts as long as someone else's read of
# it -- milliseconds. Six attempts with doubling pauses wait out ~0.6 s in total.
REPLACE_ATTEMPTS = 6

# A temp file this old belongs to a writer that is gone: one that failed before this
# fix existed, or was killed between writing and replacing.
STALE_TMP_SECONDS = 60.0


def write_json(path: Path, payload: Any) -> None:
    """Atomically replace `path` with `payload`, never leaving the temp file behind.

    On Windows `os.replace` fails while any other process has the target open: Python
    opens files without FILE_SHARE_DELETE, so one status line reading quota.json at the
    wrong moment makes another one's write fail with PermissionError. The callers
    swallow that on purpose -- bookkeeping must not break what it describes -- and the
    temp file used to stay behind every time; 99 had piled up before anyone looked,
    every one a complete JSON document whose replace had been refused. So the
    violation is waited out, anything that still fails is cleaned up before the error
    propagates, and a successful write clears what earlier writers abandoned.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # pid for tracing a leftover back to its writer; the random part so two threads of
    # one process can never share (and delete) each other's temp file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)  # already gone after a successful replace
    _sweep_stale_tmp(path)


def _sweep_stale_tmp(path: Path) -> None:
    """Delete this file's temp siblings that no live write can still be using."""
    import glob

    cutoff = time.time() - STALE_TMP_SECONDS
    for leftover in path.parent.glob(f"{glob.escape(path.name)}.*.tmp"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError:
            continue


@contextlib.contextmanager
def file_lock(name: str, *, timeout: float = 120.0) -> Iterator[None]:
    """Exclusive advisory lock, held for the duration of the block.

    Uses O_EXCL sentinel creation rather than msvcrt.locking() so a stale lock left
    by a killed process can be identified by the PID it recorded and cleared, instead
    of blocking every future run until reboot.
    """
    ensure_dirs()
    lock_path = LOCK_DIR / f"{name}.lock"
    deadline = time.monotonic() + timeout

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            holder = _lock_holder(lock_path)
            if holder is not None and not _pid_alive(holder):
                # The holder died without cleaning up. Reclaim it.
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire the {name!r} lock within {timeout:g}s"
                    + (f"; held by PID {holder}" if holder else "")
                )
            time.sleep(0.25)

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _lock_holder(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)
    return True


def load_dotenv(path: Path = LITELLM_ENV) -> dict[str, str]:
    """Parse a KEY=VALUE .env file the same way start-litellm.ps1 does."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values

"""Start the LiteLLM proxy with no console window -- what the RutiLiteLLM task runs.

Run by `pythonw.exe`, which has no console of its own, and it starts litellm with
CREATE_NO_WINDOW, so no console is ever created that could be shown. That is the
point of this file. `powershell.exe -WindowStyle Hidden` stopped being enough once
the task no longer ran elevated: on Windows 11 the default terminal is Windows
Terminal, which takes over the console of an ordinary (non-elevated) process and
cannot hide it, so every logon left an empty window whose closing killed the proxy.

It does what start-litellm.ps1 does -- load .env into the environment, stub any
missing generated include, bind to loopback only -- and that script stays for a
foreground start by hand. Nothing here is encoded or hidden from inspection: the
last attempt to launch the proxy through an encoded PowerShell command got
powershell.exe flagged by the antivirus.

Two things it adds, both found live on 2026-09-18. The proxy is a grandchild of this
process (pythonw -> litellm.exe -> python.exe), and stopping the task terminates this
process only: Task Scheduler does not end the tree, so the old proxy kept :4000 and
the restart looked like it worked. So this process joins a job object that kills
everything in it once its last handle closes -- which happens when this process dies,
however it dies. And LiteLLM, finding its default port taken, does not fail: it binds
a random port (proxy_cli.py, `port == 4000 and _is_port_in_use`) and runs on
unreachable, one more resident proxy per restart attempt. So a taken port is checked
here first, and refused.

And it watches the proxy after starting it. Python 3.12's proactor loop closes the
listening socket for good when a single accept fails (WinError 64, a client gone
mid-accept -- asyncio/proactor_events.py), and LiteLLM runs on with nothing on :4000:
found live on 2026-09-18 after about seven hours. The task still counts as running,
so nothing restarts it. `supervise` does.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
GENERATED = ("models.generated.yaml", "providers.generated.yaml")
# LiteLLM defaults to 0.0.0.0 and serves /model/info without auth; see start-litellm.ps1.
BIND_HOST = "127.0.0.1"
PORT = 4000

# The watchdog's health check is a TCP connect to :4000. The kernel completes it from
# the listen backlog however busy the proxy is, so load never looks like death -- only
# a closed listening socket does.
CHECK_EVERY_SECONDS = 30.0
# Consecutive failed checks before a proxy that was up counts as dead.
STRIKES = 2
# How long a (re)started proxy gets to bind; LiteLLM has taken over a minute.
STARTUP_SECONDS = 180.0
# More restarts than this within the window and the watchdog gives up, so a proxy
# that dies as soon as it starts cannot become a restart loop; `ruti doctor` then
# reports it down.
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 3600.0


def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, parsed with the same pattern as start-litellm.ps1."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        match = re.match(r"^\s*([^#=]+?)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def litellm_executable() -> str:
    found = shutil.which("litellm")
    if found:
        return found
    # pythonw.exe lives next to python.exe; its console scripts are in Scripts\.
    return str(Path(sys.executable).parent / "Scripts" / "litellm.exe")


def command(here: Path = HERE) -> list[str]:
    return [litellm_executable(), "--config", str(here / "config.yaml"), "--host", BIND_HOST]


def port_taken(host: str = BIND_HOST, port: int = PORT) -> bool:
    """Whether something already accepts connections there -- LiteLLM's own test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        return probe.connect_ex((host, port)) == 0


def die_with_this_process() -> bool:
    """Put this process in a job that kills every member when its last handle closes.

    Children join the job as they are created, so the proxy dies with this process.
    The handle is not inheritable, so no child can keep the job alive. False if the
    job could not be set up -- the proxy then starts as it always did.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
        job, job_object_extended_limit_information, ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(job)
        return False
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(job)
        return False
    # Deliberately never closed: the handle dies with this process, and that is the
    # moment the job is meant to end.
    return True


def kill_tree(child: subprocess.Popen) -> None:
    """End the proxy and everything under it (litellm.exe -> python.exe)."""
    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                   stdin=subprocess.DEVNULL, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _watch(child, healthy: Callable[[], bool], *, sleep: Callable[[float], None],
           clock: Callable[[], float]) -> str | None:
    """Wait on a running proxy. None once it exits by itself; otherwise why it should
    be restarted."""
    started = clock()
    up = False
    strikes = 0
    while child.poll() is None:
        sleep(CHECK_EVERY_SECONDS)
        if child.poll() is not None:
            break
        if healthy():
            up, strikes = True, 0
        elif up:
            strikes += 1
            if strikes >= STRIKES:
                return f"stopped listening on :{PORT} while still running"
        elif clock() - started >= STARTUP_SECONDS:
            return f"did not bind :{PORT} within {STARTUP_SECONDS:.0f} s"
    return None


def supervise(start: Callable[[], subprocess.Popen], healthy: Callable[[], bool],
              kill: Callable[[subprocess.Popen], None], note: Callable[[str], None], *,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> int:
    """Run the proxy; restart it when it is running but not listening.

    A proxy that exits by itself is not restarted -- a configuration error would only
    fail again -- and its exit code is returned. 1 when restarts run out, or when
    something else took the port while this one was being replaced.
    """
    restarts: list[float] = []
    while True:
        child = start()
        reason = _watch(child, healthy, sleep=sleep, clock=clock)
        if reason is None:
            code = child.poll()
            note(f"the proxy (PID {child.pid}) exited with {code}")
            return code if code is not None else 1

        note(f"restarting the proxy (PID {child.pid}): it {reason}")
        kill(child)
        now = clock()
        restarts = [at for at in restarts if now - at < RESTART_WINDOW_SECONDS] + [now]
        if len(restarts) > MAX_RESTARTS:
            note(f"giving up after {MAX_RESTARTS} restarts within "
                 f"{RESTART_WINDOW_SECONDS / 60:.0f} min -- `ruti doctor` reports the proxy "
                 "down, and `ruti doctor --fix` starts it again")
            return 1
        if healthy():
            note(f":{PORT} was taken while the proxy was being replaced -- not starting a "
                 "second one")
            return 1


def main() -> int:
    with (HERE / "litellm.log").open("ab") as log:
        def note(message: str) -> None:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"{stamp} start_litellm: {message}\n".encode("utf-8"))
            log.flush()

        if port_taken():
            note(f"{BIND_HOST}:{PORT} is already taken -- not starting a second proxy "
                 "(LiteLLM would move to a random port nobody uses). `ruti doctor --fix` "
                 "replaces the running one.")
            return 1
        if not die_with_this_process():
            note("no job object -- stopping the task will not stop the proxy")

        env = {**os.environ, **load_env(HERE / ".env"), "PYTHONIOENCODING": "utf-8"}
        for name in GENERATED:
            stub = HERE / name
            if not stub.exists():
                stub.write_text("# Placeholder until ruti regenerates it.\nmodel_list: []\n",
                                encoding="utf-8")

        def start() -> subprocess.Popen:
            return subprocess.Popen(
                command(), cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            )

        return supervise(start, port_taken, kill_tree, note)


if __name__ == "__main__":
    sys.exit(main())

"""Health checks, and the fixes for the ones that are safe to automate.

Every check here exists because the corresponding failure is *silent*. A stopped LM
Studio, a dangling model alias, an unrepaired TLS chain -- none of them announce
themselves. They surface as a delegate that answers a little oddly, or a provider key
that appears to be rejected, and cost far more time to diagnose than to detect.

Fixes are opt-in (`--fix`) and each one names what it will change before doing it.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable

from . import jev, litellm_cfg, lmstudio, planner, tls
from .config import (
    CA_BUNDLE, LITELLM_ENV, LITELLM_GENERATED, LITELLM_START_SCRIPT, REPO_ROOT,
    SCHEDULED_TASK, load_dotenv,
)

OK, WARN, BAD = "ok", "warn", "bad"

LIVE_OPENCODE = Path.home() / ".config" / "opencode" / "opencode.json"
REPO_OPENCODE = REPO_ROOT / "opencode" / "opencode.json"


@dataclass
class Check:
    name: str
    status: str
    message: str
    detail: str = ""
    fix: Callable[[], str] | None = None
    fix_label: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if any(c.status == BAD for c in self.checks):
            return BAD
        if any(c.status == WARN for c in self.checks):
            return WARN
        return OK

    @property
    def fixable(self) -> list[Check]:
        return [c for c in self.checks if c.fix and c.status != OK]

    def cache(self) -> None:
        """Persist enough of this report for the status line to show it later.

        The status line cannot call `run_checks()` itself -- several checks here walk
        `schtasks` or a TLS chain, seconds of work that would freeze the interface on
        the repaint that hit an expired cache. So instead this writes once, right after
        the session-start hook already paid the cost, and the status line only ever
        reads what lands here -- passively, with no TTL of its own to expire.
        """
        from .config import STATE_ROOT, write_json

        problems = [c for c in self.checks if c.status != OK]
        write_json(STATE_ROOT / "doctor-last.json", {
            "at": time.time(),
            "worst": self.worst,
            "problems": [{"name": c.name, "status": c.status} for c in problems],
        })


def cached() -> dict | None:
    """The last report `cache()` wrote, or None if there is none yet.

    Read-only and TTL-free on purpose -- see `Report.cache`. A missing file (no
    session has started yet, or this is a `-p` run where the hook never fired) and a
    corrupt one both read as "nothing to show", never as a problem worth surfacing.
    """
    from .config import STATE_ROOT, read_json

    data = read_json(STATE_ROOT / "doctor-last.json", default=None)
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- fixes


def _fix_tls() -> str:
    certifi_count, added = tls.build_bundle()
    _set_env_var("SSL_CERT_FILE", str(CA_BUNDLE))
    return (
        f"merged {certifi_count} certifi roots with {added} from the OS store into "
        f"{CA_BUNDLE}, and pointed SSL_CERT_FILE at it in .env "
        f"(restart the proxy for it to take effect)"
    )


def _set_env_var(key: str, value: str) -> None:
    """Add or replace a KEY=VALUE line in litellm/.env, leaving comments intact."""
    lines = LITELLM_ENV.read_text(encoding="utf-8-sig").splitlines() if LITELLM_ENV.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if "=" in line and not line.strip().startswith("#") and line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
    LITELLM_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wait_for_liveliness(budget_s: float, *, interval_s: float = 2.0) -> bool:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if litellm_cfg.liveliness(timeout=2.0):
            return True
        time.sleep(interval_s)
    return litellm_cfg.liveliness(timeout=2.0)


def _listening_pids(port: int = 4000) -> list[int]:
    """PIDs holding a LISTENING socket on `port`, from netstat's last column."""
    from . import proc

    try:
        result = proc.run(["netstat", "-ano"], timeout=15.0)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTEN" not in parts[3].upper():
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, lower-cased image name), from a Toolhelp snapshot.

    ctypes rather than PowerShell or WMI: cheap, and nothing an antivirus reads as a
    script launching processes. {} where it cannot be taken.
    """
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _is_litellm(pid: int, table: dict[int, tuple[int, str]]) -> bool:
    """Whether `pid` is a LiteLLM proxy: litellm.exe itself, or the interpreter that
    pip's `litellm.exe` console-script launcher started."""
    parent, image = table.get(pid, (0, ""))
    if image == "litellm.exe":
        return True
    return image.startswith("python") and table.get(parent, (0, ""))[1] == "litellm.exe"


# How long a killed proxy gets to let go of :4000 before it counts as still running.
PORT_RELEASE_SECONDS = 5.0
# How long a started proxy gets to answer /health/liveliness. 45 s was not enough on
# 2026-09-18: a start after hours of uptime answered at about a minute, and the fix
# reported a start that then succeeded as a failure.
PROXY_START_SECONDS = 90.0


def _fix_proxy_restart() -> str:
    """Replace a proxy that is alive but serving the config it started with.

    Stopping the scheduled task is not enough, and the failure looks like success:
    the listener is a grandchild of the task's process (pythonw -> litellm.exe ->
    python.exe), and the scheduler ends only the process it started, so the old
    proxy keeps :4000. The replacement then does not even fail -- LiteLLM moves to a
    random port -- and the stale process carries on answering, `schtasks` having
    reported exit 0 throughout. start_litellm.pyw now guards against both (a job
    object, a port check), but a launcher started before that still needs this.

    Killing it has the same trap one level down, so the kill is confirmed rather
    than assumed. A proxy started by a task registered with highest privileges runs
    elevated, and from a shell that is not, `taskkill` is refused. This used to carry
    on regardless: it started the task, the old process answered the liveliness
    probe, and the fix reported "started via the RutiLiteLLM scheduled task" while
    the proxy was exactly as stale as before. Found live when a config change needed
    a restart and never got one. `_check_proxy_task` removes the elevation itself.
    """
    from . import proc

    # End the task's own instance first. The listener is a grandchild of it, and
    # killing only that leaves the launcher alive for a moment -- long enough for the
    # scheduler to count the task as running and silently ignore the /Run below.
    with contextlib.suppress(proc.ToolNotFound, proc.ToolTimeout):
        proc.run(["schtasks", "/End", "/TN", SCHEDULED_TASK], timeout=15.0)

    killed: list[int] = []
    refused: dict[int, str] = {}
    listeners = _listening_pids()
    # Only ever a LiteLLM. This used to kill whatever held :4000, which was safe only
    # while it ran after liveliness had answered; it now also runs when nothing
    # answers, and from `openrouter setup` -- where :4000 could be any other server.
    table = _process_table() if listeners else {}
    foreign = [pid for pid in listeners if table and not _is_litellm(pid, table)]
    if foreign:
        pid = foreign[0]
        image = table.get(pid, (0, "?"))[1]
        raise RuntimeError(
            f"PID {pid} ({image}) holds :4000 and is not a LiteLLM proxy, so ruti will "
            "not kill it and the proxy cannot bind there. Stop it, then `ruti doctor --fix`"
        )
    for pid in listeners:
        try:
            result = proc.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15.0)
        except (proc.ToolNotFound, proc.ToolTimeout) as exc:
            refused[pid] = str(exc)
            continue
        # taskkill's own message is localised and arrives in the OEM code page, which
        # proc cannot always tell apart from cp1251 -- the exit code is what is reliable.
        if result.ok:
            killed.append(pid)
        else:
            refused[pid] = f"taskkill exited {result.returncode}"

    survivors = _wait_for_port_release(PORT_RELEASE_SECONDS)
    if survivors:
        pid = survivors[0]
        why = refused.get(pid) or "it was still listening after taskkill reported success"
        cause = (
            "this shell is not elevated, so the proxy most likely is -- the proxy-task "
            "check says how to stop that for good"
            if not _is_elevated() else "the process would not exit"
        )
        raise RuntimeError(
            f"could not stop the running proxy (PID {pid}: {why}; {cause}). It keeps "
            "serving the config it started with, so nothing new was started. From an "
            f"administrator PowerShell: taskkill /PID {pid} /T /F; "
            f"Start-ScheduledTask -TaskName {SCHEDULED_TASK}"
        )

    started = _fix_proxy_start()
    freed = ", ".join(str(pid) for pid in killed) or "nothing"
    return f"killed {freed}, then {started}"


def _wait_for_port_release(budget_s: float, *, interval_s: float = 0.5) -> list[int]:
    """PIDs still listening on :4000 once `budget_s` is up, or [] as soon as none are."""
    deadline = time.monotonic() + budget_s
    while True:
        pids = _listening_pids()
        if not pids or time.monotonic() >= deadline:
            return pids
        time.sleep(interval_s)


def _is_elevated() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _fix_proxy_start() -> str:
    """Bring the proxy up through the scheduled task.

    `schtasks /Run` reports exit code 0 whether or not the triggered instance ever
    actually launches the action. When the task ran elevated, on this machine the
    instance could sit in the Queued state forever -- something blocked elevated
    token duplication for a plain user account -- and the exit code said nothing
    about it. Liveliness is therefore the only trustworthy signal, and a direct
    launch of the same script is the fallback when the task does not deliver.

    There is deliberately no automatic direct launch any more. The last one started a
    hidden PowerShell through WMI with a base64 `-EncodedCommand` -- which is also how
    malware hides what it runs, and the antivirus on this machine read it that way:
    it flagged powershell.exe itself (IDP.HELU.PSE90%s_cmd) and removed the
    RutiLiteLLM task. Starting the script by hand is a one-line instruction; a
    quarantined system binary is not.
    """
    from . import proc

    proc.run(["schtasks", "/Run", "/TN", SCHEDULED_TASK], timeout=15.0)
    # LiteLLM takes 14-25 s to import and bind on this machine; 20 s was measured
    # to be too short, and reported a start that then succeeded as a failure.
    if _wait_for_liveliness(PROXY_START_SECONDS):
        return f"started via the {SCHEDULED_TASK} scheduled task"
    raise RuntimeError(
        f"the {SCHEDULED_TASK} task did not bring the proxy up -- check that it exists "
        f"(`ruti doctor`'s proxy-task check), or run {LITELLM_START_SCRIPT} by hand "
        "and read litellm/litellm.log"
    )


def _task_xml() -> str | None:
    """The proxy task's definition, or None if it is not registered."""
    from . import proc

    try:
        # schtasks ends its XML lines with \r\r\n, which progress stripping reads as
        # a line being overwritten -- and erases the whole document.
        result = proc.run(["schtasks", "/Query", "/TN", SCHEDULED_TASK, "/XML"],
                          timeout=15.0, strip_progress=False)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return None
    return result.stdout if result.ok else None


def _task_run_level() -> str | None:
    """The proxy task's RunLevel ("HighestAvailable" / "LeastPrivilege"), or None if
    the task is not registered."""
    xml = _task_xml()
    if xml is None:
        return None
    match = re.search(r"<RunLevel>\s*(\w+)\s*</RunLevel>", xml)
    # No element is the scheduler's default, which is least privilege.
    return match.group(1) if match else "LeastPrivilege"


# The scheduler's defaults, which apply when the element is absent, all stop the proxy:
# not started on battery, stopped when the laptop is unplugged, stopped after 72 hours.
# Before start_litellm.pyw joined a job object that went unnoticed -- stopping the task
# ended only the launcher, and the proxy it had started carried on as an orphan. Now
# the proxy goes with the task, so these have to say what is meant.
PROXY_TASK_SETTINGS = {
    "DisallowStartIfOnBatteries": "false",
    "StopIfGoingOnBatteries": "false",
    "ExecutionTimeLimit": "PT0S",
}


def _task_settings_problems(xml: str) -> list[str]:
    settings = re.search(r"<Settings>(.*?)</Settings>", xml, re.S)
    body = settings.group(1) if settings else ""
    problems = []
    for name, wanted in PROXY_TASK_SETTINGS.items():
        found = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", body)
        if (found.group(1) if found else None) != wanted:
            problems.append(name)
    return problems


def _with_proxy_task_settings(xml: str) -> str:
    """The task definition with PROXY_TASK_SETTINGS set, everything else untouched."""
    def fix(match: re.Match[str]) -> str:
        body = match.group(2)
        for name, wanted in PROXY_TASK_SETTINGS.items():
            element = f"<{name}>{wanted}</{name}>"
            body, count = re.subn(rf"<{name}>.*?</{name}>", element, body, flags=re.S)
            if not count:
                body = f"\n    {element}" + body
        return match.group(1) + body + match.group(3)

    return re.sub(r"(<Settings>)(.*?)(</Settings>)", fix, xml, count=1, flags=re.S)


def _fix_proxy_task_settings() -> str:
    """Re-register the task with settings that keep the proxy running.

    Through `schtasks /Create /XML`, not PowerShell: nothing encoded, nothing for the
    antivirus to read as a script. The previous definition is kept next to ruti's state.
    """
    from . import proc
    from .config import STATE_ROOT

    xml = _task_xml()
    if xml is None:
        raise RuntimeError(f"the {SCHEDULED_TASK} task is not registered")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    backup = STATE_ROOT / f"{SCHEDULED_TASK}.task-backup.xml"
    staged = STATE_ROOT / f"{SCHEDULED_TASK}.task.xml"
    # schtasks reads the file as the encoding its declaration names: UTF-16.
    backup.write_text(xml, encoding="utf-16")
    staged.write_text(_with_proxy_task_settings(xml), encoding="utf-16")
    try:
        result = proc.run(["schtasks", "/Create", "/TN", SCHEDULED_TASK, "/XML", str(staged),
                           "/F"], timeout=30.0)
    finally:
        staged.unlink(missing_ok=True)
    if not result.ok:
        raise RuntimeError(f"schtasks /Create exited {result.returncode}; the task is "
                           f"unchanged (its definition is saved in {backup})")
    left = _task_settings_problems(_task_xml() or "")
    if left:
        raise RuntimeError(f"re-registered, but still set: {', '.join(left)}")
    started = "" if litellm_cfg.liveliness(timeout=2.0) else f"; {_fix_proxy_start()}"
    return (f"the task now keeps the proxy running on battery and past 72 hours "
            f"(previous definition: {backup}){started}")


def _task_command() -> str:
    """The program the proxy task runs, or "" if it cannot be read."""
    match = re.search(r"<Command>\s*(.*?)\s*</Command>", _task_xml() or "", re.S)
    return match.group(1).strip('"') if match else ""


def _fix_lmstudio_server() -> str:
    lmstudio.start_server()
    return "started the LM Studio server on :1234"


def _fix_sync() -> str:
    entries = [
        litellm_cfg.local_entry(planner.identifier_for(m.key))
        for m in lmstudio.list_models()
        if m.kind == "llm"
    ]
    litellm_cfg.write_generated(entries)
    wired = litellm_cfg.wire_include()
    suffix = " and wired the include: line" if wired else ""
    return f"regenerated {len(entries)} local model entries{suffix} (restart the proxy)"


def _fix_opencode_drift() -> str:
    LIVE_OPENCODE.parent.mkdir(parents=True, exist_ok=True)
    if not LIVE_OPENCODE.exists():
        shutil.copyfile(REPO_OPENCODE, LIVE_OPENCODE)
        return f"copied {REPO_OPENCODE} over {LIVE_OPENCODE}"

    # Merge rather than overwrite: a locally registered provider (`ruti provider
    # add`, `ruti openrouter setup`) adds models to the live file that the repo
    # template never had and never will -- those are not drift, and a plain
    # copy used to delete them every time the repo picked up a new baseline
    # model. Only the repo's own models are ever added or refreshed; anything
    # live-only is left alone.
    live = json.loads(LIVE_OPENCODE.read_text(encoding="utf-8"))
    repo = json.loads(REPO_OPENCODE.read_text(encoding="utf-8"))
    live_models = live.setdefault("provider", {}).setdefault("ruti-router", {}).setdefault("models", {})
    repo_models = repo.get("provider", {}).get("ruti-router", {}).get("models") or {}
    added = [name for name in repo_models if name not in live_models]
    live_models.update(repo_models)
    LIVE_OPENCODE.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    return f"merged {len(added)} new model(s) from the repo into {LIVE_OPENCODE}"


# -------------------------------------------------------------------------- checks


def _check_tls() -> Check:
    if tls.bundle_works():
        env = load_dotenv()
        if env.get("SSL_CERT_FILE") != str(CA_BUNDLE):
            return Check(
                "tls", WARN,
                "a working CA bundle exists but .env does not point at it",
                detail="the proxy will still verify against certifi and fail",
                fix=_fix_tls, fix_label="write SSL_CERT_FILE into .env",
            )
        return Check("tls", OK, "certificate chain verifies through the merged bundle")

    found = tls.detect()
    if found.intercepted:
        return Check(
            "tls", BAD,
            f"TLS to {found.host} is intercepted by {found.issuer!r}",
            detail=(
                "its root is trusted by the OS but absent from certifi, so every "
                "provider call fails with CERTIFICATE_VERIFY_FAILED -- which is easy "
                "to mistake for a rejected API key"
            ),
            fix=_fix_tls, fix_label="build a merged CA bundle and point .env at it",
        )
    if not found.os_store_ok:
        return Check("tls", WARN, f"{found.host} is unreachable", detail="offline?")
    return Check("tls", OK, "certificate chain verifies against certifi")


def _check_proxy_bind() -> Check:
    from . import proc

    try:
        result = proc.run(["netstat", "-ano"], timeout=15.0)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return Check("proxy-bind", WARN, "could not inspect listening sockets")

    for line in result.stdout.splitlines():
        if ":4000" in line and "LISTEN" in line.upper():
            local = line.split()[1] if len(line.split()) > 1 else ""
            if local.startswith("0.0.0.0") or local.startswith("[::]"):
                return Check(
                    "proxy-bind", BAD,
                    f"the proxy is listening on {local} -- every interface",
                    detail=(
                        "/model/info answers without authentication, so anyone who can "
                        "route to this machine can read the config and spend your API keys. "
                        "start-litellm.ps1 passes --host 127.0.0.1; this process predates it"
                    ),
                )
            return Check("proxy-bind", OK, f"listening on {local}")
    return Check("proxy-bind", WARN, "nothing is listening on :4000")


def _check_proxy_alive() -> Check:
    if litellm_cfg.liveliness():
        served = litellm_cfg.served_models()
        # Liveness alone is not health. A proxy that started before the last
        # `provider add` / `openrouter setup` keeps answering happily while missing
        # every model registered since, and `route` then rules those out as
        # "registered but not served" -- the silent failure this tool exists to catch.
        missing = [name for name in litellm_cfg.declared_models() if name not in served]
        if missing:
            return Check(
                "proxy", WARN,
                f"alive, but serving {len(served)} of {len(served) + len(missing)} "
                "declared model(s)",
                detail=(
                    f"declared but not served: {', '.join(missing)}. The running process "
                    "predates their registration; restarting it is what picks them up"
                ),
                fix=_fix_proxy_restart, fix_label="restart the proxy",
            )
        return Check("proxy", OK, f"alive, serving {len(served)} model(s)",
                     detail=", ".join(served))
    # Not responding does not mean not running. Python 3.12's proactor loop closes the
    # listening socket for good when one accept fails (WinError 64, a client gone
    # mid-accept -- asyncio/proactor_events.py), and the process lives on with nothing
    # on :4000. The task then still counts as running, `schtasks /Run` is ignored, and
    # a plain start could never bring it back. Found live on 2026-09-18 after ~7 hours
    # of uptime. So the fix is the full restart, which ends the task first.
    return Check(
        "proxy", BAD, "not responding on /health/liveliness",
        detail=(
            f"the {SCHEDULED_TASK} scheduled task should start it at logon; a proxy "
            "can also be running but no longer listening. `--fix` ends the task and "
            "starts it again, and says what to do by hand if that does not bring it up"
        ),
        fix=_fix_proxy_restart, fix_label="restart the proxy",
    )


def _check_proxy_task() -> Check:
    """The proxy must not run elevated.

    It used to, on purpose: .env was meant to be readable only by administrators, so
    the task that starts the proxy was registered with highest privileges to read
    its own keys. That protection never took hold -- the user's own account kept
    full control of the file, so any process it runs can read the keys anyway -- and
    the elevation cost three real problems. ruti cannot restart an elevated proxy
    (`taskkill` is refused from an ordinary shell). The elevated task could sit
    Queued forever on this machine, which is what the UAC fallback in
    `_fix_proxy_start` was for. And a server that accepts unauthenticated requests
    from every local process, running as administrator, makes any LiteLLM bug an
    administrator one. Nothing in the proxy needs the rights: :4000 is not a
    privileged port.
    """
    level = _task_run_level()
    if level is None:
        return Check(
            "proxy-task", WARN, f"the {SCHEDULED_TASK} scheduled task is not registered",
            detail="the proxy will not start at logon -- see step 5 of the README",
        )
    if level == "HighestAvailable":
        return Check(
            "proxy-task", WARN, f"the {SCHEDULED_TASK} task runs the proxy elevated",
            # No automatic fix: changing an elevated task takes an elevated shell, and
            # the scripted route there (an encoded command through UAC) is what the
            # antivirus on this machine quarantined powershell.exe over.
            detail=(
                "nothing in the proxy needs administrator rights, and while it has them "
                "ruti cannot restart it. From an administrator PowerShell: "
                f"$t = Get-ScheduledTask {SCHEDULED_TASK}; Set-ScheduledTask "
                f"{SCHEDULED_TASK} -Principal (New-ScheduledTaskPrincipal -UserId "
                "$t.Principal.UserId -LogonType Interactive -RunLevel Limited); "
                f"Stop-ScheduledTask {SCHEDULED_TASK}; taskkill /PID <proxy pid> /T /F; "
                f"Start-ScheduledTask {SCHEDULED_TASK}"
            ),
        )
    # PureWindowsPath, not Path: this string comes from schtasks and is always a
    # Windows path, but `Path` takes the flavour of whatever platform is running. On
    # POSIX a backslash is an ordinary character, so the whole of
    # C:\Python312\pythonw.exe reads as the file name and the check below misfires.
    program = PureWindowsPath(_task_command()).name.lower()
    if program and program != "pythonw.exe":
        # A console program started by a non-elevated task gets its console taken over
        # by Windows Terminal, which cannot hide it: an empty window at every logon,
        # and closing it kills the proxy. pythonw.exe never creates a console.
        return Check(
            "proxy-task", WARN,
            f"the {SCHEDULED_TASK} task starts the proxy through {program}",
            detail=(
                "that leaves an empty console window open, and closing it stops the "
                "proxy. Re-register the task as in step 5 of the README, which runs "
                "litellm/start_litellm.pyw with pythonw.exe and opens no window"
            ),
        )
    problems = _task_settings_problems(_task_xml() or "")
    if problems:
        return Check(
            "proxy-task", WARN,
            f"the {SCHEDULED_TASK} task stops the proxy on battery or after 72 hours",
            detail=(
                f"set by the scheduler's defaults: {', '.join(problems)}. Stopping the "
                "task stops the proxy with it, and on battery it would not start again"
            ),
            fix=_fix_proxy_task_settings, fix_label="keep the proxy running on battery",
        )
    return Check("proxy-task", OK, f"{SCHEDULED_TASK} runs the proxy without elevation "
                                   "or a console window")


def _check_lmstudio() -> Check:
    if not lmstudio.available():
        return Check("lmstudio", WARN, "the `lms` CLI is not installed",
                     detail="local models are unavailable; remote providers still work")
    if not lmstudio.server_running():
        return Check(
            "lmstudio", BAD, "the LM Studio server is down",
            detail=(
                "the desktop app being open does not start it. Every local request "
                "will fail over to a remote provider, which looks like success"
            ),
            fix=_fix_lmstudio_server, fix_label="start the server",
        )
    loaded = lmstudio.loaded_models()
    if not loaded:
        return Check("lmstudio", WARN, "server up, but no model is loaded",
                     detail="run `ruti model use <key>`")
    return Check(
        "lmstudio", OK,
        f"{len(loaded)} model(s) loaded",
        detail=", ".join(f"{m.identifier}@{m.loaded_context}" for m in loaded),
    )


def _declared_local_models() -> set[str]:
    """Model names the generated include declares -- the ones LM Studio has to back."""
    if not LITELLM_GENERATED.exists():
        return set()
    text = LITELLM_GENERATED.read_text(encoding="utf-8")
    return {line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.strip().startswith("- model_name:")}


def _check_generated_sync() -> Check:
    if not litellm_cfg.include_is_wired():
        return Check(
            "model-list", BAD, "config.yaml does not include models.generated.yaml",
            detail="no local model is reachable through the proxy",
            fix=_fix_sync, fix_label="regenerate and wire the include",
        )
    if not LITELLM_GENERATED.exists():
        return Check("model-list", BAD, "models.generated.yaml is missing",
                     detail="the proxy will refuse to start",
                     fix=_fix_sync, fix_label="regenerate it")

    on_disk = {planner.identifier_for(m.key) for m in lmstudio.list_models() if m.kind == "llm"}
    declared = _declared_local_models()
    missing, stale = on_disk - declared, declared - on_disk
    if missing or stale:
        parts = []
        if missing:
            parts.append("downloaded but not declared: " + ", ".join(sorted(missing)))
        if stale:
            parts.append("declared but no longer on disk: " + ", ".join(sorted(stale)))
        return Check("model-list", WARN, "the generated model list is out of date",
                     detail="; ".join(parts), fix=_fix_sync, fix_label="regenerate it")
    return Check("model-list", OK, f"{len(on_disk)} local model(s) declared and present")


def _check_local_route() -> Check:
    """Do the local models the proxy advertises and the ones LM Studio holds agree?

    Both directions break silently. A model the proxy serves without LM Studio behind
    it fails the request, `default_fallbacks` answers from gemini-flash, and nothing
    anywhere says the work left the machine -- so this is worth naming loudest exactly
    when LM Studio is down, which is when it used to skip. A model that is resident but
    unserved is the cheaper mirror image: paid for in VRAM, reachable by nothing.
    """
    if not litellm_cfg.liveliness():
        return Check("local-route", WARN, "skipped -- the proxy is down",
                     detail="nothing serves a local model without it")

    served = set(litellm_cfg.served_models())
    advertised = sorted(_declared_local_models() & served)

    # The `lmstudio` check already reports the stopped server and owns the fix for it;
    # this one adds what that check cannot know -- which models the proxy still offers,
    # and hence which requests will be answered by a provider nobody asked for.
    if not lmstudio.server_running():
        if not advertised:
            return Check("local-route", OK,
                         "LM Studio is down, but the proxy advertises no local model")
        return Check(
            "local-route", WARN,
            f"{len(advertised)} advertised local model(s) cannot answer",
            detail=("LM Studio is down, so a request for " + ", ".join(advertised) +
                    " does not error -- it falls back to gemini-flash. Start the "
                    "server (`ruti doctor --fix`) or stop serving them."),
        )

    resident = {m.identifier for m in lmstudio.loaded_models() if m.identifier}
    if not resident:
        return Check("local-route", WARN, "no local model is resident to route to",
                     detail="run `ruti model use <key>`")

    unreachable = resident - served
    if unreachable:
        return Check(
            "local-route", WARN,
            "loaded but not served: " + ", ".join(sorted(unreachable)),
            detail="run `ruti models sync` and restart the proxy",
            fix=_fix_sync, fix_label="regenerate the model list",
        )
    return Check("local-route", OK,
                 "resident models are routable: " + ", ".join(sorted(resident)))


def _check_opencode_drift() -> Check:
    if not LIVE_OPENCODE.exists():
        return Check("opencode", BAD, "no OpenCode config installed",
                     detail=f"expected {LIVE_OPENCODE}",
                     fix=_fix_opencode_drift, fix_label="install the repo copy")
    try:
        live = json.loads(LIVE_OPENCODE.read_text(encoding="utf-8"))
        repo = json.loads(REPO_OPENCODE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return Check("opencode", WARN, f"could not compare configs: {exc}")

    live_models = set((live.get("provider", {}).get("ruti-router", {}).get("models") or {}))
    repo_models = set((repo.get("provider", {}).get("ruti-router", {}).get("models") or {}))
    # Only models the repo has and the install lacks are drift (a version bump
    # added a baseline model this machine never picked up). Models the install
    # has beyond the repo's are locally registered providers (`provider add`,
    # `openrouter setup`) -- expected, not a problem, and never worth flagging.
    missing = repo_models - live_models
    if missing:
        return Check(
            "opencode", WARN, "the installed OpenCode config is missing repo models",
            detail=f"missing: {sorted(missing)}; installed has {len(live_models)} total",
            fix=_fix_opencode_drift, fix_label="merge the repo's models in",
        )
    extra = live_models - repo_models
    note = f" (plus {len(extra)} locally registered)" if extra else ""
    return Check("opencode", OK, f"config matches the repo ({len(live_models)} models){note}")


# Groups whose membership is effectively "anyone with a session on this box". A
# credentials file granted to any of these is readable by every process the machine
# runs, which for API keys means every installer, updater, and browser extension.
_BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-32-545": "Users",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "Interactive",
}


def _acl_sids(path: Path) -> list[str] | None:
    """SIDs with an allow-ACE on `path`, or None if they cannot be read.

    icacls prints localised group names, so it cannot be matched against reliably on a
    non-English Windows. Translating to SIDs via .NET keeps the check language-neutral.
    """
    from . import proc

    script = (
        f"(Get-Acl -LiteralPath '{path}').Access | "
        "Where-Object { $_.AccessControlType -eq 'Allow' } | "
        "ForEach-Object { try { "
        "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
        "} catch { $_.IdentityReference.Value } }"
    )
    try:
        result = proc.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=25.0,
        )
    except (proc.ToolNotFound, proc.ToolTimeout):
        return None
    if not result.ok:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _check_env_permissions() -> Check:
    if not LITELLM_ENV.exists():
        return Check("secrets", BAD, "litellm/.env does not exist",
                     detail="copy .env.example and add your keys")

    sids = _acl_sids(LITELLM_ENV)
    if sids is None:
        return Check("secrets", OK, ".env present (permissions could not be read)")

    exposed = [name for sid, name in _BROAD_SIDS.items() if sid in sids]
    if exposed:
        return Check(
            "secrets", BAD,
            ".env grants access to " + ", ".join(exposed),
            detail=(
                "your API keys are readable by every process running as any local user. "
                "Restrict it with: icacls litellm\\.env /inheritance:d /remove:g "
                "\"*S-1-5-32-545\" \"*S-1-5-11\""
            ),
        )
    # Your own account is among these, so any process you run can read the keys --
    # what this check guarantees is that no other local user's processes can.
    return Check("secrets", OK, f".env not readable by other local users "
                                f"({len(sids)} principal(s) listed)")


def _git_ssl_backend() -> str | None:
    """The TLS backend git will actually use, or None if git is unavailable."""
    from . import proc

    try:
        result = proc.run(["git", "config", "--get", "http.sslBackend"], timeout=10.0)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return None
    # Unset means git's compiled-in default. Git for Windows ships a system gitconfig
    # that sets openssl explicitly, so an empty answer here means a non-Windows build.
    return (result.stdout or "").strip().lower() or ""


def _fix_git_tls() -> str:
    from . import proc

    proc.run(
        ["git", "config", "--global", "http.sslBackend", "schannel"], timeout=15.0
    ).check()
    return "set git's global http.sslBackend to schannel (the Windows certificate store)"


def _check_git_tls() -> Check:
    """Does git trust the intercepting root, or does it fail the way everything else did?

    The `tls` check above covers Python, which verifies against certifi. git does not
    use certifi: Git for Windows ships its own `ca-bundle.crt` and defaults to the
    openssl backend, so it fails separately, with the same uninformative message, and
    the earlier check passing says nothing about it. Found while pushing this
    repository -- `ruti doctor` was entirely green at the time.

    The fix is to verify against the Windows certificate store, which does trust the
    root, rather than to stop verifying.
    """
    backend = _git_ssl_backend()
    if backend is None:
        return Check("git-tls", WARN, "git is not on PATH",
                     detail="cannot check whether it can reach an HTTPS remote")
    if backend == "schannel":
        return Check("git-tls", OK, "git verifies through the Windows certificate store")

    # Only a problem when something is actually intercepting; on a clean machine the
    # bundled roots are fine and there is nothing to fix.
    found = tls.detect()
    if not found.intercepted:
        return Check("git-tls", OK,
                     f"git uses {backend or 'its default backend'}; nothing is intercepting")

    return Check(
        "git-tls", BAD,
        f"git verifies against its own bundle while {found.issuer!r} intercepts TLS",
        detail=(
            "`git push` and `git clone` over HTTPS fail with `unable to get local "
            "issuer certificate`. The Windows certificate store does trust that root, "
            "so switching backends fixes it without weakening verification"
        ),
        fix=_fix_git_tls, fix_label="point git at the Windows certificate store",
    )


def _check_statusline() -> Check:
    """Is the status line registered, and is it actually producing readings?

    This check exists because its absence cost a day. The status line is the only local
    source of quota data, and when it does not run, nothing breaks -- `ruti route` just
    reports UNKNOWN forever and quietly routes as if the window were nearly spent. The
    two ways it silently dies are both checked here: a config Claude Code rewrote (it
    drops the unsupported `args` key, leaving a bare interpreter that prints nothing),
    and a config that looks right but has never produced a reading.
    """
    from . import install, quota

    try:
        settings = json.loads(install.SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check("statusline", BAD, "settings.json is missing or unreadable",
                     detail="run `ruti install --apply`")

    configured = settings.get("statusLine") or {}
    command = str(configured.get("command") or "")

    if "ruti.statusline" not in command:
        return Check(
            "statusline", BAD,
            "ruti's status line is not registered" if not command
            else "statusLine points somewhere else",
            detail=(
                "without it there is no quota reading at all, and routing falls back to "
                "assuming the window is nearly spent"
                + (" -- note `args` is not a supported statusLine field and is dropped "
                   "by Claude Code" if configured.get("args") else "")
            ),
            fix=lambda: "registered ruti.statusline" if _fix_install() else "",
            fix_label="register it",
        )

    if "\\" in command:
        return Check(
            "statusline", BAD, "the status line command contains backslashes",
            detail=("Claude Code runs it through Git Bash, which consumes them as escapes; "
                    "the command then fails with no visible error. Use forward slashes."),
            fix=lambda: "rewrote the command with forward slashes" if _fix_install() else "",
            fix_label="rewrite it",
        )

    snapshot = quota.load()
    if snapshot.five_hour is None:
        return Check(
            "statusline", WARN, "registered, but no quota reading has ever arrived",
            detail=("restart Claude Code so it picks the setting up; if it is already "
                    "running, the reading appears on the next repaint"),
        )
    if snapshot.freshness in ("unknown", "never"):
        return Check(
            "statusline", WARN,
            f"last reading is stale ({snapshot.freshness})",
            detail="normal between sessions; routing assumes ORANGE until it refreshes",
        )
    return Check(
        "statusline", OK,
        f"live: {snapshot.five_hour.used_percentage:.0f}% of the 5h window used",
    )


def _fix_install() -> bool:
    from . import install

    changes = [c for c in install.plan() if c[0] == "settings.json"]
    if not changes:
        return False
    install.apply(changes)
    return True


def _check_jev() -> Check:
    """Does the task classifier answer, and how fast?

    An unconfigured classifier is not a fault: `route` works exactly as it always did
    without one, which is why this reports `off` rather than a problem. A *configured*
    one that cannot answer is worth naming, because the failure is silent by design --
    `jev.classify` swallows every error and returns None so that a dead endpoint can
    never block a task, and the only visible symptom would be `--describe` quietly
    doing nothing.
    """
    transport = jev.DEFAULT_TRANSPORT
    result = jev.probe(transport=transport)
    if result is None:
        return Check("jev", OK, "off -- no key, so route takes your flags as given",
                     detail=(f"set {jev.TRANSPORTS[transport]['key_env']} to let "
                             "`ruti route --describe` classify a task for you"))

    if not result.get("ok"):
        return Check(
            "jev", WARN, f"configured, but the {transport} endpoint did not answer",
            detail=("`route --describe` will silently fall back to the flags you pass. "
                    "The endpoint is alpha and may have moved; `ruti classify` shows "
                    "the raw failure."),
        )

    age = result.get("age_seconds", 0)
    when = "just now" if not result.get("cached") else f"checked {age // 60} min ago"
    return Check("jev", OK,
                 f"{result['model']} answers in {result['latency_ms']:.0f} ms "
                 f"via {transport}",
                 detail=f"{when}; ${result['cost_usd']:.6f} for that probe")


CHECKS = (
    _check_statusline,
    _check_tls,
    _check_git_tls,
    _check_proxy_bind,
    _check_proxy_alive,
    _check_proxy_task,
    _check_lmstudio,
    _check_generated_sync,
    _check_local_route,
    _check_opencode_drift,
    _check_env_permissions,
    _check_jev,
)


def run_checks() -> Report:
    report = Report()
    for check in CHECKS:
        try:
            report.checks.append(check())
        except Exception as exc:  # A broken check must not hide the healthy ones.
            report.checks.append(
                Check(check.__name__.removeprefix("_check_"), WARN,
                      f"check failed to run: {type(exc).__name__}: {exc}")
            )
    return report

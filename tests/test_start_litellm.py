"""The windowless launcher the RutiLiteLLM task runs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from ruti import doctor, proc

LAUNCHER = Path(__file__).resolve().parent.parent / "litellm" / "start_litellm.pyw"


@pytest.fixture(scope="module")
def launcher():
    loader = importlib.machinery.SourceFileLoader("start_litellm_under_test", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_env_is_parsed_like_the_powershell_script(launcher, tmp_path):
    env = tmp_path / ".env"
    env.write_text("\ufeff# comment\nA_KEY = sk-123 \nSSL_CERT_FILE=C:\\x\\ca.pem\nnot a pair\n",
                   encoding="utf-8")
    assert launcher.load_env(env) == {"A_KEY": "sk-123", "SSL_CERT_FILE": "C:\\x\\ca.pem"}


def test_it_binds_to_loopback_only(launcher, tmp_path):
    argv = launcher.command(tmp_path)
    assert argv[1:] == ["--config", str(tmp_path / "config.yaml"), "--host", "127.0.0.1"]


def test_a_console_launcher_in_the_task_is_flagged(monkeypatch):
    xml = ("<Task><RunLevel>LeastPrivilege</RunLevel><Exec><Command>powershell.exe</Command>"
           "</Exec><Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings></Task>")
    monkeypatch.setattr(proc, "run", lambda argv, **_k: proc.Result(argv, 0, xml, "", 0.0))
    check = doctor._check_proxy_task()
    assert check.status == doctor.WARN and "powershell.exe" in check.message

    xml = xml.replace("powershell.exe", '"C:\\Python312\\pythonw.exe"')
    check = doctor._check_proxy_task()
    assert check.status == doctor.OK


def test_a_taken_port_is_detected(launcher):
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert launcher.port_taken(port=port) is True
    assert launcher.port_taken(port=port) is False


def test_a_second_launch_refuses_instead_of_drifting_to_a_random_port(launcher, tmp_path,
                                                                      monkeypatch):
    # LiteLLM, finding :4000 taken, binds a random port and runs on unreachable. The
    # launcher must not get that far.
    monkeypatch.setattr(launcher, "HERE", tmp_path)
    monkeypatch.setattr(launcher, "port_taken", lambda *a, **k: True)
    monkeypatch.setattr(launcher.subprocess, "call",
                        lambda *a, **k: pytest.fail("started a second proxy"))
    assert launcher.main() == 1
    assert "already taken" in (tmp_path / "litellm.log").read_text(encoding="utf-8")


_PARENT = r"""
import importlib.machinery, importlib.util, subprocess, sys, time
if sys.argv[2] == "job":
    loader = importlib.machinery.SourceFileLoader("launcher", sys.argv[1])
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    assert module.die_with_this_process()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def _grandchild_outlives_parent(tmp_path, mode: str) -> bool:
    """Start parent -> grandchild, terminate the parent the way Task Scheduler does,
    and report whether the grandchild is still running afterwards."""
    import ctypes
    import subprocess
    import sys

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    synchronize_and_terminate = 0x00100000 | 0x0001

    script = tmp_path / "parent.py"
    script.write_text(_PARENT, encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(script), str(LAUNCHER), mode],
                              stdout=subprocess.PIPE, text=True)
    handle = None
    try:
        pid = int(parent.stdout.readline())
        # Opened before the parent dies, so a reused PID cannot be mistaken for it.
        handle = kernel32.OpenProcess(synchronize_and_terminate, False, pid)
        assert handle, "could not open the grandchild"
        parent.kill()  # TerminateProcess, as `Stop-ScheduledTask` does
        parent.wait(timeout=10)
        return kernel32.WaitForSingleObject(handle, 5000) != 0  # 0 = it exited
    finally:
        if parent.poll() is None:
            parent.kill()
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows job objects")
def test_stopping_the_launcher_takes_the_proxy_with_it(tmp_path):
    # Without the job the grandchild survives -- the proxy that kept :4000 ...
    assert _grandchild_outlives_parent(tmp_path, "plain")
    # ... and with it, it goes with the launcher.
    assert not _grandchild_outlives_parent(tmp_path, "job")


# As the README's registration used to leave it: no settings, so the defaults apply.
DEFAULT_TASK = ("<Task><RunLevel>LeastPrivilege</RunLevel><Exec><Command>pythonw.exe</Command>"
                "</Exec><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
                "<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries></Settings></Task>")


def test_a_task_that_would_stop_the_proxy_on_battery_is_flagged(monkeypatch):
    # With the job object, stopping the task stops the proxy -- so the scheduler's
    # battery and 72-hour defaults now matter.
    monkeypatch.setattr(proc, "run",
                        lambda argv, **_k: proc.Result(argv, 0, DEFAULT_TASK, "", 0.0))
    check = doctor._check_proxy_task()
    assert check.status == doctor.WARN and check.fix is doctor._fix_proxy_task_settings
    assert "battery" in check.message


def test_the_settings_fix_keeps_everything_else():
    fixed = doctor._with_proxy_task_settings(DEFAULT_TASK)
    assert doctor._task_settings_problems(DEFAULT_TASK) == [
        "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "ExecutionTimeLimit"]
    assert doctor._task_settings_problems(fixed) == []
    assert fixed.count("<StopIfGoingOnBatteries>") == 1
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in fixed
    assert "<Command>pythonw.exe</Command>" in fixed

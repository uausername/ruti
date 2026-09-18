"""The launcher's watchdog: a proxy that is running but no longer listening is
replaced; one that exits by itself, or keeps dying, is not."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "litellm" / "start_litellm.pyw"


@pytest.fixture
def launcher():
    loader = importlib.machinery.SourceFileLoader("start_litellm_watchdog", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _supervise(launcher, children):
    """Run supervise against scripted children: each is (checks, exit code), where
    checks are "up"/"down" answers to successive health checks and the child exits
    with its code once they run out -- unless it is killed first."""
    clock = [0.0]
    queue = list(children)
    state = {"child": None, "started": 0, "killed": 0, "notes": []}

    class Child:
        def __init__(self, checks, code):
            self.checks, self.code, self.returncode = list(checks), code, None
            self.pid = 1000 + state["started"]

        def poll(self):
            if self.returncode is None and not self.checks:
                self.returncode = self.code
            return self.returncode

    def start():
        state["started"] += 1
        state["child"] = Child(*queue.pop(0))
        return state["child"]

    def healthy():
        child = state["child"]
        if child.returncode is not None or not child.checks:
            return False
        return child.checks.pop(0) == "up"

    def kill(child):
        state["killed"] += 1
        child.returncode = -1

    def sleep(seconds):
        clock[0] += seconds

    code = launcher.supervise(start, healthy, kill, state["notes"].append,
                              sleep=sleep, clock=lambda: clock[0])
    return code, state


def test_a_proxy_that_stops_listening_is_replaced(launcher):
    code, state = _supervise(launcher, [
        (["up", "up", "down", "down", "down"], 0),   # the WinError 64 wedge
        (["up", "up"], 0),                           # its replacement, then a clean exit
    ])
    assert state["started"] == 2 and state["killed"] == 1
    assert code == 0
    assert any("stopped listening" in note for note in state["notes"])


def test_one_missed_check_is_not_a_death(launcher):
    code, state = _supervise(launcher, [(["up", "down", "up", "up"], 0)])
    assert state["started"] == 1 and state["killed"] == 0 and code == 0


def test_a_proxy_that_exits_by_itself_is_not_restarted(launcher):
    # A config error would only fail again.
    code, state = _supervise(launcher, [(["up"], 3)])
    assert code == 3 and state["started"] == 1 and state["killed"] == 0
    assert "exited with 3" in state["notes"][-1]


def test_a_proxy_that_never_binds_is_retried_then_given_up_on(launcher):
    never = (["down"] * 20, 0)
    code, state = _supervise(launcher, [never] * (launcher.MAX_RESTARTS + 1))
    assert code == 1
    assert state["started"] == launcher.MAX_RESTARTS + 1
    assert any("did not bind" in note for note in state["notes"])
    assert "giving up" in state["notes"][-1]


def test_a_slow_start_is_waited_for(launcher):
    checks = int(launcher.STARTUP_SECONDS // launcher.CHECK_EVERY_SECONDS) - 1
    code, state = _supervise(launcher, [(["down"] * checks + ["up", "up"], 0)])
    assert state["killed"] == 0 and code == 0


# The child: listens on PORT and starts a grandchild (as litellm.exe starts python.exe).
# "wedge" then closes its listening socket and stays alive; "serve" exits after a while.
_CHILD = r"""
import socket, subprocess, sys, time
port, mode, pidfile = int(sys.argv[1]), sys.argv[2], sys.argv[3]
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(pidfile, "w").write(str(grandchild.pid))
server = socket.socket()
server.bind(("127.0.0.1", port))
server.listen()
time.sleep(1.0)
if mode == "wedge":
    server.close()
    time.sleep(60)
grandchild.kill()
"""


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill and Windows handles")
def test_the_watchdog_replaces_a_wedged_proxy_tree(launcher, tmp_path, monkeypatch):
    import ctypes

    monkeypatch.setattr(launcher, "CHECK_EVERY_SECONDS", 0.3)
    monkeypatch.setattr(launcher, "STARTUP_SECONDS", 15.0)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    modes = iter(["wedge", "serve"])
    pidfile = tmp_path / "grandchild.pid"
    children, handles = [], []

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def start():
        pidfile.unlink(missing_ok=True)
        child = subprocess.Popen([sys.executable, str(script), str(port), next(modes),
                                  str(pidfile)])
        children.append(child)
        return child

    def kill(child):
        # Opened before the kill, so a reused PID cannot be mistaken for the grandchild.
        handles.append(kernel32.OpenProcess(0x00100000, False, int(pidfile.read_text())))
        launcher.kill_tree(child)

    notes = []
    try:
        code = launcher.supervise(start, lambda: launcher.port_taken(port=port), kill,
                                  notes.append)
        assert code == 0, notes
        assert len(children) == 2 and children[0].poll() is not None
        assert handles and handles[0], "the wedged child was never killed"
        assert kernel32.WaitForSingleObject(handles[0], 5000) == 0, "grandchild survived"
        assert "stopped listening" in notes[0]
    finally:
        for child in children:
            if child.poll() is None:
                launcher.kill_tree(child)
        for handle in handles:
            if handle:
                kernel32.CloseHandle(handle)

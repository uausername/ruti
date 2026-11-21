"""`_check_proxy_bind` reads `netstat -ano`; the label of a live listener varies.

On this Windows host netstat reports the proxy's listening socket as CLOSED or
FIN_WAIT_1 (foreign address 0.0.0.0:0) while the proxy keeps answering. Such a row
needs a successful TCP connection before it can count as a listener.
"""

from __future__ import annotations

from contextlib import nullcontext
import socket
from types import SimpleNamespace

import pytest

from ruti import doctor, proc

HEADER = "  Proto  Local Address          Foreign Address        State           PID\n"
TIME_WAIT = "  TCP    127.0.0.1:61258        127.0.0.1:4000         TIME_WAIT       0\n"


def _run_with(monkeypatch, body: str, *, reachable=True):
    monkeypatch.setattr(proc, "run", lambda *a, **k: SimpleNamespace(stdout=HEADER + body))

    def connect(address, *, timeout):
        assert timeout <= 1.0
        if not reachable:
            raise ConnectionRefusedError("not listening")
        return nullcontext()

    monkeypatch.setattr(socket, "create_connection", connect)
    return doctor._check_proxy_bind()


@pytest.mark.parametrize("state", ["LISTENING", "CLOSED", "FIN_WAIT_1"])
def test_loopback_listener_is_ok_whatever_netstat_calls_it(monkeypatch, state):
    row = f"  TCP    127.0.0.1:4000         0.0.0.0:0              {state:<15} 9892\n"
    check = _run_with(monkeypatch, TIME_WAIT + row)
    assert check.status == doctor.OK
    assert "127.0.0.1:4000" in check.message


@pytest.mark.parametrize("state", ["LISTENING", "CLOSED"])
def test_every_interface_listener_is_bad_whatever_netstat_calls_it(monkeypatch, state):
    row = f"  TCP    0.0.0.0:4000           0.0.0.0:0              {state:<15} 9892\n"
    assert _run_with(monkeypatch, row).status == doctor.BAD


def test_only_time_wait_leftovers_is_still_a_warning(monkeypatch):
    # Client-side sockets connected *to* :4000 are not a listener.
    check = _run_with(monkeypatch, TIME_WAIT)
    assert check.status == doctor.WARN
    assert "nothing is listening" in check.message


@pytest.mark.parametrize("state", ["CLOSED", "FIN_WAIT_1"])
@pytest.mark.parametrize("local", ["127.0.0.1:4000", "0.0.0.0:4000", "[::1]:4000", "[::]:4000"])
def test_no_peer_without_accepting_connections_is_not_a_listener(monkeypatch, state, local):
    row = f"TCP {local} 0.0.0.0:0 {state} 9892\n"
    assert _run_with(monkeypatch, row, reachable=False).status == doctor.WARN


@pytest.mark.parametrize("body", [
    "UDP 0.0.0.0:4000 *:* 9892\n",
    "UDP [::]:4000 *:* 9892\n",
    "TCP 127.0.0.1:40001 0.0.0.0:0 LISTENING 9892\n",
    "TCP 127.0.0.1:61258 127.0.0.1:4000 LISTENING 9892\n",
    "TCP 127.0.0.1:4000 192.0.2.1:80 FIN_WAIT_1 9892\n",
    "TCP 127.0.0.1:4000\n",
])
def test_unrelated_or_malformed_rows_never_trigger_a_probe(monkeypatch, body):
    def unexpected(*args, **kwargs):
        pytest.fail("Only a peerless TCP endpoint on local port 4000 may be probed")

    monkeypatch.setattr(proc, "run", lambda *a, **k: SimpleNamespace(stdout=HEADER + body))
    monkeypatch.setattr(socket, "create_connection", unexpected)
    assert doctor._check_proxy_bind().status == doctor.WARN


@pytest.mark.parametrize("local,host,status", [
    ("127.0.0.1:4000", "127.0.0.1", doctor.OK),
    ("0.0.0.0:4000", "127.0.0.1", doctor.BAD),
    ("[::1]:4000", "::1", doctor.OK),
    ("[::]:4000", "::1", doctor.BAD),
])
def test_ambiguous_endpoint_is_probed_at_its_own_address(monkeypatch, local, host, status):
    row = f"TCP {local} [::]:0 CLOSED 9892\n"
    monkeypatch.setattr(proc, "run", lambda *a, **k: SimpleNamespace(stdout=HEADER + row))
    calls = []

    def connect(address, *, timeout):
        calls.append((address, timeout))
        return nullcontext()

    monkeypatch.setattr(socket, "create_connection", connect)
    assert doctor._check_proxy_bind().status == status
    assert calls == [((host, 4000), 0.5)]


def test_loopback_row_cannot_hide_a_wildcard_listener(monkeypatch):
    rows = "TCP 127.0.0.1:4000 0.0.0.0:0 LISTENING 9892\nTCP [::]:4000 [::]:0 LISTENING 9893\n"
    assert _run_with(monkeypatch, rows).status == doctor.BAD


def test_explicit_listener_does_not_need_a_connect_probe(monkeypatch):
    row = "TCP 127.0.0.1:4000 0.0.0.0:0 LISTENING 9892\n"
    assert _run_with(monkeypatch, row, reachable=False).status == doctor.OK


def test_missing_endpoint_is_rechecked_before_reporting_no_listener(monkeypatch):
    snapshots = iter([TIME_WAIT, "TCP 127.0.0.1:4000 0.0.0.0:0 LISTENING 9892\n"])
    monkeypatch.setattr(proc, "run", lambda *a, **k: SimpleNamespace(stdout=HEADER + next(snapshots)))
    assert doctor._check_proxy_bind().status == doctor.OK


def test_reachable_proxy_without_bind_evidence_still_warns(monkeypatch):
    # A TCP connect alone cannot distinguish loopback-only from wildcard binding.
    assert _run_with(monkeypatch, TIME_WAIT, reachable=True).status == doctor.WARN

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ruti import footer_facts as facts


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "FACTS_FILE", tmp_path / "facts.json")
    monkeypatch.setattr(facts, "LOCK_FILE", tmp_path / "lease")


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("at", [100, 90, 80])
def test_fresh(at):
    write(facts.FACTS_FILE, {"at": at})
    spawn = Mock()
    assert not facts.refresh_in_background(now=100, spawn=spawn)
    spawn.assert_not_called()


@pytest.mark.parametrize("at", [None, "bad", True, float("nan"), float("inf"), 101, 79])
def test_invalid_or_expired(at):
    write(facts.FACTS_FILE, {"at": at})
    assert facts.refresh_in_background(now=100, spawn=Mock())


def test_corrupt_cache():
    facts.FACTS_FILE.write_text("{")
    assert facts.refresh_in_background(now=100, spawn=Mock())


def test_exclusive_lease_and_token_cleanup():
    assert facts.refresh_in_background(now=100, spawn=Mock())
    token = facts._read(facts.LOCK_FILE)["token"]
    assert not facts.refresh_in_background(now=100, spawn=Mock())
    facts._release("different")
    assert facts.LOCK_FILE.exists()
    facts._release(token)
    assert not facts.LOCK_FILE.exists()


def test_busy_os_guard_does_not_wait_or_spawn():
    spawn = Mock()
    with facts._lease_guard() as acquired:
        assert acquired
        assert not facts.refresh_in_background(now=100, spawn=spawn)
    spawn.assert_not_called()


def test_concurrent_stale_recovery_starts_only_one_worker():
    from concurrent.futures import ThreadPoolExecutor
    import threading

    write(facts.LOCK_FILE, {"at": 1, "token": "crashed"})
    os.utime(facts.LOCK_FILE, (1, 1))
    barrier = threading.Barrier(8)
    def request(_):
        barrier.wait(timeout=5)
        return facts.refresh_in_background(now=100, spawn=Mock())
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(8))) == 1


@pytest.mark.parametrize("payload", [{"at": 1, "token": "old"}, None])
def test_stale_lease(payload):
    write(facts.LOCK_FILE, payload)
    os.utime(facts.LOCK_FILE, (1, 1))
    assert facts.refresh_in_background(now=100, spawn=Mock())


def test_failed_spawn():
    assert not facts.refresh_in_background(now=100, spawn=Mock(side_effect=OSError))
    assert not facts.LOCK_FILE.exists()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_child_contract(platform, monkeypatch):
    monkeypatch.setattr(facts.sys, "platform", platform)
    monkeypatch.setenv("RUTI_HOME", "retained")
    spawn = Mock()
    assert facts.refresh_in_background(now=100, spawn=spawn)
    args, kwargs = spawn.call_args
    token = facts._read(facts.LOCK_FILE)["token"]
    exe = Path(facts.sys.executable).absolute()
    assert args == ([str(exe), "-I", "-X", "utf8", "-m", "ruti.footer_facts",
                     "--refresh", "--lock-token", token],)
    assert kwargs["cwd"] == str(exe.parent)
    for name in ("stdin", "stdout", "stderr"):
        assert kwargs[name] == facts.subprocess.DEVNULL
    assert "env" not in kwargs
    if platform == "win32":
        assert kwargs["creationflags"] & 0x08000008 == 0x08000008
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True


def test_collect(monkeypatch):
    get = Mock(side_effect=[{}, {"data": [None, {"state": "unloaded"},
        {"state": "loaded", "id": "a" * 300, "loaded_context_length": 4096},
        {"state": "loaded", "id": "b", "loaded_context_length": "invalid"}]}])
    monkeypatch.setattr(facts, "_get", get)
    run = Mock(return_value=SimpleNamespace(ok=True, stdout="100, 200\n"))
    monkeypatch.setattr(facts.proc, "run", run)
    result = facts._collect()
    assert result["proxy"] is True
    assert result["loaded_ok"] is True
    assert result["loaded"] == [{"id": "a" * 256, "ctx": 4096}, {"id": "b", "ctx": None}]
    assert result["gpu"] == {"free_mib": 100, "total_mib": 200}
    assert [c.args[0] for c in get.call_args_list] == [
        facts.config.PROXY_BASE + "/health/liveliness",
        facts.config.LMSTUDIO_BASE + "/api/v0/models"]
    assert run.call_args.kwargs == {"timeout": 2.0}


@pytest.mark.parametrize("models,ok", [({"data": []}, True), ({"data": {}}, False),
                                       ([], False), (None, False)])
def test_empty_or_malformed(monkeypatch, models, ok):
    monkeypatch.setattr(facts, "_get", Mock(side_effect=[OSError(), models]))
    monkeypatch.setattr(facts.proc, "run", Mock(side_effect=OSError))
    result = facts._collect()
    assert result["proxy"] is False
    assert result["loaded_ok"] is ok
    assert result["loaded"] == []
    assert result["gpu"] is None


def test_worker_failure_releases(monkeypatch):
    write(facts.LOCK_FILE, {"token": "ours"})
    monkeypatch.setattr(facts, "_collect", Mock(side_effect=RuntimeError))
    facts._worker("ours")
    assert not facts.LOCK_FILE.exists()


def test_worker_writes_and_respects_successor(monkeypatch):
    write(facts.LOCK_FILE, {"token": "ours"})
    monkeypatch.setattr(facts, "_collect", lambda: {"at": 100})
    facts._worker("ours")
    assert facts._read(facts.FACTS_FILE) == {"at": 100}
    write(facts.LOCK_FILE, {"token": "new"})
    facts._worker("ours")
    assert facts._read(facts.LOCK_FILE) == {"token": "new"}


def test_http_contract(monkeypatch):
    response = Mock()
    response.status = 200
    response.read.return_value = b'{"data": []}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(facts.urllib.request, "build_opener", build)
    assert facts._get("http://127.0.0.1/api/v0/models") == {"data": []}
    assert build.call_args.args[0].proxies == {}
    opener.open.assert_called_once_with("http://127.0.0.1/api/v0/models", timeout=0.4)
    response.read.assert_called_once_with(facts.MAX_BYTES + 1)
    assert build.call_args.args[1].redirect_request(None) is None


def test_failed_spawn_preserves_new_token():
    def spawn(*args, **kwargs):
        write(facts.LOCK_FILE, {"token": "successor"})
        raise OSError()
    assert not facts.refresh_in_background(now=100, spawn=spawn)
    assert facts._read(facts.LOCK_FILE)["token"] == "successor"


def test_worker_loses_lease_during_probe(monkeypatch):
    write(facts.LOCK_FILE, {"token": "ours"})
    def collect():
        write(facts.LOCK_FILE, {"token": "successor"})
        return {"at": 100}
    monkeypatch.setattr(facts, "_collect", collect)
    facts._worker("ours")
    assert not facts.FACTS_FILE.exists()
    assert facts._read(facts.LOCK_FILE)["token"] == "successor"


def test_background_never_probes(monkeypatch):
    get = Mock(side_effect=AssertionError("probe on UI path"))
    run = Mock(side_effect=AssertionError("GPU on UI path"))
    monkeypatch.setattr(facts, "_get", get)
    monkeypatch.setattr(facts.proc, "run", run)
    assert facts.refresh_in_background(now=100, spawn=Mock())
    get.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("output", ["", "bad", "-1, 100", "200, 100"])
def test_invalid_gpu(monkeypatch, output):
    monkeypatch.setattr(facts, "_get", Mock(side_effect=OSError))
    monkeypatch.setattr(facts.proc, "run", Mock(return_value=SimpleNamespace(ok=True, stdout=output)))
    assert facts._collect()["gpu"] is None

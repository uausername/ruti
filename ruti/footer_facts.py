"""Asynchronous local machine facts; safe to request from a footer repaint."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import urllib.request

from . import config, proc

FACTS_FILE = config.STATE_ROOT / "facts.json"
LOCK_FILE = config.STATE_ROOT / "footer-facts.lock"
FACTS_TTL = 20.0
LOCK_TTL = 60.0
MAX_BYTES = 65536


def _read(path: Path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        return json.loads(raw) if len(raw) <= MAX_BYTES else None
    except (OSError, ValueError):
        return None


def _timestamp(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


@contextmanager
def _lease_guard():
    """Serialize lease replacement without waiting on a footer repaint.

    This guard file is never deleted. The OS releases its advisory lock on exit,
    including crashes, so concurrent stale-lease recovery cannot unlink a new owner.
    """
    stream = None
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        stream = LOCK_FILE.with_suffix(".guard").open("a+b")
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if stream is not None:
            stream.close()
        yield False
        return
    try:
        yield True
    finally:
        stream.close()


def _release(token: str) -> None:
    try:
        with _lease_guard() as acquired:
            if acquired:
                lease = _read(LOCK_FILE)
                if isinstance(lease, dict) and lease.get("token") == token:
                    LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _acquire(now: float, token: str) -> bool:
    with _lease_guard() as acquired:
        return _acquire_locked(now, token) if acquired else False


def _acquire_locked(now: float, token: str) -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            with LOCK_FILE.open("x", encoding="utf-8") as stream:
                json.dump({"at": now, "token": token}, stream)
            return True
        except FileExistsError:
            # mtime also protects a lease whose creator has not written its JSON yet.
            age = now - LOCK_FILE.stat().st_mtime
            lease = _read(LOCK_FILE)
            at = lease.get("at") if isinstance(lease, dict) else None
            if age <= LOCK_TTL and (not _timestamp(at) or now - at <= LOCK_TTL):
                return False
            LOCK_FILE.unlink(missing_ok=True)
    return False


def refresh_in_background(now=None, spawn=None) -> bool:
    """Request a refresh without probing services or waiting for the worker."""
    token = uuid.uuid4().hex
    try:
        now = time.time() if now is None else now
        if not _timestamp(now):
            return False
        cached = _read(FACTS_FILE)
        at = cached.get("at") if isinstance(cached, dict) else None
        if _timestamp(at) and 0 <= now - at <= FACTS_TTL:
            return False
        if not _acquire(now, token):
            return False
        executable = Path(sys.executable).absolute()
        kwargs = dict(cwd=str(executable.parent), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                       | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        else:
            kwargs["start_new_session"] = True
        (spawn or subprocess.Popen)(
            [str(executable), "-I", "-X", "utf8", "-m", "ruti.footer_facts",
             "--refresh", "--lock-token", token], **kwargs)
        return True
    except Exception:
        _release(token)
        return False


def _get(url: str):
    # No environment proxies or redirects: these GETs must stay on the local service.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=0.4) as response:
        if response.status != 200:
            raise ValueError("unsuccessful local GET")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized local response")
        return json.loads(raw)


def _collect() -> dict:
    facts = {"at": time.time(), "proxy": False, "loaded": [],
             "loaded_ok": False, "gpu": None}
    try:
        _get(config.PROXY_BASE + "/health/liveliness")
        facts["proxy"] = True
    except Exception:
        pass
    try:
        models = _get(config.LMSTUDIO_BASE + "/api/v0/models")
        if isinstance(models, dict) and isinstance(models.get("data"), list):
            facts["loaded_ok"] = True
            for model in models["data"][:128]:
                if not isinstance(model, dict) or model.get("state") != "loaded":
                    continue
                name = model.get("id")
                if not isinstance(name, str) or not name:
                    continue
                ctx = model.get("loaded_context_length")
                if type(ctx) is not int or not 0 <= ctx <= 2**31 - 1:
                    ctx = None
                facts["loaded"].append({"id": name[:256], "ctx": ctx})
    except Exception:
        pass
    try:
        result = proc.run(["nvidia-smi", "--query-gpu=memory.free,memory.total",
                           "--format=csv,noheader,nounits"], timeout=2.0)
        if result.ok:
            free, total = map(int, result.stdout.splitlines()[0].split(","))
            if 0 <= free <= total:
                facts["gpu"] = {"free_mib": free, "total_mib": total}
    except Exception:
        pass
    return facts


def _worker(token: str) -> None:
    try:
        lease = _read(LOCK_FILE)
        if isinstance(lease, dict) and lease.get("token") == token:
            facts = _collect()
            # An expired worker must not overwrite its successor's cache.
            with _lease_guard() as acquired:
                lease = _read(LOCK_FILE) if acquired else None
                if isinstance(lease, dict) and lease.get("token") == token:
                    config.write_json(FACTS_FILE, facts)
                    LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    finally:
        _release(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", required=True)
    parser.add_argument("--lock-token", required=True)
    args = parser.parse_args()
    _worker(args.lock_token)


if __name__ == "__main__":
    main()

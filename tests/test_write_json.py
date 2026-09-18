"""`write_json` must not leave temp files behind, whatever happens to the replace."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from ruti import config


def _tmps(directory):
    return sorted(p.name for p in directory.glob("*.tmp"))


def test_a_refused_replace_cleans_up_before_raising(tmp_path, monkeypatch):
    def refuse(*_args):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(config.os, "replace", refuse)
    monkeypatch.setattr(config.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        config.write_json(tmp_path / "quota.json", {"a": 1})
    assert _tmps(tmp_path) == []


def test_a_brief_sharing_violation_is_waited_out(tmp_path, monkeypatch):
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", flaky)
    monkeypatch.setattr(config.time, "sleep", lambda _s: None)
    config.write_json(tmp_path / "quota.json", {"a": 1})
    assert json.loads((tmp_path / "quota.json").read_text(encoding="utf-8")) == {"a": 1}
    assert _tmps(tmp_path) == []


@pytest.mark.skipif(sys.platform != "win32", reason="the sharing violation is Windows-only")
def test_a_reader_holding_the_file_open_does_not_cost_the_write(tmp_path):
    target = tmp_path / "facts.json"
    target.write_text("{}", encoding="utf-8")
    handle = target.open(encoding="utf-8")  # exactly what another status line does
    threading.Timer(0.1, handle.close).start()

    config.write_json(target, {"fresh": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"fresh": True}
    assert _tmps(tmp_path) == []


def test_abandoned_temp_files_are_swept_but_a_live_write_is_not(tmp_path):
    old = tmp_path / "facts.json.1234.tmp"
    old.write_text("{}", encoding="utf-8")
    stale = time.time() - config.STALE_TMP_SECONDS - 5
    os.utime(old, (stale, stale))
    in_flight = tmp_path / "facts.json.5678-abcd.tmp"
    in_flight.write_text("{}", encoding="utf-8")
    other_file = tmp_path / "quota.json.1234.tmp"
    other_file.write_text("{}", encoding="utf-8")
    os.utime(other_file, (stale, stale))

    config.write_json(tmp_path / "facts.json", {})
    # its own stale sibling goes; a fresh one may be another writer mid-flight, and
    # another file's leftovers are that file's write to clear
    assert _tmps(tmp_path) == ["facts.json.5678-abcd.tmp", "quota.json.1234.tmp"]

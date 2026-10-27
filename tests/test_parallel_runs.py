import json
import time

import pytest

from ruti import delegate, runs, sessions, usage


def test_finishing_one_run_keeps_other_active(tmp_path):
    marker = tmp_path / "running.json"
    runs.start(marker, "ruti-router/free", 60, "aaaaaaaa", "session-a")
    runs.start(marker, "ruti-router/free", 60, "bbbbbbbb", "session-b")
    assert {r["run_id"] for r in runs.active(marker)} == {"aaaaaaaa", "bbbbbbbb"}
    runs.finish(marker, "aaaaaaaa")
    assert [r["run_id"] for r in runs.active(marker)] == ["bbbbbbbb"]


def test_bad_and_expired_markers_do_not_hide_running_jobs(tmp_path):
    marker = tmp_path / "running.json"
    runs.start(marker, "ruti-router/free", 60, "aaaaaaaa")
    runs.start(marker, "ruti-router/free", -1, "bbbbbbbb")
    (marker.with_suffix(".d") / "corrupt.json").write_text("{", encoding="utf-8")
    (marker.with_suffix(".d") / "invalid.json").write_text(
        json.dumps({"model": "x", "expires_at": "never"}), encoding="utf-8")
    assert [r["run_id"] for r in runs.active(marker)] == ["aaaaaaaa"]
    with pytest.raises(ValueError):
        runs.finish(marker, "../running")


def test_identified_usage_excludes_overlapping_and_legacy_records():
    entries = [
        {"requested": "free", "at": 2, "run_id": "aaaaaaaa", "model": "one", "cost_usd": 0.1},
        {"requested": "free", "at": 2, "run_id": "bbbbbbbb", "model": "two", "cost_usd": 0.2},
        {"requested": "free", "at": 2, "model": "legacy", "cost_usd": 5},
    ]
    first = usage.aggregate("free", usage.in_window(entries, "free", 1, 3, run_id="aaaaaaaa"))
    second = usage.aggregate("free", usage.in_window(entries, "free", 1, 3, run_id="bbbbbbbb"))
    assert (first.model, first.cost_usd, first.requests) == ("one", 0.1, 1)
    assert (second.model, second.cost_usd, second.requests) == ("two", 0.2, 1)
    assert len(usage.in_window(entries, "free", 1, 3)) == 3


def test_running_alias_matches_session_not_other_job(tmp_path, monkeypatch):
    marker = tmp_path / "running.json"
    monkeypatch.setattr(delegate, "RUNNING_FILE", marker)
    runs.start(marker, "ruti-router/free", 60, "aaaaaaaa", "session-a")
    runs.start(marker, "ruti-router/other", 60, "bbbbbbbb", "session-b")
    assert delegate.running_for("free", "session-a")
    assert not delegate.running_for("free", "session-b")


def test_unattributed_usage_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_LOG", tmp_path / "usage.jsonl")
    usage.USAGE_LOG.write_text(json.dumps({"requested": "free", "at": 2, "model": "other"}) + "\n")
    result = usage.collect("free", 1, 3, run_id="aaaaaaaa", settle=0)
    assert result.model == "unknown" and result.cost_usd is None
    assert "restart the proxy" in result.note

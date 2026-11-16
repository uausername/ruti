"""Claude + Codex quota as one gate for starting a Codex worker."""

from __future__ import annotations

import json
import os

from ruti.operator import quota

NOW = 1_800_000_000.0


def rollout(path, sid, at, five, week, reset5=NOW + 3600, reset7=NOW + 86400):
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = lambda t: __import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc) \
        .isoformat().replace("+00:00", "Z")  # noqa: E731
    rows = [
        {"type": "session_meta", "timestamp": ts(at - 60), "payload": {"id": sid}},
        {"type": "event_msg", "timestamp": ts(at), "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": five, "window_minutes": 300, "resets_at": reset5},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": reset7}}}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    os.utime(path, (at, at))


def test_freshest_codex_rollout_wins_across_dirs(tmp_path):
    wsl, win = tmp_path / "wsl", tmp_path / "win"
    rollout(wsl / "2026/11/16/rollout-a.jsonl", "a", NOW - 3600, 10, 30)
    rollout(win / "2026/11/16/rollout-b.jsonl", "b", NOW - 60, 40, 50)
    r = quota.codex_reading([wsl, win], now=NOW)
    assert r["five_hour"]["used_percentage"] == 40 and r["_source"].endswith("rollout-b.jsonl")


def test_old_rollouts_are_ignored(tmp_path):
    rollout(tmp_path / "rollout-old.jsonl", "o", NOW - 30 * 86400, 99, 99)
    assert quota.codex_reading([tmp_path], now=NOW) == {}


def test_gate_open():
    g = quota.gate(now=NOW, claude={}, codex={"five_hour": {"used_percentage": 10, "resets_at": NOW + 99}},
                   live_codex=0)
    assert g["ok"] and g["reasons"] == []


def test_gate_codex_five_hour_at_70():
    codex = {"five_hour": {"used_percentage": 70, "resets_at": NOW + 600}}
    g = quota.gate(now=NOW, claude={}, codex=codex, live_codex=0)
    assert not g["ok"] and "Codex" in g["reasons"][0]


def test_gate_codex_week_at_85_but_reset_passed_is_open():
    codex = {"seven_day": {"used_percentage": 90, "resets_at": NOW - 1}}
    assert quota.gate(now=NOW, claude={}, codex=codex, live_codex=0)["ok"]


def test_gate_claude_spent():
    claude = {"captured_at_epoch": NOW - 60, "five_hour": {"used_percentage": 96, "resets_at": NOW + 600}}
    g = quota.gate(now=NOW, claude=claude, codex={}, live_codex=0)
    assert not g["ok"] and "Claude" in g["reasons"][0]


def test_gate_parallel_limit():
    assert quota.gate(now=NOW, claude={}, codex={}, live_codex=3)["ok"]
    assert not quota.gate(now=NOW, claude={}, codex={}, live_codex=4)["ok"]
    assert not quota.gate(now=NOW, claude={}, codex={}, live_codex=2, max_parallel=2)["ok"]


def test_live_codex_count():
    rows = {"harnesses": [{"agent": "Codex", "state": "working"}, {"agent": "Codex", "state": "idle"},
                          {"agent": "Claude Code", "state": "idle"}, {"agent": "Codex", "state": "closed"}]}
    assert quota.live_codex_count(lambda argv: json.dumps(rows)) == 2
    assert quota.live_codex_count(lambda argv: "nope") is None

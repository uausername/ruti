"""The master waker, driven by a fake harness/tmux and a fake clock."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from ruti.operator import waker
from ruti.operator.waker import Waker

FOLDER = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees/haccp-master-x"
NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class Fake:
    def __init__(self, state="idle", last=NOW - 600, pane=True, after_paste="working", new_ok=True):
        self.rows = [{"id": "old", "agent": "Claude Code", "state": state, "folder": FOLDER,
                      "lastActive": iso(last)}] if state else []
        self.pane, self.after_paste, self.new_ok = pane, after_paste, new_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["harness", "ps"]:
            return json.dumps({"harnesses": self.rows})
        if argv[:2] == ["tmux", "list-panes"]:
            return f"%1|claude|{FOLDER}\n%0|bash|/home/dima\n" if self.pane else "%0|bash|/home/dima\n"
        if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "Enter":
            if self.after_paste and self.rows:
                self.rows[0]["state"] = self.after_paste
            return ""
        if argv[:2] == ["tmux", "load-buffer"]:
            self.pasted = open(argv[-1], encoding="utf-8").read()
            return ""
        if argv[:1] == ["tmux"]:
            return ""
        if argv[:2] == ["harness", "new"]:
            if not self.new_ok:
                return json.dumps({"ok": False})
            self.rows.append({"id": "new", "agent": "Claude Code", "state": "working", "folder": FOLDER})
            return json.dumps({"ok": True, "agent": {"id": "new"}})
        if argv[:2] == ["harness", "close"]:
            self.rows = [r for r in self.rows if r["id"] != argv[2]]
            return ""
        raise AssertionError(argv)

    def names(self):
        return [" ".join(c[:2]) for c in self.calls]


@pytest.fixture(autouse=True)
def folder_exists(monkeypatch):
    monkeypatch.setattr(waker.Path, "is_dir",
                        lambda self: True if str(self).endswith("haccp-master-x") else os.path.isdir(self))


def test_missing_master_folder_fails_without_starting(tmp_path, monkeypatch):
    monkeypatch.setattr(waker.Path, "is_dir",
                        lambda self: False if str(self).endswith("haccp-master-x") else os.path.isdir(self))
    fake = Fake(state=None)
    w = make(tmp_path, fake)
    arm(w)
    row = w.tick()
    assert row["event"] == "failed" and "no longer exists" in row["reason"]
    assert not any(c[:2] == ["harness", "new"] for c in fake.calls)
    assert (tmp_path / "needs_admin.jsonl").exists()


def make(tmp_path, fake, quota=None, now=NOW):
    return Waker(run=fake, sleep=lambda s: None, now=lambda: now,
                 quota=lambda: quota or {}, root=tmp_path)


def arm(w, after=NOW - 1, mode="auto"):
    w.save(waker.request(harness_id="old", folder=FOLDER, checkpoint="/x/MASTER_RESUME.md",
                         after=after, reason="weekly quota 96%", mode=mode, model="opus", now=NOW - 7200))


def test_not_due_does_nothing(tmp_path):
    fake = Fake()
    w = make(tmp_path, fake)
    arm(w, after=NOW + 60)
    assert w.tick() is None and fake.calls == []


def test_manual_is_never_woken(tmp_path):
    fake = Fake()
    w = make(tmp_path, fake)
    arm(w, mode="manual")
    assert w.tick() is None and fake.calls == []


def test_spent_quota_postpones_to_reset(tmp_path):
    fake = Fake()
    w = make(tmp_path, fake, quota={"seven_day": {"used_percentage": 98, "resets_at": NOW + 3600}})
    arm(w)
    row = w.tick()
    assert row["event"] == "postponed" and w.load()["after"] == NOW + 3600 + 120
    assert fake.calls == []


def test_stale_reading_uses_stricter_limits(tmp_path):
    q = {"captured_at_epoch": NOW - 5 * 86400,
         "five_hour": {"used_percentage": 85, "resets_at": NOW + 600}}
    fake = Fake()
    w = make(tmp_path, fake, quota=q)
    arm(w)
    assert w.tick()["event"] == "postponed"
    assert fake.calls == []


def test_fresh_reading_below_limit_does_not_block(tmp_path):
    q = {"captured_at_epoch": NOW - 60, "five_hour": {"used_percentage": 85, "resets_at": NOW + 600}}
    w = make(tmp_path, Fake(), quota=q)
    arm(w)
    assert w.tick()["event"] == "done"


def test_freshest_source_wins(tmp_path):
    old, new = tmp_path / "wsl.json", tmp_path / "win.json"
    old.write_text(json.dumps({"captured_at_epoch": NOW - 86400 * 5,
                               "five_hour": {"used_percentage": 61}}), encoding="utf-8")
    new.write_text(json.dumps({"captured_at_epoch": NOW - 60,
                               "five_hour": {"used_percentage": 81}}), encoding="utf-8")
    q = waker.load_quota([old, tmp_path / "missing.json", new])
    assert q["five_hour"]["used_percentage"] == 81 and q["_source"] == str(new)


def test_quota_that_already_reset_does_not_block(tmp_path):
    fake = Fake()
    w = make(tmp_path, fake, quota={"five_hour": {"used_percentage": 100, "resets_at": NOW - 10}})
    arm(w)
    assert w.tick()["event"] == "done"


def test_recent_idle_master_is_poked_in_its_pane(tmp_path):
    fake = Fake(last=NOW - 600)
    w = make(tmp_path, fake)
    arm(w)
    row = w.tick()
    assert row["event"] == "done" and row["how"] == "poked"
    assert "MASTER_RESUME.md" in fake.pasted
    assert "harness new" not in fake.names()
    assert w.load()["status"] == "done"


def test_long_idle_master_gets_a_fresh_session_and_old_is_closed(tmp_path):
    fake = Fake(last=NOW - 4 * 86400)
    w = make(tmp_path, fake)
    arm(w)
    row = w.tick()
    assert row["how"] == "new session" and row["new_id"] == "new" and row["closed"] == "old"
    new = next(c for c in fake.calls if c[:2] == ["harness", "new"])
    assert FOLDER in new and "--model" in new and "opus" in new
    assert "MASTER_RESUME.md" in new[new.index("--task") + 1]
    assert ["harness", "close", "old"] in fake.calls


def test_dead_master_gets_a_fresh_session(tmp_path):
    fake = Fake(state=None)
    w = make(tmp_path, fake)
    arm(w)
    assert w.tick()["how"] == "new session"
    assert not any(c[:2] == ["harness", "close"] for c in fake.calls)


def test_master_without_unique_pane_gets_a_fresh_session(tmp_path):
    fake = Fake(last=NOW - 60, pane=False)
    w = make(tmp_path, fake)
    arm(w)
    assert w.tick()["how"] == "new session"


def test_working_master_is_left_alone(tmp_path):
    fake = Fake(state="working")
    w = make(tmp_path, fake)
    arm(w)
    assert w.tick()["how"] == "already working"
    assert not any(c[0] == "tmux" or c[:2] == ["harness", "new"] for c in fake.calls)


def test_poke_that_does_not_start_work_retries_then_fails(tmp_path):
    fake = Fake(last=NOW - 60, after_paste=None)
    w = make(tmp_path, fake)
    arm(w)
    assert w.tick()["event"] == "retry"
    assert w.load()["after"] == NOW + 300
    for _ in range(2):
        req = w.load()
        req["after"] = NOW - 1
        w.save(req)
        row = w.tick()
    assert row["event"] == "failed" and w.load()["status"] == "failed"
    assert (tmp_path / "needs_admin.jsonl").exists()
    assert w.tick() is None  # failed requests are not retried forever


def test_failed_harness_new_keeps_old_master(tmp_path):
    fake = Fake(last=NOW - 86400, new_ok=False)
    w = make(tmp_path, fake)
    arm(w)
    assert w.tick()["event"] == "retry"
    assert not any(c[:2] == ["harness", "close"] for c in fake.calls)


def test_harness_ps_failure_sends_nothing(tmp_path):
    w = make(tmp_path, lambda argv: "not json")
    arm(w)
    assert w.tick()["event"] == "error"
    assert w.load()["status"] == "pending"


@pytest.mark.parametrize("value,expected", [
    ("+90m", NOW + 5400), ("+2h", NOW + 7200), ("+30s", NOW + 30),
    ("2027-01-15T15:00:00Z", 1_800_025_200.0), ("1800000100", 1_800_000_100.0),
])
def test_parse_after(value, expected):
    assert waker.parse_after(value, NOW) == expected


def test_parse_after_quota():
    q = {"five_hour": {"used_percentage": 96, "resets_at": NOW + 100},
         "seven_day": {"used_percentage": 98, "resets_at": NOW + 900}}
    assert waker.parse_after("quota", NOW, q) == NOW + 900
    assert waker.parse_after("quota", NOW, {}) == NOW


def test_cli_resume_at_and_cancel(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ruti.cli import main

    fake = Fake()
    monkeypatch.setattr(waker, "Waker", lambda: Waker(run=fake, sleep=lambda s: None, root=tmp_path))
    monkeypatch.setattr(waker, "load_quota", lambda: {})
    r = CliRunner().invoke(main, ["operator", "resume-at", "--after", "+1h", "--checkpoint", "cp.md",
                                  "--folder", FOLDER, "--reason", "test"])
    assert r.exit_code == 0, r.output
    req = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert req["harness_id"] == "old" and req["status"] == "pending" and req["checkpoint"].endswith("cp.md")
    r = CliRunner().invoke(main, ["operator", "resume-at", "--cancel"])
    assert json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))["status"] == "cancelled"

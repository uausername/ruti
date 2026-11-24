"""Administrator helpers: telling the master, collecting side-agent cards."""

from __future__ import annotations

import json

import pytest

from ruti.operator import admin

M = "/mnt/c/mycode/haccp/.ruti/h/worktrees/haccp-master-20261116"


class Fake:
    def __init__(self, states, panes=f"%21|claude|{M}\n%2|bash|/home/x\n"):
        self.states, self.panes, self.calls, self.pasted = list(states), panes, [], None

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["harness", "ps"]:
            st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps({"harnesses": [
                {"id": "m", "name": "master", "agent": "Claude Code", "state": st, "folder": M},
                {"id": "c", "name": "codex-x", "agent": "Codex", "state": "working", "folder": "/x/codex-x"}]})
        if argv[:2] == ["tmux", "list-panes"]:
            return self.panes
        if argv[:2] == ["tmux", "load-buffer"]:
            self.pasted = open(argv[-1], encoding="utf-8").read()
        return ""


def test_tell_pastes_into_the_master_pane_and_presses_enter():
    f = Fake(["working"])
    r = admin.tell_master("продолжай", run=f, sleep=lambda s: None)
    assert r["pane"] == "%21" and f.pasted == "продолжай"
    assert f.calls[-1] == ["tmux", "send-keys", "-t", "%21", "Enter"]


def test_when_idle_waits_for_idle():
    f = Fake(["working", "working", "idle"])
    r = admin.tell_master("/model sonnet", when_idle=True, run=f, sleep=lambda s: None)
    assert r["state_before"] == "idle" and r["waited_s"] == 30


def test_two_panes_in_the_master_folder_refuse():
    f = Fake(["idle"], panes=f"%21|claude|{M}\n%22|claude|{M}\n")
    with pytest.raises(admin.TellError):
        admin.tell_master("x", run=f, sleep=lambda s: None)


def test_ysk_collects_new_cards_once_and_tracks_reading(tmp_path):
    store = tmp_path / "cc-plugin-you-should-know_builtin-1.json"
    store.write_text(json.dumps({"sessions": [
        {"id": "s1", "tag": "Heads up", "view": {"kind": "offer", "line": "deploy-functions выкатит все функции",
                                                 "explanation": "**…**", "shownAt": 1}},
        {"id": "s2", "tag": "Heads up"}]}), encoding="utf-8")
    feed, mark = tmp_path / "ysk.jsonl", tmp_path / "ysk.read"
    assert len(admin.collect_ysk(feed, [store])) == 1
    assert admin.collect_ysk(feed, [store]) == []
    assert [c["session"] for c in admin.unread_ysk(feed, mark)] == ["s1"]
    admin.mark_read(feed, mark)
    assert admin.unread_ysk(feed, mark) == []

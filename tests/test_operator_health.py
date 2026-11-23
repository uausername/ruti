"""Login watchdog: notify once on logout, repeat every 2 h, once more on recovery."""

from __future__ import annotations

import base64
import json
import subprocess

from ruti.operator import health


def cp(out, rc=0):
    return subprocess.CompletedProcess([], rc, out, "")


def test_claude_check_reads_logged_in_flag():
    assert health.check_claude(lambda a: cp('{"loggedIn": true}'))[0]
    ok, why = health.check_claude(lambda a: cp('{"loggedIn": false, "authMethod": "none"}'))
    assert not ok and "не залогинен" in why
    assert not health.check_claude(lambda a: cp("garbage"))[0]


def test_codex_check():
    assert health.check_codex(lambda a: cp("Logged in using ChatGPT\n"))[0]
    assert not health.check_codex(lambda a: cp("Not logged in\n", 1))[0]


def test_watch_notifies_on_change_repeats_and_recovers(tmp_path):
    t = [1000.0]
    status = {"claude": (False, "Claude в WSL не залогинен")}
    shown, logged = [], []
    w = health.Watch(tmp_path / "health.json", lambda e, **f: logged.append(e) or {},
                     notify=lambda title, text: shown.append(text) or True, now=lambda: t[0],
                     checks={"claude": lambda: status["claude"]}, repeat_s=7200)
    w.tick()
    assert len(shown) == 1 and "/login" in shown[0] and logged == ["needs_admin"]
    t[0] += 600
    w.tick()
    assert len(shown) == 1  # not every 5 minutes
    t[0] += 7200
    w.tick()
    assert len(shown) == 2 and "уже 2 ч" in shown[1]
    status["claude"] = (True, "logged in")
    t[0] += 300
    w.tick()
    assert len(shown) == 3 and logged[-1] == "health_recovered"
    w.tick()
    assert len(shown) == 3
    assert json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))["claude"]["ok"] is True


def test_toast_script_escapes_and_encodes():
    s = health.toast_script("Ruti: <x>", "it's & ok")
    assert "&lt;x&gt;" in s and "it''s &amp; ok" in s
    calls = []
    ok = health.notify_windows("t", "x", run=lambda a: calls.append(a) or cp("", 0))
    if calls:  # only where powershell.exe exists (WSL)
        assert calls[0][-2] == "-EncodedCommand"
        assert "ToastNotificationManager" in base64.b64decode(calls[0][-1]).decode("utf-16-le")
        assert ok

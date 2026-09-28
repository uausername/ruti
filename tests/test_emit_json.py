"""`--json` output survives a console that cannot encode what it prints."""

from __future__ import annotations

import io
import json
import sys

from ruti import ui


def console(monkeypatch, encoding: str) -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding=encoding))
    return raw


def test_a_cp1251_console_gets_escapes_instead_of_a_traceback(monkeypatch):
    # Seen on this machine: `ruti delegate --json` died on the arrow in a summary.
    raw = console(monkeypatch, "cp1251")
    ui.emit_json({"note": "read → write", "ru": "готово"})
    sys.stdout.flush()
    text = raw.getvalue().decode("cp1251")
    # One document, whole: the failed first attempt left nothing behind.
    assert json.loads(text) == {"note": "read → write", "ru": "готово"}
    assert "\\u2192" in text


def test_a_utf8_console_gets_the_text_as_it_is(monkeypatch):
    raw = console(monkeypatch, "utf-8")
    ui.emit_json({"note": "read → write"})
    sys.stdout.flush()
    assert "read → write" in raw.getvalue().decode("utf-8")

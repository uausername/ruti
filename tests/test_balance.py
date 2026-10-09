"""The OpenRouter balance: fetched off the status line's path, read from a cache."""

from __future__ import annotations

import io
import json
import subprocess
import time
import urllib.error

from ruti import balance, quota, statusline


def collecting_spawn():
    calls: list[tuple[list[str], dict]] = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))

    return calls, spawn


def fake_response(payload):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    return Response(json.dumps(payload).encode("utf-8"))


def test_fetch_reads_the_credit_totals(monkeypatch):
    monkeypatch.setattr(balance.urllib.request, "urlopen", lambda *_a, **_k: fake_response(
        {"data": {"total_credits": 15, "total_usage": 2.44}}))
    data = balance.fetch("key")
    assert data["total"] == 15.0 and data["used"] == 2.44
    assert round(balance.remaining(data), 2) == 12.56


def test_fetch_is_none_when_openrouter_cannot_be_reached_or_answers_oddly(monkeypatch):
    def refuse(*_a, **_k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(balance.urllib.request, "urlopen", refuse)
    assert balance.fetch("key") is None
    monkeypatch.setattr(balance.urllib.request, "urlopen",
                        lambda *_a, **_k: fake_response({"data": {}}))
    assert balance.fetch("key") is None


def test_the_colour_follows_what_is_left_and_an_old_figure_is_dimmed():
    now = time.time()
    fresh = {"total": 15.0, "used": 2.5, "at": now}
    assert balance.segment(fresh, now) == ("or:$12.50", "32")
    assert balance.segment({**fresh, "used": 12.0}, now) == ("or:$3.00", "33")
    assert balance.segment({**fresh, "used": 14.5}, now) == ("or:$0.50", "31")
    assert balance.segment({**fresh, "used": 20.0}, now) == ("or:$0.00", "31")
    old = {**fresh, "at": now - balance.STALE_AFTER - 1}
    assert balance.segment(old, now) == ("~or:$12.50", "90")


def test_no_cache_means_a_run_is_started_and_a_fresh_one_leaves_it_alone(tmp_path):
    calls, spawn = collecting_spawn()
    assert balance.refresh_in_background(spawn=spawn) is True
    argv, kwargs = calls[0]
    assert argv[-2:] == ["-m", "ruti.balance"]
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["close_fds"] is True
    assert (tmp_path / "openrouter-credits.lock").exists()
    # A run is already holding the lock.
    assert balance.refresh_in_background(spawn=spawn) is False
    assert len(calls) == 1


def test_a_fresh_cache_is_not_fetched_again(tmp_path):
    (tmp_path / "openrouter-credits.json").write_text(
        json.dumps({"total": 15, "used": 2, "at": time.time()}), encoding="utf-8")
    calls, spawn = collecting_spawn()
    assert balance.refresh_in_background(spawn=spawn) is False
    assert calls == []
    assert balance.cached()["total"] == 15


def test_a_cache_that_is_not_ours_reads_as_nothing(tmp_path):
    (tmp_path / "openrouter-credits.json").write_text('{"total": "x"}', encoding="utf-8")
    assert balance.cached() is None


def test_the_status_line_shows_the_balance_after_the_jev_spend(monkeypatch):
    monkeypatch.setattr(statusline, "_refresh_facts", lambda: {
        "at": time.time(), "proxy": True, "loaded": [], "gpu": None})
    monkeypatch.setattr(statusline, "_running_delegate", lambda: None)
    monkeypatch.setattr(statusline, "_route_segment", lambda _sid: (None, None))
    monkeypatch.setattr(balance, "cached",
                        lambda: {"total": 15.0, "used": 2.44, "at": time.time()})
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3600), seven_day=None,
                           captured_at=time.time())
    out = statusline.render({"session_id": "s1"}, snapshot)
    assert "or:$12.56" in out
    monkeypatch.setattr(balance, "cached", lambda: None)
    assert "or:$" not in statusline.render({"session_id": "s1"}, snapshot)

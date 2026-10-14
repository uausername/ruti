"""What the five-hour window really charges per model, checked against API prices."""

from __future__ import annotations

import json
import random

import pytest

from ruti import window_review as wr

DAY = 86400.0


def test_a_repeated_reading_is_not_recorded_and_a_changed_one_is():
    wr.record_sample(10, 1000, at=1.0)
    wr.record_sample(10, 1000, at=2.0)
    wr.record_sample(11, 1000, at=3.0)
    wr.record_sample(11, 2000, at=4.0)
    wr.record_sample(None, 2000, at=5.0)
    assert [(s["at"], s["pct"]) for s in wr.load_samples()] == [(1.0, 10), (3.0, 11), (4.0, 11)]


def test_intervals_span_at_least_three_points_inside_one_window():
    rows = [(1, 10, "a"), (2, 11, "a"), (3, 12, "a"), (4, 13, "a"),
            (5, 14, "b"), (6, 15, "b"), (7, 16, "b"),
            (8, 2, "b"), (9, 5, "b")]
    samples = [{"at": at, "pct": pct, "resets_at": w} for at, pct, w in rows]
    # 10->13 in window a; 14->16 in b is short; the drop to 2 restarts, and 2->5 counts.
    assert wr.intervals(samples) == [(1.0, 4.0, 3.0), (8.0, 9.0, 3.0)]


def _assistant(mid, model, at, **usage):
    return json.dumps({"type": "assistant", "timestamp": at,
                       "message": {"id": mid, "model": model, "usage": usage}})


def test_requests_are_read_from_every_transcript_once_each(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    project = tmp_path / "cfg" / "projects" / "p"
    (project / "s" / "subagents").mkdir(parents=True)
    (project / "s.jsonl").write_text("\n".join([
        _assistant("m1", "claude-opus-5-5", "2026-10-14T10:00:00Z", input_tokens=2,
                   cache_read_input_tokens=1000, cache_creation_input_tokens=300,
                   cache_creation={"ephemeral_1h_input_tokens": 200,
                                   "ephemeral_5m_input_tokens": 100}, output_tokens=50),
        _assistant("m1", "claude-opus-5-5", "2026-10-14T10:00:01Z", output_tokens=50),
        _assistant("m2", "<synthetic>", "2026-10-14T10:00:02Z", output_tokens=1),
        "not json",
    ]), encoding="utf-8")
    (project / "s" / "subagents" / "a.jsonl").write_text(
        _assistant("m3", "claude-sonnet-5-5", "2026-10-14T10:00:03Z",
                   cache_creation_input_tokens=400, output_tokens=7), encoding="utf-8")
    requests = wr.load_requests(0)
    assert [(r["group"], r["read"], r["write_1h"], r["write_5m"]) for r in requests] == [
        ("opus", 1000, 200, 100), ("sonnet", 0, 400, 0)]
    read, other = wr.dollars(requests[0])
    assert read == pytest.approx(1000 * 0.20 / 1e6)
    assert other == pytest.approx((2 * 4 + 200 * 8 + 100 * 5 + 50 * 20) / 1e6)


def test_nnls_recovers_non_negative_weights():
    rng = random.Random(1)
    truth = [2.0, 1.0, 0.5, 3.0, 0.0]
    rows = [[rng.random() for _ in truth] for _ in range(40)]
    ys = [sum(a * b for a, b in zip(row, truth)) for row in rows]
    assert wr.nnls(rows, ys) == pytest.approx(truth, abs=1e-3)


def _world(monkeypatch, weights, rows=60):
    """A window that charges `weights` (% per API dollar of each feature)."""
    rng = random.Random(2)
    requests, spans = [], []
    for n in range(rows):
        start = n * 100.0
        for k in range(30):
            group = "opus" if rng.random() < 0.6 else "sonnet"
            requests.append({"at": start + k, "group": group, "input": 0,
                             "write_1h": rng.randint(1000, 6000), "write_5m": 0,
                             "read": rng.randint(100_000, 400_000),
                             "output": rng.randint(200, 2000)})
        spans.append((start, start + 100.0, 0.0))
    spans = [(s, e, sum(w * f for w, f in zip(weights, wr.features(requests, s, e))))
             for s, e, _ in spans]
    monkeypatch.setattr(wr, "load_samples", lambda: [{"at": 0.0}, {"at": 30 * DAY}])
    monkeypatch.setattr(wr, "intervals", lambda samples: spans)
    monkeypatch.setattr(wr, "load_requests", lambda since, until=None: requests)


def test_a_window_that_prices_like_the_api_keeps_sonnet_at_half(monkeypatch):
    _world(monkeypatch, [1.0, 1.0, 1.0, 1.0, 1.0])
    result = wr.review(current_threshold=250_000)
    assert result["verdict"] in ("keep", "change")
    assert result["sonnet_vs_opus"] == pytest.approx(0.5, abs=0.01)
    assert result["read_vs_other"]["opus"] == pytest.approx(1.0, abs=0.01)
    assert result["recommended"] is not None
    assert "threshold" in wr.format_review(result)


def test_a_window_that_charges_sonnet_like_opus_says_never(monkeypatch):
    _world(monkeypatch, [1.0, 1.0, 2.0, 2.0, 1.0])
    result = wr.review(current_threshold=250_000)
    assert result["verdict"] == "never" and result["recommended"] is None


def test_too_few_intervals_are_insufficient(monkeypatch):
    _world(monkeypatch, [1.0] * 5, rows=10)
    result = wr.review(current_threshold=250_000)
    assert result["verdict"] == "insufficient"
    assert "INSUFFICIENT" in wr.format_review(result)


def _eight_days():
    # A reading an hour, three points apart, in windows of 30 readings: ~185 intervals.
    for n in range(8 * 24):
        wr.record_sample((n % 30) * 3, n // 30, at=n * 3600.0)


def test_the_nudge_comes_once_a_day_until_settled():
    _eight_days()
    now = 8 * DAY
    assert wr.due(now)
    assert "ruti seat review" in wr.nudge(now)
    assert wr.nudge(now + 3600) is None
    assert wr.nudge(now + DAY + 1) is not None
    wr.settle({"ok": True, "verdict": "keep", "recommended": 250_000}, now=now)
    assert not wr.due(now + 2 * DAY) and wr.nudge(now + 2 * DAY) is None


def test_an_insufficient_review_asks_again_a_week_later():
    _eight_days()
    now = 8 * DAY
    wr.settle({"ok": True, "verdict": "insufficient"}, now=now)
    assert not wr.due(now + DAY)
    assert wr.due(now + wr.SNOOZE + 1)


def test_quota_capture_records_the_reading(tmp_path, monkeypatch):
    from ruti import config, quota

    monkeypatch.setattr(quota, "QUOTA_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(config, "QUOTA_FILE", tmp_path / "quota.json", raising=False)
    quota.capture({"rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": 99}}})
    assert [(s["pct"], s["resets_at"]) for s in wr.load_samples()] == [(12.0, 99)]

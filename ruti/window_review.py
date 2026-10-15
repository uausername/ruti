"""Checking the model-switch threshold against what the subscription window really charges.

`manager` decides when a model switch pays from API dollar prices: Sonnet 5.5 is half of
Opus 5.5 on every line, a cache read is 0.05x base input and a one-hour cache write 2x.
This account does not pay in dollars, though, but in a share of the five-hour window,
and Anthropic does not publish how the window weighs a cache read against a write or
against output. If it weighs reads more heavily than the API does, a switch that "pays
back in 19 requests" may never pay back at all.

So the data is collected as the sessions run, and judged once there is enough of it:

* `record_sample` keeps every change of the five-hour percentage (from `quota.capture`,
  which has already dropped stale readings from other sessions).
* `review` cuts the readings into intervals, sums what every Claude Code transcript
  spent in each one -- in API dollars, split into each model's cache reads and the rest
  -- and fits how much of the window one API dollar of each kind costs. If the window
  priced like the API, the four weights would be equal.
* `nudge` tells the session, once a day, when there is enough to judge; the user gets
  the verdict and decides.

The fit is noisy by nature: the percentage is an integer, and claude.ai chats and other
machines spend the same window without leaving a transcript here. Intervals of a few
percent and many of them are what makes it usable; the R^2 says how much to trust it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

# USD per million tokens: input, 1h cache write, 5m cache write, cache read, output.
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "opus": (4.0, 8.0, 5.0, 0.20, 20.0),
    "sonnet": (2.0, 4.0, 2.5, 0.10, 10.0),
    "haiku": (1.0, 2.0, 1.25, 0.10, 5.0),
    "fable": (10.0, 20.0, 12.5, 0.25, 50.0),
}
FEATURES = ("opus_read", "opus_other", "sonnet_read", "sonnet_other", "rest")

# The percentage is an integer: an interval of one point is mostly rounding.
MIN_DELTA = 3.0
# When the data is enough to judge, and when to judge with whatever there is.
DUE_DAYS = 7
DUE_INTERVALS = 100
DUE_ANYWAY_DAYS = 21
MIN_ROWS = 30
MIN_SONNET_SHARE = 0.15
# A review with too little Sonnet in it is asked for again this much later.
SNOOZE = 7 * 86400
NUDGE_EVERY = 86400

# Fallback Opus request profile, from a real 729-request session.
DEFAULT_PROFILE = {"input": 0.0, "read": 250_000.0, "write": 3_600.0, "output": 1_000.0}


def _samples_file() -> Path:
    return config.STATE_ROOT / "window-samples.jsonl"


def _state_file() -> Path:
    return config.STATE_ROOT / "window-review.json"


def _state() -> dict[str, Any]:
    data = config.read_json(_state_file(), default={})
    return data if isinstance(data, dict) else {}


def _transcripts_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


# ------------------------------------------------------------------ readings


def _last_line(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            lines = handle.read().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


def record_sample(pct: Any, resets_at: Any, at: float | None = None) -> None:
    """Append a reading of the five-hour percentage, when it differs from the last one."""
    if pct is None:
        return
    try:
        path = _samples_file()
        last = _last_line(path)
        if last and last.get("pct") == float(pct) and last.get("resets_at") == resets_at:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.time() if at is None else at, "pct": float(pct),
               "resets_at": resets_at}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def load_samples() -> list[dict[str, Any]]:
    out = []
    try:
        with _samples_file().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    float(row["at"]), float(row["pct"])
                except (ValueError, KeyError, TypeError):
                    continue
                out.append(row)
    except OSError:
        return []
    return sorted(out, key=lambda row: row["at"])


def intervals(samples: list[dict[str, Any]],
              min_delta: float = MIN_DELTA) -> list[tuple[float, float, float]]:
    """(start, end, percentage points spent), each at least `min_delta` points.

    A new window, or a reading that went down (a reset seen late), starts over: usage
    never falls inside one window.
    """
    out: list[tuple[float, float, float]] = []
    anchor: dict[str, Any] | None = None
    for row in samples:
        if (anchor is None or row.get("resets_at") != anchor.get("resets_at")
                or row["pct"] < anchor["pct"]):
            anchor = row
            continue
        delta = row["pct"] - anchor["pct"]
        if delta >= min_delta:
            out.append((float(anchor["at"]), float(row["at"]), float(delta)))
            anchor = row
    return out


# ------------------------------------------------------------------ spending


def _group(model: str) -> str | None:
    for name in PRICES:
        if name in model:
            return name
    return None


def _epoch(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_requests(since: float, until: float | None = None,
                  root: Path | None = None) -> list[dict[str, Any]]:
    """Every API request Claude Code logged between `since` and `until`, subagents too."""
    end = float("inf") if until is None else until
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in (root or _transcripts_root()).rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message") if isinstance(record, dict) else None
                if record.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                usage, group = message.get("usage"), _group(str(message.get("model") or ""))
                at = _epoch(record.get("timestamp"))
                if not isinstance(usage, dict) or group is None or at is None:
                    continue
                if not since <= at < end:
                    continue
                # The same message is written once per content block while it streams.
                key = str(message.get("id") or "")
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                detail = usage.get("cache_creation")
                if isinstance(detail, dict):
                    write_1h = detail.get("ephemeral_1h_input_tokens") or 0
                    write_5m = detail.get("ephemeral_5m_input_tokens") or 0
                else:
                    write_1h, write_5m = usage.get("cache_creation_input_tokens") or 0, 0
                out.append({"at": at, "group": group,
                            "input": usage.get("input_tokens") or 0,
                            "write_1h": write_1h, "write_5m": write_5m,
                            "read": usage.get("cache_read_input_tokens") or 0,
                            "output": usage.get("output_tokens") or 0})
    return sorted(out, key=lambda req: req["at"])


def dollars(req: dict[str, Any]) -> tuple[float, float]:
    """(cache-read dollars, all other dollars) of one request at API prices."""
    p_in, p_w1h, p_w5m, p_read, p_out = PRICES[req["group"]]
    read = req["read"] * p_read
    other = (req["input"] * p_in + req["write_1h"] * p_w1h + req["write_5m"] * p_w5m
             + req["output"] * p_out)
    return read / 1e6, other / 1e6


def features(requests: list[dict[str, Any]], start: float, end: float) -> list[float]:
    row = [0.0] * len(FEATURES)
    for req in requests:
        if start <= req["at"] < end:
            read, other = dollars(req)
            if req["group"] in ("opus", "sonnet"):
                row[FEATURES.index(f"{req['group']}_read")] += read
                row[FEATURES.index(f"{req['group']}_other")] += other
            else:
                row[FEATURES.index("rest")] += read + other
    return row


def nnls(rows: list[list[float]], ys: list[float], iterations: int = 5000) -> list[float]:
    """Non-negative least squares by cyclic coordinate descent -- five columns, no numpy."""
    width = len(rows[0]) if rows else 0
    weights = [0.0] * width
    norms = [sum(row[k] ** 2 for row in rows) for k in range(width)]
    residual = list(ys)
    for _ in range(iterations):
        moved = 0.0
        for k in range(width):
            if norms[k] == 0:
                continue
            step = sum(row[k] * r for row, r in zip(rows, residual)) / norms[k]
            new = max(0.0, weights[k] + step)
            change = new - weights[k]
            if change:
                weights[k] = new
                residual = [r - change * row[k] for row, r in zip(rows, residual)]
                moved = max(moved, abs(change))
        if moved < 1e-12:
            break
    return weights


# ------------------------------------------------------------------ the verdict


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def review(now: float | None = None, flow_cap: int = 500_000,
           current_threshold: int | None = None) -> dict[str, Any]:
    """Fit what the window charges, and say whether the switch threshold holds."""
    samples = load_samples()
    if len(samples) < 2:
        return {"ok": False, "reason": "no window readings yet"}
    if current_threshold is None:
        from . import manager
        current_threshold = manager.SWITCH_FREE_TOKENS_FLOW
    spans = intervals(samples)
    requests = load_requests(float(samples[0]["at"]), now)
    rows, ys = [], []
    for start, end, delta in spans:
        row = features(requests, start, end)
        if sum(row):
            rows.append(row)
            ys.append(delta)
    weights = nnls(rows, ys) if rows else [0.0] * len(FEATURES)
    w = dict(zip(FEATURES, weights))

    mean = sum(ys) / len(ys) if ys else 0.0
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - sum(a * b for a, b in zip(row, weights))) ** 2
                 for row, y in zip(rows, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    total = sum(sum(row) for row in rows)
    sonnet = sum(row[2] + row[3] for row in rows)

    opus = [req for req in requests if req["group"] == "opus"]
    if opus:
        profile = {"input": sum(r["input"] for r in opus) / len(opus),
                   "read": sum(r["read"] for r in opus) / len(opus),
                   "write": sum(r["write_1h"] + r["write_5m"] for r in opus) / len(opus),
                   "output": sum(r["output"] for r in opus) / len(opus)}
    else:
        profile = dict(DEFAULT_PROFILE)
    i, r, wr, o = profile["input"], profile["read"], profile["write"], profile["output"]
    c_opus = (w["opus_read"] * r * 0.20 + w["opus_other"] * (i * 4 + wr * 8 + o * 20)) / 1e6
    c_sonnet = (w["sonnet_read"] * r * 0.10
                + w["sonnet_other"] * (i * 2 + wr * 4 + o * 10)) / 1e6
    saving = c_opus - c_sonnet

    def payback(tokens: int) -> float | None:
        # Sonnet writes the context where Opus would have read it.
        cost = (w["sonnet_other"] * tokens * 4 - w["opus_read"] * tokens * 0.20) / 1e6
        return cost / saving if saving > 0 else None

    def runway(tokens: int) -> float:
        return (flow_cap - tokens) / max(wr, 1.0)

    recommended = None
    for tokens in range(50_000, 450_001, 50_000):
        back = payback(tokens)
        if back is not None and back <= runway(tokens) / 2:
            recommended = tokens

    if len(rows) < MIN_ROWS or (w["sonnet_read"] == 0 and w["sonnet_other"] == 0):
        verdict = "insufficient"
    elif recommended is None:
        verdict = "never"
    elif abs(recommended - current_threshold) <= 50_000:
        verdict = "keep"
    else:
        verdict = "change"
    return {
        "ok": True, "verdict": verdict, "recommended": recommended,
        "current": current_threshold, "rows": len(rows), "r2": r2, "weights": w,
        "read_vs_other": {"opus": _ratio(w["opus_read"], w["opus_other"]),
                          "sonnet": _ratio(w["sonnet_read"], w["sonnet_other"])},
        "sonnet_vs_opus": _ratio(c_sonnet, c_opus),
        "payback_at_current": payback(current_threshold),
        "profile": profile,
        "days": (float(samples[-1]["at"]) - float(samples[0]["at"])) / 86400,
        "sonnet_share": sonnet / total if total else 0.0,
    }


def _fmt(value: float | None, pattern: str = "{:.2f}") -> str:
    return "n/a" if value is None else pattern.format(value)


def format_review(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"window review: {result.get('reason')}"
    verdict = result["verdict"]
    current = result["current"]
    head = {
        "keep": f"KEEP the switch threshold at {current // 1000}k: the window prices a "
                "switch close enough to the API.",
        "change": f"CHANGE the switch threshold from {current // 1000}k to "
                  f"{(result['recommended'] or 0) // 1000}k.",
        "never": "NEVER switch model down mid-task: in window terms it does not pay back "
                 "before the flow handoff.",
        "insufficient": "INSUFFICIENT data to judge yet: "
                        f"{result['rows']} intervals (need {MIN_ROWS}), Sonnet share "
                        f"{result['sonnet_share']:.0%} (need {MIN_SONNET_SHARE:.0%}).",
    }[verdict]
    ratios = result["read_vs_other"]
    return "\n".join([
        head,
        f"fit: {result['rows']} intervals over {result['days']:.1f} days, R^2 "
        f"{result['r2']:.2f}, Sonnet {result['sonnet_share']:.0%} of the API dollars",
        f"cache read vs other tokens, per API dollar: opus {_fmt(ratios['opus'])}, "
        f"sonnet {_fmt(ratios['sonnet'])} (1.00 if the window prices reads like the API)",
        f"a Sonnet request costs {_fmt(result['sonnet_vs_opus'])} of an Opus one "
        "(API: 0.50)",
        f"a switch at {current // 1000}k pays back in "
        f"{_fmt(result['payback_at_current'], '{:.0f}')} requests",
        "recommended threshold: "
        + (f"{result['recommended'] // 1000}k" if result["recommended"] else "none"),
    ])


# ------------------------------------------------------------------ when to ask


def due(now: float | None = None) -> bool:
    """Whether there is enough to judge. Reads only the readings: it runs in the prompt
    hook, and scanning the transcripts is `review`'s job."""
    try:
        moment = time.time() if now is None else now
        state = _state()
        if state.get("done_at") or moment < float(state.get("snooze_until") or 0):
            return False
        samples = load_samples()
        if len(samples) < 2:
            return False
        days = (moment - float(samples[0]["at"])) / 86400
        if days >= DUE_ANYWAY_DAYS:
            return True
        return days >= DUE_DAYS and len(intervals(samples)) >= DUE_INTERVALS
    except Exception:
        return False


def nudge(now: float | None = None) -> str | None:
    try:
        moment = time.time() if now is None else now
        if not due(moment):
            return None
        state = _state()
        if moment - float(state.get("nudged_at") or 0) < NUDGE_EVERY:
            return None
        config.write_json(_state_file(), {**state, "nudged_at": moment})
        return ("ruti: enough 5-hour-window readings have been collected to check whether "
                "the model-switch threshold (priced from API dollars) matches what the "
                "subscription window really charges. Run `ruti seat review`, show the "
                "user its verdict and numbers in their language, and ask whether to apply "
                "the recommendation.")
    except Exception:
        return None


def settle(result: dict[str, Any], now: float | None = None) -> None:
    """Record a review: done for a verdict, asked again in a week for too little data."""
    moment = time.time() if now is None else now
    state = _state()
    if result.get("ok") and result.get("verdict") != "insufficient":
        state.update({"done_at": moment, "verdict": result["verdict"],
                      "recommended": result.get("recommended")})
    else:
        state["snooze_until"] = moment + SNOOZE
    config.write_json(_state_file(), state)

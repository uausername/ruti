"""A record of what ruti actually did, so its usefulness is measurable rather than assumed.

The claim this project makes is specific: delegating keeps a delegate's output out of
the manager's context, and context length is what drives the subscription burn. That
claim is checkable, and it should be checked -- a tool that costs turns to run and
cannot show it saves more than it costs is worse than not having it.

What is measured exactly:

* **Lines of code the manager never had to emit.** A delegate writes files directly;
  had the manager done the same work, every one of those lines would have passed
  through its context as tool input. This is the bulk of the saving.
* **Bytes of delegate transcript kept in a log rather than read.** Included for
  completeness, but measurement showed this to be small -- `opencode`'s default output
  is terse, so containing it is worth far less than the design originally assumed.
* **Delegation outcomes** -- succeeded, failed, silently answered by a fallback.
* **Utilisation at session start and end**, from the status line's readings.

What is *not* measured, and must not be implied: the counterfactual. Nobody can say
what the window would have read had the same work been done in session, because the
comparison was never run. Quota deltas here are observations, not attribution -- other
projects share the same window, and a session that spans a reset shows a decrease.
The report says so rather than quietly presenting a number that looks like proof.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from .config import STATE_ROOT, ensure_dirs, read_json

LEDGER = STATE_ROOT / "ledger.jsonl"
MAX_BYTES = 4 * 1024 * 1024

# Rough and stated as such: used only to render counts in a more familiar unit.
BYTES_PER_TOKEN = 4
TOKENS_PER_LINE = 12


def current_session() -> str | None:
    """The session id the status line last saw, used to group events."""
    data = read_json(STATE_ROOT / "quota.json", default={}) or {}
    return data.get("session_id")


def record(event: str, **fields: Any) -> None:
    """Append one event. Never raises: bookkeeping must not break the thing it measures."""
    try:
        ensure_dirs()
        if LEDGER.exists() and LEDGER.stat().st_size > MAX_BYTES:
            LEDGER.replace(LEDGER.with_suffix(".jsonl.1"))
        entry = {"at": time.time(), "event": event, "session": current_session(), **fields}
        # Append mode with a single small write: atomic enough for line-oriented data,
        # and cheaper than taking a lock on something written this often.
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


def read_events(since_days: float | None = None) -> list[dict[str, Any]]:
    cutoff = time.time() - since_days * 86400 if since_days else 0.0
    events: list[dict[str, Any]] = []
    for path in (LEDGER.with_suffix(".jsonl.1"), LEDGER):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a truncated final line from a killed process
            if entry.get("at", 0) >= cutoff:
                events.append(entry)
    return events


def _sessions(events: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Pair up session start/end markers with the delegations in between."""
    by_session: dict[str | None, dict[str, Any]] = {}
    for entry in events:
        record_ = by_session.setdefault(
            entry.get("session"),
            {"session": entry.get("session"), "start": None, "end": None, "delegations": []},
        )
        if entry["event"] == "session_start":
            record_["start"] = entry
        elif entry["event"] == "session_end":
            record_["end"] = entry
        elif entry["event"] == "delegation":
            record_["delegations"].append(entry)
    yield from by_session.values()


def summarise(events: list[dict[str, Any]]) -> dict[str, Any]:
    delegations = [e for e in events if e["event"] == "delegation"]
    routes = [e for e in events if e["event"] == "route"]

    log_bytes = sum(e.get("log_bytes", 0) for e in delegations)
    summary_bytes = sum(e.get("summary_bytes", 0) for e in delegations)
    avoided = max(0, log_bytes - summary_bytes)
    lines_written = sum(e.get("lines_written", 0) for e in delegations)

    by_tier: dict[str, dict[str, Any]] = {}
    for entry in delegations:
        tier = by_tier.setdefault(
            entry.get("tier", "unknown"),
            {"runs": 0, "failed": 0, "seconds": 0.0, "lines": 0},
        )
        tier["runs"] += 1
        tier["failed"] += 0 if entry.get("ok") else 1
        tier["seconds"] += entry.get("duration_s", 0.0)
        tier["lines"] += entry.get("lines_written", 0)

    sessions = list(_sessions(events))
    measured = [
        s for s in sessions
        if s["start"] and s["end"]
        and s["start"].get("five_hour") is not None
        and s["end"].get("five_hour") is not None
    ]
    # A session that spans a window reset shows utilisation going down, which is a new
    # window rather than a saving. Those cannot be read as consumption and are excluded.
    usable = [s for s in measured if s["end"]["five_hour"] >= s["start"]["five_hour"]]

    with_delegation = [s for s in usable if s["delegations"]]
    without = [s for s in usable if not s["delegations"]]

    def spend(group: list[dict[str, Any]]) -> float | None:
        if not group:
            return None
        return sum(s["end"]["five_hour"] - s["start"]["five_hour"] for s in group) / len(group)

    return {
        "delegations": {
            "total": len(delegations),
            "failed": sum(1 for e in delegations if not e.get("ok")),
            "substituted": sum(1 for e in delegations if e.get("substituted")),
            "by_tier": by_tier,
        },
        "work_offloaded": {
            "lines_written": lines_written,
            "approx_tokens": lines_written * TOKENS_PER_LINE,
        },
        "transcript_contained": {
            "bytes": avoided,
            "approx_tokens": avoided // BYTES_PER_TOKEN,
            "delegate_output_bytes": log_bytes,
            "summary_bytes": summary_bytes,
        },
        "routes": {
            "total": len(routes),
            "followed": sum(1 for r in routes if r.get("followed")),
        },
        "sessions": {
            "seen": len(sessions),
            "with_usable_quota_readings": len(usable),
            "spanning_a_reset_excluded": len(measured) - len(usable),
            "mean_five_hour_spend_with_delegation": spend(with_delegation),
            "mean_five_hour_spend_without": spend(without),
            "counted_with": len(with_delegation),
            "counted_without": len(without),
        },
    }

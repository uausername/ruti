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
* **Money, where the provider states it** -- which model really answered each run and
  what the provider billed for it. Runs without a stated price are counted as runs,
  never silently as zero.
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

from . import usage
from .config import STATE_ROOT, ensure_dirs, read_json

LEDGER = STATE_ROOT / "ledger.jsonl"
MAX_BYTES = 4 * 1024 * 1024

# Rough and stated as such: used only to render counts in a more familiar unit.
BYTES_PER_TOKEN = 4
TOKENS_PER_LINE = 12


def current_session() -> str | None:
    """The session that ran the command, used to group events.

    `CLAUDE_CODE_SESSION_ID` is exported into every tool subprocess, so it names the
    session that actually invoked `route` or `delegate`. quota.json's id is only a
    fallback for callers outside a session: the status line writes it, and the status
    line is shared, so whichever session repainted last would otherwise be credited
    with another session's work -- silently mixing two sessions' compliance together.
    """
    from . import sessions

    data = read_json(STATE_ROOT / "quota.json", default={}) or {}
    return sessions.current_session_id() or data.get("session_id")


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


def last_delegation() -> dict[str, Any] | None:
    """The most recent delegation event, read from the tail so the status line can
    afford to call this on every repaint without scanning the whole ledger."""
    if not LEDGER.exists():
        return None
    try:
        with LEDGER.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "delegation":
            return entry
    return None


def _alias_of(executor: str) -> str:
    """`ruti-router/free` -> `free`. A `claude:*` executor has no alias."""
    return executor.split("/")[-1] if "/" in executor else executor


def _outcome(entries: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """What came of the ranking at `entries[index]`, one session's events in order.

    The ledger is append-only, so a route event cannot be marked after the fact; its
    outcome is read back by pairing instead:

    * `followed` -- a later delegation in the session used the recommended alias.
      `delegation` is the latest attempt before the next ranking (or the first one
      after it, if none came before), `attempts` how many there were: a retry that
      succeeded after `ruti doctor --fix` must not stay shown as the failure;
    * `in_session` -- the ranking recommended the session itself, so writing the code
      here *is* following it;
    * `instead` -- no delegation to the recommended alias, but delegations to others
      before the next ranking (`instead` lists them);
    * `not_delegated` -- nothing was delegated before the next ranking.

    `route_compliance`, the prompt hook's reminder, `report --routes` and the status
    line all read this one function, so their counts cannot drift apart.
    """
    recommended = entries[index].get("recommended") or ""
    later = entries[index + 1:]

    if recommended and not recommended.startswith("claude:"):
        alias = _alias_of(recommended)
        matches: list[dict[str, Any]] = []
        before_next = True
        for entry in later:
            if entry.get("event") == "route":
                before_next = False
            elif (entry.get("event") == "delegation"
                    and _alias_of(entry.get("model") or "") == alias):
                matches.append({"before": before_next, "entry": entry})
        if matches:
            ours = [m["entry"] for m in matches if m["before"]]
            return {"outcome": "followed", "delegation": ours[-1] if ours
                    else matches[0]["entry"], "attempts": len(ours) or 1, "instead": []}

    instead: list[dict[str, Any]] = []
    for entry in later:
        if entry.get("event") == "route":
            break
        if entry.get("event") == "delegation":
            instead.append(entry)
    if not recommended or recommended.startswith("claude:"):
        return {"outcome": "in_session", "delegation": None, "instead": instead}
    return {"outcome": "instead" if instead else "not_delegated",
            "delegation": None, "instead": instead}


def _by_session(events: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for entry in events:
        grouped.setdefault(entry.get("session"), []).append(entry)
    return grouped


def route_compliance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether rankings were acted on, derived by pairing rather than stored.

    A ranking that recommended the session itself is not a violation -- writing the
    code here *is* following that advice -- so it is counted separately rather than
    as ignored.
    """
    outcomes = [row["outcome"] for row in route_history(events)]
    return {
        "total": len(outcomes),
        "followed": outcomes.count("followed"),
        "ignored": outcomes.count("instead") + outcomes.count("not_delegated"),
        "recommended_self": outcomes.count("in_session"),
    }


def _session_tail(session_id: str, max_bytes: int = 131072) -> list[dict[str, Any]]:
    """This session's events among the ledger's last `max_bytes`, oldest first.

    Read from the tail because the prompt hook and the status line call this on every
    prompt and every repaint, and must not scan a ledger that grows all day. Lines
    that do not mention the session are skipped before they are parsed.
    """
    if not LEDGER.exists():
        return []
    try:
        with LEDGER.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for line in data.splitlines():
        if session_id not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("session") == session_id:
            entries.append(entry)
    return entries


def session_jev_cost(session_id: str | None) -> float:
    """Known Jev spend this session: classification calls plus the council's own gate
    and judge. Deliberately partial -- it does not include what a convened council's
    member models actually cost, because pulling that from the proxy's usage log needs
    a multi-second settle wait per model (see `usage.collect`) that would tax every
    `ruti council` call whether or not anyone reads the total. Labelled `jev:` rather
    than `spent:` wherever this is shown, so the number never claims more than it is.
    """
    if not session_id:
        return 0.0
    total = 0.0
    for entry in _session_tail(session_id):
        if entry.get("event") == "route":
            total += float((entry.get("classifier") or {}).get("cost_usd", 0.0) or 0.0)
        elif entry.get("event") == "jev_spend":
            total += float(entry.get("cost_usd", 0.0) or 0.0)
    return total


def unfollowed_route(session_id: str | None) -> dict[str, Any] | None:
    """The latest ranking in this session that named a delegate and was not acted on."""
    route = last_route(session_id)
    if not route or route["outcome"] in ("followed", "in_session"):
        return None
    return route


def last_route(session_id: str | None) -> dict[str, Any] | None:
    """The latest ranking in this session, with what came of it (see `_outcome`)."""
    if not session_id:
        return None
    entries = _session_tail(session_id)
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].get("event") == "route":
            return {**entries[index], **_outcome(entries, index)}
    return None


def route_history(events: list[dict[str, Any]], *, limit: int | None = None
                  ) -> list[dict[str, Any]]:
    """Every ranking with what came of it, oldest first; the last `limit` if given."""
    rows: list[dict[str, Any]] = []
    for entries in _by_session(events).values():
        for index, entry in enumerate(entries):
            if entry.get("event") == "route":
                rows.append({**entry, **_outcome(entries, index)})
    rows.sort(key=lambda row: row.get("at", 0))
    return rows[-limit:] if limit else rows


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

    log_bytes = sum(e.get("log_bytes", 0) for e in delegations)
    summary_bytes = sum(e.get("summary_bytes", 0) for e in delegations)
    avoided = max(0, log_bytes - summary_bytes)

    # Only successful runs count as work offloaded. A delegate that exits cleanly and
    # writes something unusable has cost the manager the work twice over, and counting
    # its lines as a saving inverts the sign of the measurement. Found on a real
    # project: two runs exited 0, produced 75 lines between them, and every one of
    # those lines had to be thrown away and written by hand.
    succeeded = [e for e in delegations if e.get("ok")]
    lines_written = sum(e.get("lines_written", 0) for e in succeeded)
    discarded_lines = sum(e.get("lines_written", 0) for e in delegations if not e.get("ok"))

    delegated_files: list[str] = []
    for entry in succeeded:
        for name in entry.get("files") or []:
            if name not in delegated_files:
                delegated_files.append(name)

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
            "discarded_lines": discarded_lines,
            "files": delegated_files,
        },
        "transcript_contained": {
            "bytes": avoided,
            "approx_tokens": avoided // BYTES_PER_TOKEN,
            "delegate_output_bytes": log_bytes,
            "summary_bytes": summary_bytes,
        },
        "money": usage.summarise_costs(delegations),
        "routes": route_compliance(events),
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

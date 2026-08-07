"""The Claude Code status line, and the only local source of quota data.

Registered as `statusLine` in settings.json, this is invoked on every UI repaint with
a JSON payload on stdin. Two jobs: render one line, and persist `rate_limits` to
`quota.json` so the router has something to reason about. The second job is the
important one -- Claude Code writes those counters nowhere else.

Three hard constraints follow from running on every repaint:

* never block -- a hung status line freezes the interface;
* never raise -- a traceback would be rendered as the status line;
* stay well under a repaint's worth of time, which rules out spawning `lms` (a Node
  CLI, hundreds of milliseconds to start) and means expensive facts are cached.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from . import quota
from .config import LMSTUDIO_BASE, PROXY_BASE, STATE_ROOT, read_json, write_json

FACTS_FILE = STATE_ROOT / "facts.json"
FACTS_TTL_SECONDS = 30.0

# Generous enough to succeed against a healthy local service, tight enough that a
# hung one costs a frame rather than the session.
HTTP_TIMEOUT = 0.4

BAND_COLOUR = {
    quota.GREEN: "32", quota.YELLOW: "33", quota.ORANGE: "33",
    quota.RED: "31", quota.CRITICAL: "31", quota.UNKNOWN: "35",
}


def _colour(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def _get(url: str) -> Any:
    """A minimal local GET.

    Deliberately urllib rather than httpx: importing httpx costs ~480 ms and its first
    request another ~500 ms setting up connection pools and an SSL context. For an
    unencrypted call to localhost that is all waste, and it is waste paid on every
    repaint of the interface.
    """
    import urllib.request

    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
        if response.status != 200:
            return None
        return json.loads(response.read().decode("utf-8"))


def _refresh_facts() -> dict[str, Any]:
    """Local service state, cached so most repaints cost nothing."""
    cached = read_json(FACTS_FILE, default=None)
    if isinstance(cached, dict) and time.time() - cached.get("at", 0) < FACTS_TTL_SECONDS:
        return cached

    facts: dict[str, Any] = {"at": time.time(), "proxy": False, "loaded": [], "gpu": None}

    try:
        # /health/liveliness only. Bare /health dials every backend and hangs when one
        # is down, which is exactly when the status line most needs to render.
        facts["proxy"] = _get(f"{PROXY_BASE}/health/liveliness") is not None
    except Exception:
        pass

    try:
        # The REST API rather than the `lms` CLI: same information, but `lms` is a Node
        # program whose startup alone would blow the frame budget.
        data = _get(f"{LMSTUDIO_BASE}/api/v0/models") or {}
        facts["loaded"] = [
            {"id": m.get("id"), "ctx": m.get("loaded_context_length")}
            for m in data.get("data", [])
            if m.get("state") == "loaded"
        ]
    except Exception:
        pass

    try:
        from . import proc

        result = proc.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=2.0,
        )
        if result.ok and result.stdout.strip():
            free, total = (int(x.strip()) for x in result.stdout.strip().splitlines()[0].split(","))
            facts["gpu"] = {"free_mib": free, "total_mib": total}
    except Exception:
        pass

    try:
        from . import ledger

        facts["last_delegation"] = ledger.last_delegation()
    except Exception:
        pass

    try:
        write_json(FACTS_FILE, facts)
    except Exception:
        pass
    return facts


def _running_delegate() -> dict[str, Any] | None:
    """The delegate executing right now, or None if the marker is absent or stale."""
    from .delegate import RUNNING_FILE

    entry = read_json(RUNNING_FILE, default=None)
    if not isinstance(entry, dict):
        return None
    if time.time() > entry.get("expires_at", 0):
        return None
    return entry


def render(payload: dict[str, Any], snapshot: quota.Quota) -> str:
    segments: list[str] = []

    if snapshot.five_hour is not None:
        band = snapshot.band
        used = snapshot.five_hour.used_percentage
        text = f"{used:.0f}% 5h"
        remaining = snapshot.five_hour.resets_in_seconds
        if remaining and remaining > 0:
            text += f"/{remaining / 3600:.1f}h"
        # Colour carries the band for anyone who can see it; the word carries it for
        # everyone else, and for a terminal that strips escapes.
        segments.append(_colour(f"{band} {text}", BAND_COLOUR.get(band, "37")))
    else:
        segments.append(_colour("quota n/a", "35"))

    if snapshot.seven_day is not None:
        segments.append(f"{snapshot.seven_day.used_percentage:.0f}% 7d")

    model = (payload.get("model") or {}).get("display_name")
    effort = (payload.get("effort") or {}).get("level")
    if model:
        segments.append(f"{model}{'/' + effort if effort else ''}")

    facts = _refresh_facts()
    loaded = facts.get("loaded") or []
    if loaded:
        first = loaded[0]
        segments.append(_colour(f"local:{first['id']}@{first.get('ctx') or '?'}", "36"))
    else:
        segments.append(_colour("local:none", "90"))

    # Read live rather than from the cached facts: a 30-second-old answer to "is a
    # delegate running right now" is the one thing this segment cannot afford.
    running = _running_delegate()
    if running:
        elapsed = max(0, int(time.time() - running.get("started_at", time.time())))
        model = str(running.get("model", "?")).split("/")[-1]
        segments.append(_colour(f"running:{model} {elapsed}s", "33"))

    last = facts.get("last_delegation")
    if last and not running:
        # The model that actually answered, not just the one asked for -- a mismatch
        # here is the fallback substitution delegate.py checks for on every run.
        requested = str(last.get("model", "?")).split("/")[-1]
        if last.get("substituted"):
            segments.append(_colour(f"last:{requested} SUBST", "31"))
        elif not last.get("ok"):
            segments.append(_colour(f"last:{requested} FAILED", "31"))
        else:
            segments.append(_colour(f"last:{requested}", "36"))

    gpu = facts.get("gpu")
    if gpu:
        segments.append(f"gpu {gpu['free_mib'] / 1024:.1f}G free")

    segments.append(_colour("proxy ok", "32") if facts.get("proxy")
                    else _colour("proxy DOWN", "31"))

    return " · ".join(segments)


def main() -> int:
    # Claude Code decodes this script's output as UTF-8, but Python on Windows encodes
    # stdout in the console code page when it is a pipe. The separator below is U+00B7,
    # which cp1251 happily encodes as a single byte -- and which then arrives as a
    # replacement character. Say what the encoding is rather than inheriting it.
    try:
        # newline="\n" as well: text mode would otherwise append a carriage return that
        # the interface has no reason to render.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:
        pass

    # Nothing below may raise: whatever happens, print something printable and exit 0.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        snapshot = quota.capture(payload)
    except Exception:
        try:
            snapshot = quota.load()
        except Exception:
            snapshot = quota.Quota(None, None, 0.0)

    try:
        line = render(payload, snapshot)
    except Exception:
        line = "ruti: status unavailable"

    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The Claude Code status line, and the only local source of quota data.

Registered as `statusLine` in settings.json, this is invoked on every UI repaint with
a JSON payload on stdin. Two jobs: render one line, and persist `rate_limits` to
`quota.json` so the router has something to reason about. The second job is the
important one -- Claude Code writes those counters nowhere else. It also records the
session's context-window fill (`context_watch`) for the prompt hook to read back.

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

from . import context_watch, quota
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


def _short(executor: str) -> str:
    """`ruti-router/north-mini-code` -> `north-mini-code`; `claude:self` -> `self`."""
    return executor.split("/")[-1].split(":")[-1]


def _route_segment(session_id: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """`-> north-mini-code ✓` for the session's latest ranking, and the delegation it
    shows (so the `last:` segment need not repeat it). Never raises."""
    try:
        if not session_id:
            return None, None
        from . import ledger

        route = ledger.last_route(session_id)
        if not route:
            return None, None
        target = _short(route.get("recommended") or "") or "none"
        outcome = route["outcome"]
        if outcome == "followed":
            run = route["delegation"]
            if run.get("substituted"):
                return _colour(f"→ {target} SUBST", "31"), run
            if not run.get("ok"):
                return _colour(f"→ {target} ✗", "31"), run
            return _colour(f"→ {target} ✓", "32"), run
        if outcome == "in_session":
            return _colour(f"→ {target}", "90"), None
        if outcome == "instead":
            used = _short(str(route["instead"][-1].get("model") or "?"))
            return _colour(f"→ {target} ≠ {used}", "33"), None
        return _colour(f"→ {target} …", "33"), None
    except Exception:
        return None, None


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
        if snapshot.freshness != "live":
            # The band above was computed from this same stale reading, so the number
            # can be quietly wrong at the exact moment it is glanced at for reassurance.
            segments.append(_colour(f"~{snapshot.freshness}", "90"))
    else:
        segments.append(_colour("quota n/a", "35"))

    if snapshot.seven_day is not None:
        pct = snapshot.seven_day.used_percentage
        text = f"{pct:.0f}% 7d"
        if snapshot.seven_day_binding:
            # It is the reason `band` is this tight, not merely a number alongside a
            # worse one -- share the band's own colour so that reads at a glance.
            segments.append(_colour(text, BAND_COLOUR.get(snapshot.band, "37")))
        elif pct >= 60.0:
            segments.append(_colour(text, "33"))
        else:
            segments.append(text)

    model = (payload.get("model") or {}).get("display_name")
    effort = (payload.get("effort") or {}).get("level")
    if model:
        segments.append(f"{model}{'/' + effort if effort else ''}")

    # Session task modes, shown only while active. Never let this raise -- a status
    # line that throws is rendered as a traceback.
    try:
        session_id = payload.get("session_id")
        if session_id:
            from . import sessions

            if sessions.is_disabled(session_id):
                # The one toggle that matters most and used to have no presence here
                # at all -- `route`/`delegate` refuse outright, and previously the only
                # sign was a hook message that scrolls out of view within a prompt or
                # two. Shown first, loudest, so it cannot be scrolled past unnoticed.
                segments.append(_colour("OFF", "31"))

            from . import modes

            active = modes.current(session_id)
            if active["coding"]:
                segments.append(_colour("code", "36"))
            if active["free"] == "soft":
                segments.append(_colour("free", "32"))
            elif active["free"] == "hard":
                segments.append(_colour("free!", "33"))
            # Magenta, and the only mode shown that costs more rather than less --
            # worth reading at a glance before asking anything expensive.
            if active.get("council") == "on":
                segments.append(_colour("council", "35"))
            elif active.get("council") == "auto":
                segments.append(_colour("council?", "35"))
            if active.get("wait"):
                from . import quota as quota_mod, wait as wait_mod

                snap = quota_mod.load()
                paused = modes.wait_state(session_id).get("paused")
                if paused and paused == wait_mod.window_key(snap):
                    segments.append(_colour(f"paused->{wait_mod.reset_clock(snap)}", "33"))
                else:
                    segments.append(_colour("wait", "34"))
            if active.get("flow"):
                # The arrow once this session has passed the task on: the window stays
                # open, and should read as done rather than as still at work.
                launched = modes.flow_state(session_id).get("launched")
                segments.append(_colour("flow→" if launched else "flow", "36"))

            # Shown always, not only while active, unlike the modes above: the whole
            # point is to be able to tell "off" from "silent because irrelevant" at a
            # glance, for the one toggle that quietly changes what `route --describe`
            # and the prompt hook's hint actually do.
            from . import jev as jev_mod

            jev_on = active.get("jev", True) and jev_mod.configured()
            segments.append(_colour("jev", "32") if jev_on else _colour("jev", "90"))
    except Exception:
        pass

    # Same number `/context` reports -- how full the current turn's context window is,
    # not to be confused with the five-hour subscription budget above.
    ctx_used = (payload.get("context_window") or {}).get("used_percentage")
    if ctx_used is not None:
        colour = "31" if ctx_used >= 85 else "33" if ctx_used >= context_watch.WARN_PERCENT else "32"
        segments.append(_colour(f"{ctx_used:.0f}% ctx", colour))

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

    # The session's latest ranking and what came of it. Read live, like `running`: the
    # point is to see the decision the moment it is made.
    route_text, route_run = _route_segment(payload.get("session_id"))
    if route_text:
        segments.append(route_text)

    # Scoped to this session, not `_refresh_facts`'s machine-wide cache: that file has
    # no session key (proxy/GPU genuinely are machine-global), and a delegation from a
    # different, possibly much older session next to this session's own route
    # recommendation reads as related when it is not. Read live for the same reason
    # `route_text` above is -- the point is to see it the moment it happens.
    try:
        from . import ledger

        last = ledger.last_delegation(payload.get("session_id"))
    except Exception:
        last = None
    # Not repeated when the route segment already shows this very run.
    if route_run and last and route_run.get("at") == last.get("at"):
        last = None
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

    # Known Jev spend this session -- classification plus the council's own gate and
    # judge, never the council members' own answers (see `session_jev_cost`'s
    # docstring for why). Silent at exactly $0.00, like the other spend segments.
    try:
        from . import ledger

        jev_cost = ledger.session_jev_cost(payload.get("session_id"))
        if jev_cost > 0:
            segments.append(_colour(f"jev:${jev_cost:.4f}", "90"))
    except Exception:
        pass

    gpu = facts.get("gpu")
    if gpu:
        segments.append(f"gpu {gpu['free_mib'] / 1024:.1f}G free")

    segments.append(_colour("proxy ok", "32") if facts.get("proxy")
                    else _colour("proxy DOWN", "31"))

    # The status line cannot afford to run `doctor` itself (schtasks calls, a TLS
    # chain walk -- seconds, not the milliseconds a repaint budget allows), so this
    # reads whatever the session-start hook last cached, passively and without a TTL.
    # Silent when there is no cached report yet, or when it was clean.
    try:
        from . import doctor

        cached = doctor.cached()
        if cached and cached.get("problems"):
            colour = "31" if cached["worst"] == doctor.BAD else "33"
            segments.append(_colour(f"doctor:{len(cached['problems'])}", colour))
    except Exception:
        pass

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

    # Per session, for the prompt hook: `quota.json` above is machine-wide and would
    # hand one session another's number.
    try:
        context_watch.record(
            payload.get("session_id"),
            (payload.get("context_window") or {}).get("used_percentage"),
        )
    except Exception:
        pass

    try:
        line = render(payload, snapshot)
    except Exception:
        line = "ruti: status unavailable"

    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""How much of the Claude subscription window is left, and what that permits.

Claude Code does not write its usage counters anywhere on disk. The only local source
is the JSON it hands a configured `statusLine` command on stdin, which carries
`rate_limits.five_hour` and `rate_limits.seven_day` as a used percentage plus a reset
timestamp. `ruti statusline` captures that into `quota.json`; everything here reads it.

Two things make this more than a percentage lookup:

* **Rate matters more than level.** 45% used one hour into a five-hour window is a
  problem; 45% used four hours in is fine. The band is chosen from the projection to
  the reset, not from the instantaneous number.
* **Staleness is not the same as safety.** The status line only runs while a TUI is
  rendering, so in `-p` runs, background tasks, and between sessions the file freezes.
  A stale 30% can be a real 90%. Past a threshold the reading is treated as *unknown*,
  and unknown assumes the worst plausible band rather than the last one seen -- because
  the failure being guarded against is the manager running out of budget mid-task, and
  optimism there costs the whole session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import QUOTA_FILE, read_json, write_json

GREEN, YELLOW, ORANGE, RED, CRITICAL, UNKNOWN = (
    "GREEN", "YELLOW", "ORANGE", "RED", "CRITICAL", "UNKNOWN"
)

# Upper bound of five-hour utilisation for each band.
BAND_CEILINGS = ((GREEN, 40.0), (YELLOW, 70.0), (ORANGE, 85.0), (RED, 95.0))

FRESH_SECONDS = 120
STALE_SECONDS = 1800

HISTORY_LIMIT = 64

# What each band permits. The manager's own model matters most: it is the one thing
# that must not run out, because nothing else gets assigned work if it does.
BAND_POLICY: dict[str, dict[str, Any]] = {
    GREEN: {
        "manager": "opus / xhigh",
        "anthropic_executors": ["haiku", "sonnet"],
        "guidance": "Delegate bulk generation; plan and review in session.",
    },
    YELLOW: {
        "manager": "opus / high for planning, sonnet for routine",
        "anthropic_executors": ["haiku", "sonnet"],
        "guidance": "Delegate aggressively. Route through `ruti delegate` so verbose "
                    "output never enters the manager's context.",
    },
    ORANGE: {
        "manager": "sonnet, no opus",
        "anthropic_executors": ["haiku"],
        "guidance": "Everything mechanical goes to a non-Anthropic executor. No "
                    "speculative repo exploration; batch questions.",
    },
    RED: {
        "manager": "haiku, coordination only",
        "anthropic_executors": [],
        "guidance": "Finish the current task, write a handoff note, warn the user. "
                    "Start nothing new.",
    },
    CRITICAL: {
        "manager": "none -- stop",
        "anthropic_executors": [],
        "guidance": "Emit a handoff and stop. The user can keep working through "
                    "`opencode` directly until the window resets.",
    },
    UNKNOWN: {
        "manager": "assume ORANGE",
        "anthropic_executors": ["haiku"],
        "guidance": "The quota reading is too old to trust. Behave as if the window is "
                    "mostly spent until a fresh reading arrives.",
    },
}


@dataclass(frozen=True)
class Window:
    used_percentage: float
    # Claude Code sends this as Unix epoch seconds. Kept permissive because the value
    # is also read back from quota.json files written before that was known, which
    # hold an ISO string instead.
    resets_at: str | int | float | None

    @property
    def resets_in_seconds(self) -> float | None:
        """Seconds until the window resets, or None if that cannot be determined.

        Never raises. The band, and therefore every routing decision, is derived from
        this: a reset timestamp in an unexpected shape must degrade to "unknown", not
        take the status line and the router down with it.
        """
        if self.resets_at in (None, ""):
            return None
        raw = self.resets_at
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            when = datetime.fromtimestamp(float(raw), timezone.utc)
        else:
            try:
                when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        return (when - datetime.now(timezone.utc)).total_seconds()


@dataclass(frozen=True)
class Quota:
    five_hour: Window | None
    seven_day: Window | None
    captured_at: float
    model: str = ""
    effort: str = ""
    history: tuple[tuple[float, float], ...] = ()

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.captured_at)

    @property
    def freshness(self) -> str:
        if self.captured_at <= 0:
            return "never"
        if self.age_seconds <= FRESH_SECONDS:
            return "live"
        if self.age_seconds <= STALE_SECONDS:
            return "stale"
        return "unknown"

    @property
    def burn_rate_per_hour(self) -> float | None:
        """Percentage points of the five-hour window consumed per hour."""
        if len(self.history) < 2:
            return None
        (t0, v0), (t1, v1) = self.history[0], self.history[-1]
        elapsed_hours = (t1 - t0) / 3600
        # Under a few minutes the sample is dominated by noise, and a reset makes the
        # value go down, which is not a negative burn rate but a new window.
        if elapsed_hours < 0.05 or v1 < v0:
            return None
        return (v1 - v0) / elapsed_hours

    @property
    def projected_at_reset(self) -> float | None:
        """Where utilisation lands by the reset if the current rate holds."""
        rate = self.burn_rate_per_hour
        if rate is None or self.five_hour is None:
            return None
        remaining = self.five_hour.resets_in_seconds
        if remaining is None or remaining <= 0:
            return None
        return self.five_hour.used_percentage + rate * (remaining / 3600)

    @property
    def band(self) -> str:
        if self.freshness in ("unknown", "never"):
            return UNKNOWN
        if self.five_hour is None:
            return UNKNOWN

        used = self.five_hour.used_percentage
        band = CRITICAL
        for name, ceiling in BAND_CEILINGS:
            if used < ceiling:
                band = name
                break

        # A trajectory that overruns the window is itself a reason to tighten, even
        # while the absolute number still looks comfortable.
        projected = self.projected_at_reset
        if projected is not None and projected > 100 and band == GREEN:
            band = YELLOW

        # The weekly window can be the binding constraint even when the five-hour one
        # is clear; never report more headroom than the tighter of the two.
        if self.seven_day is not None and self.seven_day.used_percentage >= 90:
            band = {GREEN: ORANGE, YELLOW: ORANGE, ORANGE: RED}.get(band, band)
        return band

    @property
    def policy(self) -> dict[str, Any]:
        return BAND_POLICY[self.band]

    def summary(self) -> str:
        """One line, cheap enough to inject into context on every prompt."""
        if self.five_hour is None:
            return f"quota unknown ({self.freshness})"
        parts = [f"{self.five_hour.used_percentage:.0f}% of the 5h window used"]
        remaining = self.five_hour.resets_in_seconds
        if remaining and remaining > 0:
            parts.append(f"resets in {remaining / 3600:.1f}h")
        projected = self.projected_at_reset
        if projected is not None:
            parts.append(f"on track for {projected:.0f}%")
        if self.freshness != "live":
            parts.append(f"reading is {self.freshness}")
        return f"[{self.band}] " + ", ".join(parts)


def _window(raw: Any) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percentage")
    if used is None:
        return None
    return Window(float(used), raw.get("resets_at"))


def load() -> Quota:
    data = read_json(QUOTA_FILE, default=None)
    if not isinstance(data, dict):
        return Quota(None, None, 0.0)
    return Quota(
        five_hour=_window(data.get("five_hour")),
        seven_day=_window(data.get("seven_day")),
        captured_at=float(data.get("captured_at_epoch") or 0.0),
        model=(data.get("model") or {}).get("display_name", ""),
        effort=data.get("effort") or "",
        history=tuple((float(t), float(v)) for t, v in (data.get("history") or [])),
    )


def capture(payload: dict[str, Any]) -> Quota:
    """Persist a status-line payload, appending to the burn-rate history."""
    limits = payload.get("rate_limits") or {}
    now = time.time()

    previous = read_json(QUOTA_FILE, default={}) or {}
    history = [tuple(entry) for entry in (previous.get("history") or [])]

    five = limits.get("five_hour") or {}
    if five.get("used_percentage") is not None:
        value = float(five["used_percentage"])
        # Only record a genuine change; the status line fires far more often than
        # utilisation moves, and a flat history yields no rate.
        if not history or abs(history[-1][1] - value) > 1e-9:
            history.append((now, value))
        history = history[-HISTORY_LIMIT:]

    record = {
        "version": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "captured_at_epoch": now,
        "five_hour": limits.get("five_hour"),
        "seven_day": limits.get("seven_day"),
        "model": payload.get("model"),
        "effort": (payload.get("effort") or {}).get("level"),
        "context_window": payload.get("context_window"),
        "exceeds_200k_tokens": payload.get("exceeds_200k_tokens"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "history": [list(entry) for entry in history],
    }
    write_json(QUOTA_FILE, record)
    return load()

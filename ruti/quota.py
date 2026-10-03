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

# The weekly window escalates the band in steps rather than one cliff at 90%. Applied
# cumulatively in ascending order, so crossing both tiers still lands exactly where the
# old single ">= 90" check did -- GREEN/YELLOW/ORANGE each move exactly one notch per
# tier crossed. The 90% tier's mapping is unchanged from before this was tiered; only
# the 75% tier is new, and it is what turns a quiet 82% into a visible nudge instead of
# a number nobody reads until it crosses 90.
SEVEN_DAY_ESCALATION: tuple[tuple[float, dict[str, str]], ...] = (
    (75.0, {GREEN: YELLOW}),
    (90.0, {GREEN: ORANGE, YELLOW: ORANGE, ORANGE: RED}),
)

FRESH_SECONDS = 120
STALE_SECONDS = 1800

# The weekly window is not rolling: it resets at a fixed weekly moment, so it started
# exactly seven days before `resets_at` and its pace can be projected from that.
SEVEN_DAY_SECONDS = 7 * 86400
# Too early in the week a linear projection is mostly noise -- a handful of points spent
# against a near-empty denominator reads as a spike that the week never recovers from.
MIN_PACE_ELAPSED_SECONDS = 12 * 3600

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

# ORANGE keeps Opus off the manager because the five-hour limit is a hard stop on this
# account: a manager that runs out strands every task at once. Wait mode takes that away
# -- at 95% it checkpoints and the session resumes after the reset -- so with it on the
# cost of Opus in ORANGE is a pause, not a stranded session. Only ORANGE: RED and
# CRITICAL are too close to 95% for it to matter, and UNKNOWN has no live reading, which
# is exactly what wait mode needs to know when to pause.
WAIT_MANAGER: dict[str, str] = {
    ORANGE: "opus permitted -- wait mode pauses at 95% instead of hitting the hard stop",
}


def manager(band: str, *, wait: bool) -> str:
    """Who may manage the session in this band, given whether wait mode is on."""
    if wait and band in WAIT_MANAGER:
        return WAIT_MANAGER[band]
    return str(BAND_POLICY[band]["manager"])


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


def format_left(seconds: float) -> str:
    """Time left as `4.1d` or `2.2h`. One implementation, because the status line's `/`
    suffix and `summary()`'s reset clause must not drift apart in the rounding."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    return f"{seconds / 3600:.1f}h"


def _local_reset_time(window: Window) -> str | None:
    """The reset in the machine's local time as `"%a %H:%M"`, or None when the
    timestamp is not a plain epoch number.

    An ISO string is what quota.json files written before `resets_at` was known to be
    epoch seconds hold; naming the moment is not worth failing a status line over, so
    the reset clause is dropped instead.
    """
    raw = window.resets_at
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(raw)).astimezone().strftime("%a %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


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
    def seven_day_projected_at_reset(self) -> float | None:
        """Where weekly utilisation lands at the reset if the pace so far holds.

        The five-hour equivalent extrapolates from a measured burn rate over a window
        that may have started at any time. The weekly window needs neither a history nor
        a burn rate: it is not rolling, so it began exactly `SEVEN_DAY_SECONDS` before
        `resets_at` and the average pace since then is the whole projection. None rather
        than a number whenever that arithmetic would be noise -- no weekly reading, no
        usable reset, or too little of the week elapsed to mean anything.
        """
        if self.seven_day is None:
            return None
        remaining = self.seven_day.resets_in_seconds
        if remaining is None or remaining <= 0 or remaining > SEVEN_DAY_SECONDS:
            return None
        elapsed = SEVEN_DAY_SECONDS - remaining
        if elapsed < MIN_PACE_ELAPSED_SECONDS:
            return None
        return self.seven_day.used_percentage * SEVEN_DAY_SECONDS / elapsed

    @property
    def five_hour_reset_passed(self) -> bool:
        """The five-hour window exists and its reset time has already gone by.

        An idle session sends no API requests, so its reading freezes. But a frozen
        reading whose own reset has passed is not a window about to run out -- it is a
        window that has reset and not been measured since, i.e. an empty one. Treat it
        as UNKNOWN anyway and the status line reads `UNKNOWN 93%` for a session that
        has been idle since the last reset, and the prompt hook tells the manager to
        assume the window is spent. `wait.effective_used` already counts this as 0%.
        """
        window = self.five_hour
        if window is None:
            return False
        remaining = window.resets_in_seconds
        return remaining is not None and remaining <= 0

    @property
    def seven_day_reset_passed(self) -> bool:
        window = self.seven_day
        if window is None:
            return False
        remaining = window.resets_in_seconds
        return remaining is not None and remaining <= 0

    @property
    def _five_hour_band(self) -> str:
        """The band the five-hour window alone would give, before the weekly window
        gets a say. Kept separate so `seven_day_binding` can tell "the week made this
        worse" apart from "both windows happen to be bad at once"."""
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
        return band

    @property
    def _base_band(self) -> str:
        """The band before the weekly window gets a say, and what `seven_day_binding`
        compares against.

        Compared against `_base_band` rather than `_five_hour_band` so a passed reset
        does not read as the week having made things worse: an old 93% in a window
        that has since reset is a *reset* window, and blaming the week for it is the
        misreading that sends the manager into a caution it does not need.
        """
        if self.five_hour_reset_passed:
            return GREEN
        return self._five_hour_band

    @property
    def band(self) -> str:
        if self.five_hour is None or self.freshness == "never":
            return UNKNOWN
        # Staleness alone stops meaning unknown once the reset has passed: the window
        # is empty by definition whatever its age, and the first API response of the
        # new window brings a real number. Before the reset a stale reading still has
        # to be treated as unknown -- a frozen 30% can be a real 90%.
        if self.freshness == "unknown" and not self.five_hour_reset_passed:
            return UNKNOWN

        band = self._base_band

        # The weekly window can be the binding constraint even when the five-hour one
        # is clear; never report more headroom than the tighter of the two. Applied in
        # ascending tiers rather than one cliff -- see SEVEN_DAY_ESCALATION. A weekly
        # window whose own reset has passed is a new, empty week, so the percentage
        # left over from the last one must not tighten anything.
        if self.seven_day is not None and not self.seven_day_reset_passed:
            used7 = self.seven_day.used_percentage
            for ceiling, mapping in SEVEN_DAY_ESCALATION:
                if used7 >= ceiling:
                    band = mapping.get(band, band)
        return band

    @property
    def seven_day_binding(self) -> bool:
        """Whether the weekly window is the *reason* `band` is this tight -- not just
        whether it happens to be high while the five-hour window is separately bad on
        its own. Drives the status line's colour and the one extra clause in
        `summary()`: a number that changed nothing is not worth a sentence."""
        if self.five_hour is None or self.seven_day is None:
            return False
        return self.band != self._base_band

    @property
    def policy(self) -> dict[str, Any]:
        return BAND_POLICY[self.band]

    def summary(self) -> str:
        """One line, cheap enough to inject into context on every prompt."""
        if self.five_hour is None:
            return f"quota unknown ({self.freshness})"
        if self.five_hour_reset_passed:
            # The percentage, the burn-rate projection and the "reading is unknown"
            # all describe a window that no longer exists. What is true is the one
            # thing that is: it reset, and nothing has been measured since.
            when = _local_reset_time(self.five_hour)
            parts = [f"5h window reset at {when} -- no reading since" if when is not None
                     else "5h window reset -- no reading since"]
        else:
            parts = [f"{self.five_hour.used_percentage:.0f}% of the 5h window used"]
            remaining = self.five_hour.resets_in_seconds
            if remaining and remaining > 0:
                parts.append(f"resets in {remaining / 3600:.1f}h")
            projected = self.projected_at_reset
            if projected is not None:
                parts.append(f"on track for {projected:.0f}%")
            if self.freshness != "live":
                parts.append(f"reading is {self.freshness}")
        if self.seven_day is not None:
            if self.seven_day_reset_passed:
                parts.append("7d window reset -- no reading since")
            else:
                seven = f"7d {self.seven_day.used_percentage:.0f}% used"
                # Only when there is a real future moment to point at: a reset already
                # in the past is a stale reading, not a schedule worth repeating.
                when = _local_reset_time(self.seven_day)
                left = self.seven_day.resets_in_seconds
                if when is not None and left is not None and left > 0:
                    seven += f", resets {when} (in {format_left(left)})"
                projected7 = self.seven_day_projected_at_reset
                if projected7 is not None:
                    seven += f", on pace for {projected7:.0f}%"
                if self.seven_day_binding:
                    seven += " -- the tighter window"
                parts.append(seven)
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


def _stale(new: Any, old: Any) -> bool:
    """Whether `new` is an out-of-date reading of a window `old` already records."""
    if not isinstance(new, dict) or not isinstance(old, dict):
        return False
    try:
        new_reset, old_reset = float(new["resets_at"]), float(old["resets_at"])
        new_used, old_used = float(new["used_percentage"]), float(old["used_percentage"])
    except (KeyError, TypeError, ValueError):
        return False
    # `resets_at` jitters by a second or so between readings of the same window.
    if new_reset < old_reset - 60:
        return True
    return abs(new_reset - old_reset) <= 60 and new_used < old_used


def capture(payload: dict[str, Any]) -> Quota:
    """Persist a status-line payload, appending to the burn-rate history."""
    limits = payload.get("rate_limits") or {}
    five_new, seven_new = limits.get("five_hour"), limits.get("seven_day")
    if not five_new and not seven_new:
        # A session that has not had an API response yet repaints with no limits at
        # all. `quota.json` is machine-wide, so writing that down wiped every other
        # session's reading: their bands fell to UNKNOWN and `doctor` reported that no
        # reading had ever arrived -- seen on every flow handoff's new window. Nothing
        # measured means nothing to record.
        return load()
    now = time.time()

    previous = read_json(QUOTA_FILE, default={}) or {}
    if not isinstance(previous, dict):
        previous = {}
    history = [tuple(entry) for entry in (previous.get("history") or [])]

    # Every open session repaints, and one that has not had an API response for a while
    # keeps reporting what it last saw: two sessions side by side wrote 37, 32, 37, 32
    # every few seconds, the history filled with the saw-tooth, and the burn rate --
    # read as "it went down, so a reset" -- vanished. Usage never falls inside a window,
    # so a lower or older reading of a window already recorded is someone's stale view.
    if _stale(five_new, previous.get("five_hour")):
        five_new = None
    if _stale(seven_new, previous.get("seven_day")):
        seven_new = None
    if not five_new and not seven_new:
        return load()

    # The same, one window at a time: a session can carry the weekly window without the
    # five-hour one -- seen live, on every repaint of a second open session -- and
    # blanking the five-hour reading for that sent every band back to UNKNOWN. A window
    # the payload lacks is kept from the last reading. The capture time follows the
    # five-hour window, the one the bands are built on, so a kept reading ages as it
    # should instead of passing for live.
    captured = now if five_new else float(previous.get("captured_at_epoch") or now)

    five = five_new or {}
    if five.get("used_percentage") is not None:
        value = float(five["used_percentage"])
        # Only record a genuine change; the status line fires far more often than
        # utilisation moves, and a flat history yields no rate.
        if not history or abs(history[-1][1] - value) > 1e-9:
            history.append((now, value))
        history = history[-HISTORY_LIMIT:]

    record = {
        "version": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured)),
        "captured_at_epoch": captured,
        "five_hour": five_new or previous.get("five_hour"),
        "seven_day": seven_new or previous.get("seven_day"),
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

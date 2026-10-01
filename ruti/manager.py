"""Which seat the main session should be in: model alias x effort, for this task and
this budget.

Claude Code's own model cannot be switched from inside a session. No hook can return a
model or an effort, and an edit to `settings.json` is not picked up by a running
session. The user switches with `/model <alias>` and `/effort <level>`; a *new* session
can be started as `claude --model <alias> --effort <level>`. So this module cannot fix a
wrong seat -- it can only say which one is right, and it says it where the user will
actually see it: the prompt hook's visible `systemMessage`, the status line, and the
argv a flow handoff launches the next session with.

The economics that decide what "right" means are asymmetric, and the whole ranking rests
on them:

* The prompt cache is **per model**. Switching model re-reads the whole context
  uncached, and an Opus cache read costs the same as a Sonnet one -- so the cache is not
  a reason to stay, only a reason not to switch casually. A switch pays when the context
  is still small, or when the current model is underpowered for the task at hand.
* An **effort-only change is cheap**: same model, same cache. That is why a different
  effort on the right model is always "now", and only a different *model* is ever
  deferred to a boundary.
* The five-hour window is a hard stop on this account, so what the band permits is a
  policy question, not a score question -- the blocked list is short, stated in words,
  and every blocked seat keeps its score rather than disappearing.

The numbers below are the same kind of constants `router.py` carries, and are read as
such: a rough ordering that keeps the manager on a cheaper seat when the work does not
need a stronger one, not a measurement. The one place they come from output price is
`MODEL_BURN`, which is why Fable is priced off the same axis as everything else -- it is
also the reason it is refused by default, since it bills usage credits rather than
subscription window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import modes, quota
from .config import STATE_ROOT, read_json, write_json

# `router` and `jev` are imported inside the functions that use them, not here. The
# status line calls `record_seat` below on every repaint, and `router` pulls in httpx
# and `jev` pulls in urllib -- together about 300ms of import against a repaint's
# budget, for a module that has to answer "which seat is this session on".

# The aliases `claude --model` and `/model` accept, and the efforts `/effort` takes.
MODELS: tuple[str, ...] = ("opus", "sonnet", "haiku", "fable")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Capability ceiling per model, 0..1: how hard a task the seat is asked to manage before
# it is underpowered. Fable leads because it is the Opus-class model with the most head
# room; Haiku is not a manager, it is coordination.
MODEL_CAPABILITY: dict[str, float] = {
    "opus": 0.95, "sonnet": 0.82, "haiku": 0.55, "fable": 1.00,
}

# What an effort level adds to that ceiling. High is the neutral zero, so the numbers
# above and below it say how much a step either way is worth. Low is set just far enough
# down that it clears boilerplate but not ordinary implementation: Anthropic's guidance
# is medium as the starting point for agentic coding, low for simple tasks.
EFFORT_CAPABILITY: dict[str, float] = {
    "low": -0.15, "medium": -0.05, "high": 0.0, "xhigh": 0.03, "max": 0.05,
}

# Relative quota burn per unit of work, opus/high = 1.0. The model factor is the
# published output price per MTok -- Opus 5.5 $20, Sonnet 5.5 $10, Haiku 4.5 $5,
# Fable 5.1 $50 -- normalised against Opus, so burn is the price axis and nothing else.
MODEL_BURN: dict[str, float] = {
    "opus": 1.0, "sonnet": 0.5, "haiku": 0.25, "fable": 2.5,
}

# What an effort level costs on top of that. The same model, thinking longer: no change
# of seat, but the same seat thinking for longer.
EFFORT_BURN: dict[str, float] = {
    "low": 0.45, "medium": 0.7, "high": 1.0, "xhigh": 1.35, "max": 1.8,
}

# Kinds whose judgement must not be handed to a model that cannot see the consequences.
# A cheap seat here is not cheaper, it is wrong.
MANAGER_ONLY_KINDS = ("security", "review")

# Below this many tokens in the context window, re-reading it uncached on a model switch
# costs less than carrying the wrong seat for the rest of the task.
SWITCH_FREE_TOKENS = 30_000

# How much each band pays for capability over burn. `route`'s ordering with a different
# pair of numbers: there the executor is picked and paid for, here the manager's own
# model is being chosen and every token is spent from the same five hours.
PRESSURE: dict[str, float] = {
    quota.GREEN: 0.15, quota.YELLOW: 0.35, quota.ORANGE: 0.6,
    quota.UNKNOWN: 0.6, quota.RED: 0.9, quota.CRITICAL: 0.9,
}
QUALITY: dict[str, float] = {
    quota.GREEN: 0.5, quota.YELLOW: 0.2, quota.ORANGE: 0.05,
    quota.UNKNOWN: 0.05, quota.RED: 0.0, quota.CRITICAL: 0.0,
}

# The one sentence each band contributes to a seat's reasons, so a ranking reads as a
# judgement about the budget rather than as arithmetic.
BAND_EFFECT: dict[str, str] = {
    quota.GREEN: "the window is largely intact, so capability is worth more than saving it",
    quota.YELLOW: "the window is half spent: capability still wins, but each 1x of burn now counts",
    quota.ORANGE: "the window is near its edge, so burn dominates what extra capability buys",
    quota.UNKNOWN: "the reading is too old to trust, so burn is priced as though the window were nearly spent",
    quota.RED: "the window is nearly gone: only the cheapest coordination-grade seats are permitted",
    quota.CRITICAL: "nothing is permitted -- write the handoff and stop",
}

SEATS_FILE = STATE_ROOT / "seats.json"

# A record this old belongs to a session that is long over.
MAX_AGE_SECONDS = 7 * 86400

# The hint's own confidences. A kind is acted on at the same bar `route` ranks a hint
# at; a difficulty is a continuous number and only needs the looser one, because it
# moves a score rather than choosing a kind of work.
KIND_CONFIDENCE = 0.7
DIFFICULTY_CONFIDENCE = 0.6

# How long to wait for the classifier when a flow handoff names the next session's work.
# A handoff is read once, before a window opens, so this can afford more than the
# prompt hook's hint can.
HANDOFF_TIMEOUT = 3.0


@dataclass(frozen=True)
class Seat:
    model: str  # alias accepted by `claude --model` and `/model`
    effort: str | None  # None only for haiku, which takes no effort parameter

    def label(self) -> str:
        return f"{self.model}/{self.effort}" if self.effort else self.model

    def commands(self) -> list[str]:
        """What the user types to get here, in the order they type it."""
        if not self.effort:
            return [f"/model {self.model}"]
        return [f"/model {self.model}", f"/effort {self.effort}"]

    def cli_args(self) -> list[str]:
        """The same seat as argv, for launching a session on it directly."""
        if not self.effort:
            return ["--model", self.model]
        return ["--model", self.model, "--effort", self.effort]


def seats() -> tuple[Seat, ...]:
    """Every seat there is. Haiku is one: it takes no effort parameter."""
    out: list[Seat] = []
    for model in MODELS:
        if model == "haiku":
            out.append(Seat(model, None))
            continue
        out.extend(Seat(model, effort) for effort in EFFORTS)
    return tuple(out)


def capability(seat: Seat) -> float:
    return MODEL_CAPABILITY.get(seat.model, 0.0) + EFFORT_CAPABILITY.get(seat.effort, 0.0)


def burn(seat: Seat) -> float:
    # Haiku has no effort level, so it takes the neutral capability delta and the
    # neutral burn factor rather than being special-cased at every use.
    return MODEL_BURN.get(seat.model, 1.0) * EFFORT_BURN.get(seat.effort, 1.0)


@dataclass
class Ranked:
    seat: Seat
    score: float
    capability: float
    burn: float
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.blockers


@dataclass
class Advice:
    ranked: list[Ranked]  # eligible first, then by score descending
    best: Ranked | None  # the first eligible one, None when nothing is
    current: Seat | None  # what the session runs now, when it has been recorded
    switch: dict[str, str]
    task: dict[str, Any]
    band: str

    def summary(self) -> dict[str, Any]:
        """The JSON `ruti manager --json` emits, in `ruti route --json`'s shape."""
        return {
            "band": self.band,
            "task": dict(self.task),
            "current": _render_seat(self.current),
            "switch": dict(self.switch),
            "best": _render(self.best) if self.best is not None else None,
            "ranked": [_render(entry) for entry in self.ranked],
        }


def _render_seat(seat: Seat | None) -> dict[str, Any] | None:
    if seat is None:
        return None
    return {"seat": seat.label(), "model": seat.model, "effort": seat.effort,
            "commands": seat.commands(), "cli": seat.cli_args()}


def _render(entry: Ranked) -> dict[str, Any]:
    return {
        "seat": entry.seat.label(),
        "model": entry.seat.model,
        "effort": entry.seat.effort,
        "score": round(entry.score, 3),
        "capability": round(entry.capability, 3),
        "burn": round(entry.burn, 3),
        "eligible": entry.eligible,
        "command": "  ".join(entry.seat.commands()),
        "cli": entry.seat.cli_args(),
        "reasons": entry.reasons,
        "blockers": entry.blockers,
    }


def _blockers(seat: Seat, band: str, *, manager_only: bool, allow_fable: bool,
              wait: bool) -> list[str]:
    """Why this seat may not manage this session, in the order the policy states it."""
    if band == quota.CRITICAL:
        return ["CRITICAL: stop, hand off"]

    out: list[str] = []
    if seat.model == "fable" and not allow_fable:
        out.append("Fable bills usage credits, which is money; pass --fable to consider it")
    if manager_only and seat.model not in ("opus", "fable"):
        out.append("security/review work stays with an Opus-class manager")

    if band == quota.GREEN:
        return out

    if band == quota.RED:
        # What RED leaves is coordination: haiku, or sonnet thinking briefly.
        if seat.model == "haiku" or (seat.model == "sonnet" and seat.effort in ("low", "medium")):
            return out
        out.append("the RED band leaves only haiku, and sonnet at low or medium")
        return out

    # Fable is priced off Opus, so it follows Opus's band rules rather than getting one
    # of its own -- which is what the `opus, fable` pairs below mean.
    if band == quota.YELLOW:
        if seat.model in ("opus", "fable") and seat.effort not in (None, "low", "medium", "high"):
            out.append("the YELLOW band caps Opus at high")
        return out

    # ORANGE and UNKNOWN: no Opus manager, because a manager that runs out strands
    # every task at once. Wait mode takes that away -- at 95% it checkpoints and the
    # session resumes after the reset -- so with it on, Opus is affordable up to medium.
    if seat.model in ("opus", "fable"):
        if not wait:
            out.append(f"{band}: no Opus manager")
        elif seat.effort not in (None, "low", "medium"):
            out.append(f"the {band} band permits Opus up to medium when wait mode is on")
    if seat.model == "sonnet" and seat.effort not in (None, "low", "medium", "high"):
        out.append(f"the {band} band caps Sonnet at high")
    return out


def _rank(seat: Seat, band: str, required: float, *, manager_only: bool,
          allow_fable: bool, wait: bool) -> Ranked:
    cap = capability(seat)
    cost = burn(seat)
    fit = 1.0 if cap >= required else 1.0 - 3.0 * (required - cap)
    headroom = max(0.0, cap - required)
    entry = Ranked(seat=seat, score=fit + QUALITY[band] * headroom - PRESSURE[band] * cost,
                   capability=cap, burn=cost)
    entry.reasons.append(
        f"capability {cap:.2f} against the {required:.2f} this task needs"
        + ("" if cap >= required else " -- underpowered")
    )
    entry.reasons.append(f"burn {cost:.2f}x of opus/high")
    entry.reasons.append(BAND_EFFECT.get(band, ""))
    entry.blockers = _blockers(seat, band, manager_only=manager_only,
                               allow_fable=allow_fable, wait=wait)
    return entry


def _verdict(best: Ranked | None, current: Seat | None, required: float,
             tokens: int | None) -> dict[str, str]:
    """Now, at a boundary, stay, or nothing at all -- and why."""
    if best is None:
        return {"verdict": "none",
                "reason": "no seat is eligible in this band; write the handoff instead"}
    if current is None:
        return {"verdict": "now", "reason": "current seat unknown"}
    if best.seat == current:
        return {"verdict": "stay",
                "reason": f"{current.label()} already fits the task"}
    if best.seat.model == current.model:
        return {"verdict": "now",
                "reason": "effort only, on the same model -- the prompt cache is "
                          "per model, so this costs nothing to switch"}
    if capability(current) < required:
        return {"verdict": "now",
                "reason": f"{current.label()} is underpowered for this task"}
    if tokens is None:
        return {"verdict": "now",
                "reason": "no context reading to price a switch against, so it is "
                          "priced as cheap"}
    if tokens < SWITCH_FREE_TOKENS:
        return {"verdict": "now",
                "reason": f"the context is only ~{tokens} tokens, so re-reading it "
                          f"uncached is cheaper than carrying {current.label()} on"}
    return {"verdict": "boundary",
            "reason": f"switching model re-reads ~{tokens} tokens uncached; do it at the "
                      "next boundary (flow handoff, /compact, new session)"}


def advise(*, kind: str | None, difficulty: float | None, session_id: str | None,
           snapshot: quota.Quota | None = None, allow_fable: bool = False,
           current: Seat | None = None, context_tokens: int | None = None,
           difficulty_source: str = "kind") -> Advice:
    """Rank every seat for this task in this band, and say whether to move off the
    one the session is on.

    `snapshot`, `current` and `context_tokens` are all read from disk when not passed,
    which is what the CLI and the hook want; a caller that already has one of them
    (the flow handoff, a test) passes it rather than paying for the round trip.
    """
    from . import router

    snapshot = snapshot or quota.load()
    band = snapshot.band
    session_modes = modes.current(session_id)
    wait_on = bool(session_modes.get("wait"))

    kind = kind or "implement"
    if difficulty is None:
        difficulty = router.KIND_DIFFICULTY.get(kind, 0.5)
        difficulty_source = "kind"
    required = 0.45 + 0.5 * difficulty

    if current is None:
        current = current_seat(session_id)
    # The parameter shadows the module accessor of the same name, so a missing reading
    # comes from the shared record reader rather than a call that would recurse.
    tokens = context_tokens
    if tokens is None:
        tokens = _stored_tokens(_entry(session_id))

    manager_only = kind in MANAGER_ONLY_KINDS
    ranked = [_rank(seat, band, required, manager_only=manager_only,
                    allow_fable=allow_fable, wait=wait_on)
              for seat in seats()]
    eligible = sorted((r for r in ranked if r.eligible), key=lambda r: r.score, reverse=True)
    blocked = sorted((r for r in ranked if not r.eligible), key=lambda r: r.score,
                     reverse=True)
    best = eligible[0] if eligible else None

    return Advice(
        ranked=eligible + blocked,
        best=best,
        current=current,
        switch=_verdict(best, current, required, tokens),
        task={"kind": kind, "difficulty": round(difficulty, 2),
              "required": round(required, 2), "source": difficulty_source},
        band=band,
    )


def prompt_hint(session_id: str | None, kind: str, kind_confidence: float,
                difficulty: float) -> tuple[str, str] | None:
    """(what the model is told, what the user is shown), or None.

    The user-facing half matters most: the model cannot switch its own seat, so a
    recommendation it only reads is a recommendation nobody acts on. It goes out as the
    hook's `systemMessage` -- visible, not injected -- and it goes out once per
    *change*: the same recommendation on the next prompt is noise, and noise is what
    teaches a manager to ignore a line.
    """
    try:
        if not modes.current(session_id).get("manager"):
            return None
        advice = advise(kind=kind, difficulty=difficulty, session_id=session_id,
                        difficulty_source="jev")
        best = advice.best
        if best is None or advice.switch.get("verdict") not in ("now", "boundary"):
            return None
        if not remember_recommendation(session_id, best.seat):
            return None
        now = advice.current.label() if advice.current else "unknown"
        # One command per line: on one line they get pasted together, and
        # `/model sonnet /effort low` is read as a model called "sonnet /effort low".
        commands = "\n".join(f"  {command}" for command in best.seat.commands())
        message = (f"ruti manager: {kind} work -> {best.seat.label()} fits "
                   f"(now {now}). Type, one at a time:\n{commands}")
        if advice.switch["verdict"] == "boundary":
            message += f"\n-- at the next boundary: {advice.switch['reason']}"
        # The model is told to say it itself, as the first line of its reply: the hook's
        # `systemMessage` was not shown in a live CLI session, and a recommendation
        # nobody sees is one nobody acts on. Both routes carry the same text.
        note = (f"{message}\nThe user switches; you cannot. Open your reply with this "
                "recommendation, commands on separate lines, then carry on with the work.")
        return note, message
    except Exception:
        return None


def seat_for_handoff(text: str, session_id: str | None) -> Seat | None:
    """The seat the next session should open on, read from the handoff it will start
    with. None when the handoff cannot be classified, which leaves the launch as it is."""
    from . import jev

    try:
        if not (text or "").strip() or not jev.configured():
            return None
        guess = jev.classify(text, timeout=HANDOFF_TIMEOUT)
        if guess is None:
            return None
        advice = advise(kind=guess.kind, difficulty=guess.difficulty,
                        session_id=session_id, difficulty_source="jev")
        return advice.best.seat if advice.best is not None else None
    except Exception:
        return None


# ------------------------------------------------------------- the per-session seat


def _load() -> dict[str, Any]:
    data = read_json(SEATS_FILE, default={})
    return data if isinstance(data, dict) else {}


def _entry(session_id: str | None) -> dict[str, Any]:
    """This session's record, or {} -- never raises, never guesses."""
    try:
        if not session_id:
            return {}
        entry = _load().get(session_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _fresh(entry: Any, now: float) -> bool:
    at = entry.get("at") if isinstance(entry, dict) else None
    return (not isinstance(at, bool) and isinstance(at, (int, float))
            and now - at < MAX_AGE_SECONDS)


def model_alias(model_id: str | None) -> str | None:
    """`claude-opus-5-5` or `Opus 5.5` -> `opus`; None when it is none of them.

    A substring match on the alias is what survives Claude Code renaming a model id, and
    the ids it sends do not have to be the ones `/model` accepts.
    """
    if not model_id:
        return None
    text = str(model_id).lower()
    for alias in MODELS:
        if alias in text:
            return alias
    return None


def _context_tokens(window: Any) -> int | None:
    """Tokens in context from `context_window`, or None when the payload cannot say.

    `current_usage` is the last request's own count, so it is preferred; the percentage
    is rounded to a whole number and needs the window size to mean anything.
    """
    if not isinstance(window, dict):
        return None
    usage = window.get("current_usage")
    if isinstance(usage, dict):
        parts = [usage.get(key) for key in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in parts):
            return int(sum(v for v in parts
                           if isinstance(v, (int, float)) and not isinstance(v, bool)))
    used, size = window.get("used_percentage"), window.get("context_window_size")
    if used is None or size is None:
        return None
    try:
        return int(float(used) / 100.0 * float(size))
    except (TypeError, ValueError):
        return None


def record_seat(session_id: str | None, payload: dict[str, Any]) -> None:
    """Remember the seat this session is on, and how full its context is.

    Called from the status line on every repaint, so: never raises, and writes only when
    something actually moved. The recommendation the hook writes is left alone -- it is
    written by a different caller on a different cadence, and a repaint must not wipe it.
    """
    try:
        if not session_id or not isinstance(payload, dict):
            return
        model = payload.get("model") or {}
        record: dict[str, Any] = {
            # The id first: it is what the API was asked for, where the display name
            # is whatever Claude Code chose to print.
            "model": model_alias(model.get("id")) or model_alias(model.get("display_name")),
            "effort": (payload.get("effort") or {}).get("level") or None,
            "context_tokens": _context_tokens(payload.get("context_window")),
        }
        data = _load()
        previous = data.get(session_id)
        previous = previous if isinstance(previous, dict) else {}
        moved = any(previous.get(key) != value for key, value in record.items())
        if not moved and "at" in previous:
            return
        now = time.time()
        data[session_id] = {**record, "at": now,
                            **({"recommended": previous["recommended"]}
                               if "recommended" in previous else {})}
        write_json(SEATS_FILE, {sid: entry for sid, entry in data.items()
                                if _fresh(entry, now)})
    except Exception:
        pass


def current_seat(session_id: str | None) -> Seat | None:
    """The seat this session runs on, or None when nothing has recorded one."""
    entry = _entry(session_id)
    model = entry.get("model")
    if not isinstance(model, str) or model not in MODELS:
        return None
    effort = entry.get("effort")
    return Seat(model, effort if effort in EFFORTS else None)


def _stored_tokens(entry: dict[str, Any]) -> int | None:
    value = entry.get("context_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def context_tokens(session_id: str | None) -> int | None:
    """How much context this session was carrying at its last repaint."""
    return _stored_tokens(_entry(session_id))


def remember_recommendation(session_id: str | None, seat: Seat) -> bool:
    """Store `seat` as this session's recommendation; True if it is a new one.

    The debounce the prompt hint runs on: showing the same recommendation twice is how a
    line stops being read.
    """
    try:
        if not session_id:
            return False
        data = _load()
        entry = data.get(session_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if entry.get("recommended") == seat.label():
            return False
        now = time.time()
        entry["recommended"] = seat.label()
        entry.setdefault("at", now)
        data[session_id] = entry
        write_json(SEATS_FILE, {sid: other for sid, other in data.items()
                                if _fresh(other, now)})
        return True
    except Exception:
        return False


def recommended(session_id: str | None) -> str | None:
    """The seat last recommended to this session, as a label."""
    value = _entry(session_id).get("recommended")
    return value if isinstance(value, str) and value else None
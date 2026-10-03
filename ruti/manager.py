"""Which seat the main session should be in: model alias x effort, for this task and
this budget.

Claude Code's own model cannot be switched from inside a session. No hook can return a
model or an effort, and an edit to `settings.json` is not picked up by a running
session. The user switches with `/model <alias>` and `/effort <level>`; a *new* session
can be started as `claude --model <alias> --effort <level>`. So this module cannot fix a
wrong seat -- it can only say which one is right, and it says it on every prompt: as the
prompt hook's visible `systemMessage` when the recommendation is new, and -- because
`systemMessage` was not shown in a live CLI session -- as a note the model is told to
repeat as the last line of every reply until the seat matches. The status line shows the
same recommendation as `->sonnet/medium`, and a flow handoff launches the next session
on it.

The economics that decide what "right" means are asymmetric, and the whole ranking rests
on them:

* The prompt cache is **per model**. Switching model re-reads the whole context
  uncached, and an Opus cache read costs the same as a Sonnet one -- so the cache is not
  a reason to stay, only a reason not to switch casually. A switch pays when the context
  is still small, or when the current model is underpowered for the task at hand.
* An **effort-only change is cheap where the docs say it is**: "How Claude Code uses
  prompt caching" states that on Opus 5.5, Sonnet 5.5 and Fable 5.1, changing effort
  keeps the cache (most older models recompute instead, which does not matter for the
  `opus`/`sonnet`/`fable` aliases). So a different effort on the right model is always
  "now", only a different *model* is ever deferred to a boundary, and `advise` prefers a
  same-model seat until a switch outscores it by more than re-reading the context costs
  (`SWITCH_BASE_MARGIN`, `SWITCH_MARGIN_PER_MTOK`).
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

# What a model switch has to be worth before it is taken over staying on the current one.
# Effort is free on Opus 5.5, Sonnet 5.5 and Fable 5.1 -- the cache survives an effort
# change on those -- so a model switch is never quite free, and it gets more expensive
# the more context there is to re-read. Scored in the same units as the seats, because
# that is what it is being compared against.
SWITCH_BASE_MARGIN = 0.03
SWITCH_MARGIN_PER_MTOK = 0.6


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

    def commands(self, current: Seat | None = None) -> list[str]:
        """What the user types to get here, in the order they type it.

        Given the seat the session is on, `/model` is left out when it is already right:
        asking for the model the user is already on is a cache-busting no-op and noise on
        top of noise. An empty list then means there is nothing to type at all.
        """
        if current is not None and current.model == self.model:
            if not self.effort or self.effort == current.effort:
                return []
            return [f"/effort {self.effort}"]
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


def switch_margin(tokens: int | None) -> float:
    """The score a model switch must beat to be worth what re-reading the context costs.

    Zero when the context is small, or unreadable -- which is the same thing to be
    careful in: at that size the re-read is cheap enough that the better seat is simply
    the better seat, whatever the reading would have said.
    """
    if tokens is None or tokens < SWITCH_FREE_TOKENS:
        return 0.0
    return SWITCH_BASE_MARGIN + SWITCH_MARGIN_PER_MTOK * tokens / 1_000_000


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
        best = _render(self.best) if self.best is not None else None
        if best is not None:
            # What is left to type, not what would be typed from scratch: on the model
            # the session is already on, `/model` is a no-op and the only command is
            # the effort one.
            best["commands_now"] = self.best.seat.commands(self.current)
        return {
            "band": self.band,
            "task": dict(self.task),
            "current": _render_seat(self.current),
            "switch": dict(self.switch),
            "best": best,
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
             tokens: int | None,
             effort_first: tuple[str, int] | None = None) -> dict[str, str]:
    """Now, at a boundary, stay, or nothing at all -- and why.

    `effort_first` is the seat a model switch would have recommended, and the size of
    the context that switch would have re-read. Naming it is what turns "effort only"
    from a shrug into an answer: the user is being told what the alternative costs.
    """
    if best is None:
        return {"verdict": "none",
                "reason": "no seat is eligible in this band; write the handoff instead"}
    if current is None:
        return {"verdict": "now", "reason": "current seat unknown"}
    if best.seat == current:
        return {"verdict": "stay",
                "reason": f"{current.label()} already fits the task"}
    if best.seat.model == current.model:
        if effort_first is not None:
            other, read = effort_first
            return {"verdict": "now",
                    "reason": f"effort only on {current.model} -- the prompt cache is kept "
                              f"(Opus 5.5, Sonnet 5.5, Fable 5.1), where {other} would "
                              f"re-read ~{read} tokens uncached"}
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

    # Effort first, when effort is what is actually free. A same-model seat has to be
    # capable enough, so a current seat that is blocked or underpowered still loses here
    # and the switch happens as usual. The ranking is left in pure score order: what
    # this changes is which seat is recommended, not what the seats are worth.
    effort_first: tuple[str, int] | None = None
    if (current is not None and best is not None
            and best.seat.model != current.model):
        same = [r for r in eligible
                if r.seat.model == current.model and r.capability >= required]
        if same and best.score - same[0].score <= switch_margin(tokens):
            effort_first = (best.seat.label(), tokens or 0)
            best = same[0]

    return Advice(
        ranked=eligible + blocked,
        best=best,
        current=current,
        switch=_verdict(best, current, required, tokens, effort_first),
        task={"kind": kind, "difficulty": round(difficulty, 2),
              "required": round(required, 2), "source": difficulty_source},
        band=band,
    )


def prompt_hint(session_id: str | None, kind: str, kind_confidence: float,
                difficulty: float) -> tuple[str, str | None] | None:
    """(what the model is told, what the user is shown), or None.

    Recomputed from scratch on every prompt: the band, the current seat and the size of
    the context are all read fresh, and the ranking is decided again rather than carried
    over. Only the *display* is debounced -- the user is shown a `systemMessage` when the
    recommendation is new, and not on the repeats, because the same line twice is how a
    line stops being read. The model is told to keep saying it either way, since it is
    the model's own last line that the user actually reads in a live CLI session.

    On "stay" the stored recommendation is cleared, so the status line's `->seat` arrow
    disappears the moment the seat matches instead of pointing at a move already made.
    """
    try:
        if not modes.current(session_id).get("manager"):
            return None
        advice = advise(kind=kind, difficulty=difficulty, session_id=session_id,
                        difficulty_source="jev")
        best = advice.best
        if best is None:
            return None
        verdict = advice.switch.get("verdict")
        if verdict == "stay":
            clear_recommendation(session_id, advice.current)
            return None
        if verdict not in ("now", "boundary"):
            return None

        changed = remember_recommendation(session_id, best.seat)
        now = advice.current.label() if advice.current else "unknown"
        # One command per line: on one line they get pasted together, and
        # `/model sonnet /effort low` is read as a model called "sonnet /effort low".
        commands = "\n".join(f"  {command}"
                             for command in best.seat.commands(advice.current))
        message = (f"ruti manager: {kind} work -> {best.seat.label()} fits "
                   f"(now {now}). Type, one at a time:\n{commands}")
        if verdict == "boundary":
            message += f"\n-- at the next boundary: {advice.switch['reason']}"
        # The last line of the reply, not the first: the first line of a reply is where
        # the work is, and a recommendation that displaces the answer gets ignored. The
        # `systemMessage` is the belt to this braces, since it was not shown in a live
        # CLI session at all.
        note = (f"ruti manager: {kind} work -> {best.seat.label()} (now {now}); "
                f"{advice.switch['reason']}. The user switches; you cannot. Make this "
                "the LAST line of your reply -- after everything else, nothing after it "
                "-- in the user's language, each command in its own code span: "
                "`Recommendation: <command> then <command>`. Say it every reply until "
                "the seat matches; it is recomputed for each prompt, so use exactly the "
                f"commands given here, not an earlier one.\n{commands}")
        return note, (message if changed else None)
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
    something actually moved. The recommendation and the last classified task are left
    alone -- they are written by a different caller on a different cadence, and a repaint
    must not wipe either.
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
                            **{key: previous[key]
                               for key in ("recommended", "task", "matched")
                               if key in previous}}
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

    The debounce the prompt hint's user-facing half runs on: showing the same
    recommendation twice is how a line stops being read. The classified task in the same
    record is carried across untouched -- it belongs to a different caller.
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
        entry.pop("matched", None)
        entry.setdefault("at", now)
        data[session_id] = entry
        write_json(SEATS_FILE, {sid: other for sid, other in data.items()
                                if _fresh(other, now)})
        return True
    except Exception:
        return False


def clear_recommendation(session_id: str | None, matched: Seat | None = None) -> None:
    """Drop the stored recommendation, so the status line stops pointing at a move made.

    Called when the advice comes back "stay" -- a pending arrow for a seat the session is
    already on is worse than no arrow. `matched` is that seat: it is stored so the status
    line can say the recommendation was checked and agrees, since silence alone cannot be
    told from a mode that is not running. Writes only when something changes, and never
    raises: this is on the prompt hook's path.
    """
    try:
        if not session_id:
            return
        data = _load()
        entry = data.get(session_id)
        if not isinstance(entry, dict):
            return
        label = matched.label() if matched is not None else None
        if "recommended" not in entry and entry.get("matched") == label:
            return
        kept = {key: value for key, value in entry.items()
                if key not in ("recommended", "matched")}
        if label:
            kept["matched"] = label
        data[session_id] = kept
        now = time.time()
        write_json(SEATS_FILE, {sid: other for sid, other in data.items()
                                if _fresh(other, now)})
    except Exception:
        pass


# How long the last classified task stands in for a prompt too short to classify. A
# "yes, go ahead" is the same work as the prompt before it; half an hour is longer than
# any one turn of it and short enough that the next task is classified fresh.
TASK_MAX_AGE = 1800.0


def remember_task(session_id: str | None, kind: str, difficulty: float) -> None:
    """Store the task this session is on, for the next prompt to reuse.

    The prompt hook classifies only prompts long enough to be a task, and the prompts
    that follow one -- "yes", "carry on" -- are not. Without this, the seat advice would
    go quiet for exactly the turn the user is acting on. Never raises.
    """
    try:
        if not session_id:
            return
        data = _load()
        entry = data.get(session_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        now = time.time()
        entry["task"] = {"kind": str(kind), "difficulty": float(difficulty), "at": now}
        entry.setdefault("at", now)
        data[session_id] = entry
        write_json(SEATS_FILE, {sid: other for sid, other in data.items()
                                if _fresh(other, now)})
    except Exception:
        pass


def last_task(session_id: str | None,
              max_age: float = TASK_MAX_AGE) -> tuple[str, float] | None:
    """(kind, difficulty) of the last classified task, or None when there is none to reuse.

    None rather than a default for anything doubtful -- absent, too old, or not shaped
    like a task -- because a stale kind would rank seats for work this session has
    finished.
    """
    entry = _entry(session_id).get("task")
    if not isinstance(entry, dict):
        return None
    kind, difficulty, at = entry.get("kind"), entry.get("difficulty"), entry.get("at")
    numbers = (difficulty, at)
    if not isinstance(kind, str) or not kind:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           for value in numbers):
        return None
    if time.time() - at > max_age:
        return None
    return kind, float(difficulty)


def matched(session_id: str | None) -> str | None:
    """The seat the last advice found the session already on, as a label."""
    value = _entry(session_id).get("matched")
    return value if isinstance(value, str) and value else None


def recommended(session_id: str | None) -> str | None:
    """The seat last recommended to this session, as a label."""
    value = _entry(session_id).get("recommended")
    return value if isinstance(value, str) and value else None
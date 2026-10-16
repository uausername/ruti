"""Deciding, on a fresh prompt, whether the session should change its own seat.

`manager.advise` ranks seats and `ruti manager` prints a recommendation; this module is
the step after that, for the `ruti-seat` mod that can actually change the model and the
effort (`$.config.set` in the prompt hook, `/effort` once the turn is over). The mod asks
`ruti seat plan` on every prompt and applies what comes back -- so every judgement call
about *when moving is safe* lives here, in tested code, and none of it in the mod.

The rules are deliberately lopsided, because a wrong move costs differently each way:

* Up is taken as soon as the advice says so: a stronger seat costs window, a weaker one
  costs the work.
* Down needs a fresh classification (not a task reused from the last prompt) and the
  same recommendation on two prompts in a row -- the classifier reads the prompt
  together with the assistant's previous message, and one bad guess must not be able
  to cheapen the seat on its own. A move down that is cheap to undo -- effort only, or
  a model switch over a small context -- asks for nothing more: if it was wrong, the
  next prompt moves straight back up, and the round trip costs little or nothing. A
  model switch over a larger context also needs a kind the classifier is sure of and
  an easy task.
* A model switch is only ever taken when `advise` says "now" (small context, or the
  current seat is underpowered); at "boundary" the line stays a recommendation.

Every plan is journalled, applied or not, so the decisions can be read afterwards and
judged against what actually happened -- the journal is written in every mode but `off`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import manager, modes, prompt_context
from .config import read_json, write_json

MODES = ("off", "shadow", "on")
DEFAULT_MODE = "on"

# A move down that switches model over more context than DOWN_CHEAP_TOKENS is only taken
# for work this easy, on a kind the classifier is this sure of.
DOWN_MAX_DIFFICULTY = 0.35
DOWN_MIN_CONFIDENCE = 0.8

# Below this, a wrong model switch down and the switch back up cost about $0.58 together
# (Opus 5.5 / Sonnet 5.5 one-hour cache writes against cache reads) -- six requests of a
# typical Opus session, and far less than the single switch `manager` already accepts
# "now" at SWITCH_FREE_TOKENS -- so the move is treated like an effort change.
DOWN_CHEAP_TOKENS = 50_000


# How long a recommendation counts as "the previous one" for the two-in-a-row rule.
STABLE_WINDOW = 1800.0


def _state_file():
    # Resolved here, not at import, so a test's patched STATE_ROOT applies.
    from .config import STATE_ROOT

    return STATE_ROOT / "auto-seat.json"


def _journal_file():
    from .config import STATE_ROOT

    return STATE_ROOT / "auto-seat.jsonl"


def _state() -> dict[str, Any]:
    data = read_json(_state_file(), default={})
    return data if isinstance(data, dict) else {}


def mode() -> str:
    value = _state().get("mode")
    return value if value in MODES else DEFAULT_MODE


def set_mode(value: str) -> None:
    if value not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {value!r}")
    write_json(_state_file(), {**_state(), "mode": value})


def down_hold(fresh: bool, confidence: float | None, difficulty: float, *,
              model_change: bool = True, tokens: int | None = None) -> str | None:
    """Why auto-seat will not take a move down on this classification, or None.

    The two-in-a-row rule is not checked here: it needs the previous prompt, which only
    `plan` has. An unknown context size is priced as a large one -- the cheap path is
    for moves known to be cheap.
    """
    if not fresh or confidence is None:
        return "a move down needs a fresh classification"
    if not model_change or (tokens is not None and tokens < DOWN_CHEAP_TOKENS):
        return None
    if confidence < DOWN_MIN_CONFIDENCE:
        return f"a model switch down needs kind confidence >= {DOWN_MIN_CONFIDENCE}"
    if difficulty > DOWN_MAX_DIFFICULTY:
        return f"a model switch down needs difficulty <= {DOWN_MAX_DIFFICULTY}"
    return None


def _previous(session_id: str, now: float) -> str | None:
    entry = (_state().get("sessions") or {}).get(session_id)
    if isinstance(entry, dict) and now - float(entry.get("at") or 0) < STABLE_WINDOW:
        label = entry.get("label")
        return label if isinstance(label, str) else None
    return None


def _remember(session_id: str, label: str, now: float) -> None:
    state = _state()
    sessions = {sid: entry for sid, entry in (state.get("sessions") or {}).items()
                if isinstance(entry, dict) and now - float(entry.get("at") or 0) < STABLE_WINDOW}
    sessions[session_id] = {"label": label, "at": now}
    write_json(_state_file(), {**state, "sessions": sessions})


def _mark_applied(session_id: str, label: str, now: float) -> None:
    state = _state()
    applied = {sid: entry for sid, entry in (state.get("applied") or {}).items()
               if isinstance(entry, dict) and now - float(entry.get("at") or 0) < STABLE_WINDOW}
    applied[session_id] = {"label": label, "at": now}
    write_json(_state_file(), {**state, "applied": applied})


def completes_applied(session_id: str, advice: manager.Advice, now: float | None = None) -> bool:
    """Whether the move `advice` recommends is the unfinished half of one already applied.

    A model switch lands at once and the effort follows at the end of the turn, so the
    seat can sit on the new model with the old effort -- and then the effort alone reads
    as an independent move down, which `down_hold` refuses on a reused classification.
    The move was decided when it was applied; finishing it is not a new decision.
    """
    best, current = advice.best, advice.current
    if best is None or current is None or best.seat.model != current.model:
        return False
    entry = (_state().get("applied") or {}).get(session_id)
    moment = time.time() if now is None else now
    return (isinstance(entry, dict) and entry.get("label") == best.seat.label()
            and moment - float(entry.get("at") or 0) < STABLE_WINDOW)


def _journal(row: dict[str, Any]) -> None:
    try:
        journal = _journal_file()
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _classify(session_id: str, prompt: str,
              classify: Callable[..., Any] | None) -> tuple[str, float, float | None, str] | None:
    """(kind, difficulty, kind confidence or None, source), or None when nothing is known.

    Confidence is None for a task reused from an earlier prompt: it is good enough to
    keep a seat from dropping underpowered, and not good enough to cheapen one.
    """
    guess = prompt_context.classify(session_id, prompt, classify=classify)
    if guess is not None and guess.kind_confidence >= manager.KIND_CONFIDENCE:
        manager.remember_task(session_id, guess.kind, guess.difficulty)
        return guess.kind, guess.difficulty, guess.kind_confidence, "fresh"
    task = manager.last_task(session_id)
    if task is not None:
        return task[0], task[1], None, "reused"
    return None


def plan(session_id: str, prompt: str, *, classify: Callable[..., Any] | None = None,
         now: float | None = None) -> dict[str, Any]:
    """What the session should do about its seat for this prompt. Never raises.

    `action` is the move itself -- `model` to set now, `effort` to set when the turn is
    over -- and `apply` says whether the mod should carry it out (mode `on`) or only
    note it (`shadow`).
    """
    moment = time.time() if now is None else now
    current_mode = mode()
    result: dict[str, Any] = {"mode": current_mode, "apply": False, "action": {},
                              "held": None, "session": session_id}
    try:
        if current_mode == "off" or not session_id:
            result["held"] = "off" if current_mode == "off" else "no session id"
            return result

        task = _classify(session_id, prompt, classify)
        if task is None:
            result["held"] = "no task to judge: nothing classified and nothing reused"
            return result
        kind, difficulty, confidence, source = task
        advice = manager.advise(kind=kind, difficulty=difficulty, session_id=session_id,
                                difficulty_source="jev")
        best, current = advice.best, advice.current
        verdict = advice.switch.get("verdict")
        result.update({
            "task": {"kind": kind, "difficulty": round(difficulty, 2),
                     "confidence": confidence, "source": source},
            "band": advice.band, "verdict": verdict, "reason": advice.switch.get("reason"),
            "current": current.label() if current else None,
            "best": best.seat.label() if best else None,
        })
        if best is None or current is None:
            result["held"] = "no eligible seat" if best is None else "current seat unknown"
            return result

        previous = _previous(session_id, moment)
        _remember(session_id, best.seat.label(), moment)

        if verdict == "stay":
            # The seat fits: record that it was checked, so the status line can say
            # `seat✓` -- silence alone reads the same as a manager that is not running.
            manager.clear_recommendation(session_id, current)
        elif verdict in ("now", "boundary"):
            # Kept with its verdict, so the status line can tell a move that is applied
            # from one that waits for the user.
            manager.remember_recommendation(session_id, best.seat, verdict)
        if verdict in ("stay", "none"):
            return result
        if verdict == "boundary":
            result["held"] = "a model switch re-reads the context: left as a recommendation"
            return result

        down = manager.is_down(advice)
        result["direction"] = "down" if down else "up"
        if down and completes_applied(session_id, advice, moment):
            result["completing"] = True
        elif down:
            held = down_hold(source == "fresh", confidence, difficulty,
                             model_change=manager.is_model_change(advice),
                             tokens=advice.tokens)
            if held:
                result["held"] = held
                manager.remember_recommendation(session_id, best.seat, "held")
                return result
            if previous != best.seat.label():
                result["held"] = "a move down needs the same advice on two prompts in a row"
                # Held means the status line must not paint it as a move the manager is
                # about to make (cyan) -- it is not, until the next fresh prompt agrees.
                manager.remember_recommendation(session_id, best.seat, "held")
                return result

        action: dict[str, str] = {}
        if best.seat.model != current.model:
            action["model"] = best.seat.model
        if best.seat.effort and best.seat.effort != current.effort:
            action["effort"] = best.seat.effort
        result["action"] = action
        result["apply"] = bool(action) and current_mode == "on"
        if result["apply"]:
            _mark_applied(session_id, best.seat.label(), moment)
        return result
    except Exception as error:  # a seat hint must never take a prompt down with it
        result["held"] = f"error: {error}"
        return result
    finally:
        _journal({"at": moment, "session": session_id, "prompt_chars": len(prompt or ""),
                  **{key: value for key, value in result.items() if key != "session"}})

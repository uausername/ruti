"""Tell the manager how much budget it has left, on every prompt, for almost nothing.

Without this the manager is blind between explicit `ruti status` calls, and asking it
to check costs a tool call and a round trip -- which is itself budget.

The debounce is the point. An unconditional block of policy text on every prompt would
be a few hundred tokens times however many prompts a session has, which is a real leak
in a tool whose entire purpose is to stop leaks. So: one line normally, and the full
policy only when the band changes or occasionally as a reminder.

The seat advice in manager mode is recomputed on every prompt -- from the current seat,
the band and the size of the context, none of it carried over from the last one. It
reaches the user as `systemMessage`, once per change of recommendation, because no hook
can switch Claude Code's own model and the only way to get the right seat is to show the
user what to type; on the repeats the model carries it instead, as the last line of its
own reply. A prompt too short to classify reuses the last classified task, which is what
"yes, go ahead" is.
"""

from __future__ import annotations

import json
import sys
import time

from ruti import context_watch, jev, ledger, manager, modes, quota, sessions
from ruti.config import STATE_ROOT, read_json, write_json

MEMO_FILE = STATE_ROOT / "hook-memo.json"

# How many prompts may pass before the full policy is restated even if nothing changed.
FULL_EVERY = 12

# Classifying costs about a second and a half of wall clock before the model even
# starts reading, so it is not worth spending on "yes", "continue" or "fix the typo".
# A prompt that actually describes work to be routed is longer than this.
MIN_PROMPT_CHARS = 120

# A classification is only useful where the skip is documented to happen: substantial
# work, begun without a ranking. Below this the guess is too close to call to nag over.
HINT_MIN_CONFIDENCE = 0.7

# Tighter than the library default: this one sits between the user pressing enter and
# the model reading anything, so a slow answer is worth less than no answer.
HINT_TIMEOUT = 2.0


def build_context(prompt: str | None = None, cwd: str | None = None) -> tuple[str, bool]:
    """The injected context line, and whether this was one of the full restatements."""
    return _build(prompt, cwd)[:2]


def build_output(prompt: str | None = None, cwd: str | None = None) -> dict:
    """The whole hook JSON, including the `systemMessage` the user is shown.

    Two audiences, two fields: the context line goes into the model's window, and the
    seat advice goes to the *user* as a visible message -- because the manager cannot
    switch its own model, a recommendation it only reads is a recommendation nobody
    acts on. The model gets the same advice on every prompt, in the context line, and
    repeats it to the user itself.
    """
    line, full, user_message = _build(prompt, cwd)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": line,
        },
        "suppressOutput": True,
    }
    if user_message:
        output["systemMessage"] = user_message
        # The same flag hid the message in a live CLI session, so a message that has to be
        # seen goes out without it.
        output["suppressOutput"] = False
    return output


def _build(prompt: str | None, cwd: str | None) -> tuple[str, bool, str | None]:
    """One pass over the prompt, for both outputs above.

    Shared because the classification is the expensive half: it costs about 1.5s of
    wall clock before the model reads anything, and the route hint and the manager hint
    both want it. Asking once is the difference between one call and two.
    """
    # A session-scoped `ruti off` means routing advice is not just unhelpful here, it's
    # actively wrong -- so replace the whole budget block with a one-line reminder
    # rather than layering it on top.
    session_id = sessions.current_session_id()
    # About the conversation, not delegation, so it survives `ruti off`; and not
    # debounced like the band -- it rides on every prompt while the session is past it.
    context_note = context_watch.warning(session_id)
    if sessions.is_disabled(session_id):
        line = ("ruti: OFF for this session -- `route` and `delegate` refuse. Do "
                "implementation work in-session. Run `ruti on` to resume delegation.")
        if context_note:
            line += "\n" + context_note
        return line, False, None

    snapshot = quota.load()
    memo = read_json(MEMO_FILE, default={}) or {}

    band = snapshot.band
    count = int(memo.get("prompts_since_full", 0)) + 1
    changed = memo.get("last_band") != band
    full = changed or count >= FULL_EVERY

    if snapshot.five_hour is None and snapshot.freshness in ("never", "unknown"):
        # Saying nothing is better than asserting a number we do not have.
        line = ("ruti: subscription usage is unknown (no status-line reading yet). "
                "Treat the budget as tight until one arrives.")
    else:
        line = f"ruti budget -- {snapshot.summary()}"

    if full:
        policy = snapshot.policy
        executors = ", ".join(policy["anthropic_executors"]) or "none"
        waiting = bool(modes.current(session_id).get("wait"))
        line += (
            f"\nManager for this band: {quota.manager(band, wait=waiting)}. "
            f"Anthropic executors permitted: {executors}. "
            f"{policy['guidance']} "
            "Call `ruti route --kind ... --files N --loc N --json` before starting "
            "substantial implementation work, and run delegates through `ruti delegate` "
            "so their output never enters this context."
        )

    # Modes are short and only matter while active, so they ride on every prompt they
    # are set for rather than following the band debounce.
    for note in _mode_notes(session_id):
        line += "\n" + note
    rankings_note = _rankings_note(session_id, cwd)
    if rankings_note:
        line += "\n" + rankings_note
    if context_note:
        line += "\n" + context_note
    if modes.current(session_id).get("wait"):
        from ruti import wait

        line += "\n" + wait.prompt_note(session_id, snapshot)
    if modes.current(session_id).get("flow"):
        from ruti import flow

        line += "\n" + flow.prompt_note(session_id)

    # Asked for once, here, rather than by each hint: both need it and it is the single
    # most expensive thing this hook does -- so not at all when neither will use it.
    outstanding = ledger.unfollowed_route(session_id)
    manager_on = bool(modes.current(session_id).get("manager"))
    guess = _classify_prompt(session_id, prompt) if manager_on or not outstanding else None

    # The manager's own seat, and only when it is wrong for this work. Unlike the route
    # hint below it is *not* suppressed by an unfollowed ranking: nagging about delegation
    # says nothing about which model the session is on.
    user_message = None
    if manager_on:
        kind: str | None = None
        difficulty: float | None = None
        confidence = 0.0
        if guess is not None and guess.kind_confidence >= HINT_MIN_CONFIDENCE:
            kind, difficulty = guess.kind, guess.difficulty
            confidence = guess.kind_confidence
            # Kept for the next prompt, which is very likely to be too short to classify
            # on its own -- "yes, go ahead" is the same work as the prompt above it.
            manager.remember_task(session_id, kind, difficulty)
        else:
            task = manager.last_task(session_id)
            if task is not None:
                # The task goes on; the ranking is still recomputed against the band,
                # the seat and the context as they are right now.
                kind, difficulty, confidence = task[0], task[1], 1.0
        if kind is not None:
            seat_hint = manager.prompt_hint(session_id, kind, confidence, difficulty)
            if seat_hint is not None:
                line += "\n" + seat_hint[0]
                user_message = seat_hint[1]

    # A ranking that named a delegate and was never acted on is the one thing the
    # manager cannot see for itself: the advice scrolls out of context long before the
    # decision it was meant to inform is finished.
    if outstanding:
        line += (
            f"\nruti: the last `ruti route` recommended {outstanding['recommended']} for "
            f"{outstanding.get('kind') or 'this'} work and nothing has been delegated to "
            "it since -- delegate, or say why you are writing it in-session instead."
        )
    elif guess is not None:
        hint = _route_hint(session_id, guess)
        if hint:
            line += "\n" + hint

    write_json(MEMO_FILE, {
        "last_band": band,
        "prompts_since_full": 0 if full else count,
        "at": time.time(),
    })
    return line, full, user_message


def _classify_prompt(session_id: str | None,
                     prompt: str | None) -> "jev.Classification | None":
    """The prompt's classification, or None for every reason not to ask.

    The guards live in one place because both hints depend on exactly these: a prompt
    too short to be a task, the classifier switched off or unconfigured, and a
    blackholed endpoint whose timeout `urlopen` does not actually honour. The probe is
    cached, and caches its failures for minutes, so an outage costs one slow prompt
    rather than all of them.
    """
    try:
        text = (prompt or "").strip()
        if len(text) < MIN_PROMPT_CHARS:
            return None
        if not modes.current(session_id).get("jev", True) or not jev.configured():
            return None
        health = jev.probe()
        if not health or not health.get("ok"):
            return None
        return jev.classify(text, timeout=HINT_TIMEOUT)
    except Exception:
        return None


def _route_hint(session_id: str | None, guess: "jev.Classification") -> str:
    """Name the classification when a prompt looks like work nobody ranked.

    This is the one place the classifier earns its keep. The documented failure is not
    that `route` gives bad answers, it is that it never gets called once a session
    settles into a rhythm -- so the reminder has to arrive already carrying the
    classification, or it is just another line of policy to skim past.

    Everything here fails quiet. No key, the switch off, a slow endpoint, a prompt too
    short to be a task: all of them return "" and the prompt goes on untouched.
    """
    if guess.kind_confidence < HINT_MIN_CONFIDENCE:
        return ""
    # Small work is exactly what the skip rule is for; nagging about it would teach the
    # manager to ignore the line.
    if guess.trivial:
        return ""

    return (
        f"ruti: this reads as `{guess.kind}` work ({guess.kind_confidence:.2f}) and not "
        f"trivial ({guess.trivial_probability:.2f}), and nothing has been ranked for it. "
        f"Call `ruti route --kind {guess.kind} --files N --loc N --json` before starting "
        "-- the size test is per task, not per session."
    )


def _mode_notes(session_id: str | None) -> list[str]:
    state = modes.current(session_id)
    notes: list[str] = []
    if state["coding"]:
        notes.append(modes.coding_note(state["free"]))
    if state["free"] == "soft":
        notes.append(
            "ruti free mode is ON (soft): prefer zero-cost models (`free` router, "
            "`*:free` aliases) when delegating, and warn the user before using a paid "
            "metered API. `ruti route` deprioritises paid APIs but still lists them."
        )
    elif state["free"] == "hard":
        notes.append(
            "ruti free mode is ON (hard): delegate only to zero-cost models (`free` "
            "router, `*:free` aliases). `ruti route` rules out paid metered APIs and "
            "`ruti delegate` refuses them."
        )
    if state.get("council") == "on":
        notes.append(
            "ruti council mode is ON: before settling a genuinely ambiguous or hard to "
            "reverse call -- an architecture choice, a product judgement, a tradeoff "
            "with no obviously right side -- run `ruti council \"<question>\"` and read "
            "the raw answers. Not for mechanical work: `ruti route` is for that."
        )
    elif state.get("council") == "auto":
        notes.append(
            "ruti council mode is AUTO: pass hard calls to `ruti council \"<question>\"` "
            "and let it decide -- it checks whether the question is ambiguous and "
            "consequential enough first, and declines cheaply when it is not, so a "
            "question that turns out not to need a council costs one small call."
        )
    return notes


def _rankings_note(session_id: str | None, cwd: str | None) -> str | None:
    """In coding mode, a model the project's language favours that ruti lacks.

    Offline by construction -- the rankings and the catalogue are read from their
    caches, which `ruti route` and `ruti openrouter suggest` keep current -- and at most
    once a day per language, so a prompt never waits on OpenRouter and the line never
    turns into noise. `cwd` is the session's, from the hook payload; without it there is
    no project to speak of.
    """
    if not cwd:
        return None
    try:
        state = modes.current(session_id)
        if not state["coding"]:
            return None
        from ruti import providers, rankings

        language = rankings.detect_language(cwd)
        if language is None:
            return None
        models = [str(r["model"]) for r in providers.load_registry()["providers"]
                  if r.get("enabled", True) and r.get("model")]
        return rankings.hint(language, models, free_level=state["free"])
    except Exception:
        return None


def main() -> int:
    prompt = ""
    cwd: str | None = None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            prompt = str(payload.get("prompt") or "")
            cwd = str(payload.get("cwd") or "") or None
    except Exception:
        # The stdin must still be drained; a payload we cannot parse just means the
        # hint is skipped, not that the hook fails.
        pass

    try:
        output = build_output(prompt, cwd)
    except Exception:
        # A hook that fails must not block the prompt.
        return 0

    json.dump(output, sys.stdout)
    # Once per prompt, and only after the output is written: this hook sits between the
    # user pressing enter and the model reading anything, so anything it does has to
    # come after the answer. The status line is where this would otherwise belong, and
    # it cannot -- it repaints every few seconds and doctor costs seconds.
    try:
        from ruti import doctor

        doctor.refresh_in_background()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

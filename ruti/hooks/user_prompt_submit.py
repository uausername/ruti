"""Tell the manager how much budget it has left, on every prompt, for almost nothing.

Without this the manager is blind between explicit `ruti status` calls, and asking it
to check costs a tool call and a round trip -- which is itself budget.

The debounce is the point. An unconditional block of policy text on every prompt would
be a few hundred tokens times however many prompts a session has, which is a real leak
in a tool whose entire purpose is to stop leaks. So: one line normally, and the full
policy only when the band changes or occasionally as a reminder.
"""

from __future__ import annotations

import json
import sys
import time

from ruti import jev, ledger, modes, quota, sessions
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


def build_context(prompt: str | None = None) -> tuple[str, bool]:
    # A session-scoped `ruti off` means routing advice is not just unhelpful here, it's
    # actively wrong -- so replace the whole budget block with a one-line reminder
    # rather than layering it on top.
    session_id = sessions.current_session_id()
    if sessions.is_disabled(session_id):
        return (
            "ruti: OFF for this session -- `route` and `delegate` refuse. Do "
            "implementation work in-session. Run `ruti on` to resume delegation.",
            False,
        )

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
        line += (
            f"\nManager for this band: {policy['manager']}. "
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

    # A ranking that named a delegate and was never acted on is the one thing the
    # manager cannot see for itself: the advice scrolls out of context long before the
    # decision it was meant to inform is finished.
    outstanding = ledger.unfollowed_route(session_id)
    if outstanding:
        line += (
            f"\nruti: the last `ruti route` recommended {outstanding['recommended']} for "
            f"{outstanding.get('kind') or 'this'} work and nothing has been delegated to "
            "it since -- delegate, or say why you are writing it in-session instead."
        )
    else:
        hint = _route_hint(session_id, prompt)
        if hint:
            line += "\n" + hint

    write_json(MEMO_FILE, {
        "last_band": band,
        "prompts_since_full": 0 if full else count,
        "at": time.time(),
    })
    return line, full


def _route_hint(session_id: str | None, prompt: str | None) -> str:
    """Name the classification when a prompt looks like work nobody ranked.

    This is the one place the classifier earns its keep. The documented failure is not
    that `route` gives bad answers, it is that it never gets called once a session
    settles into a rhythm -- so the reminder has to arrive already carrying the
    classification, or it is just another line of policy to skim past.

    Everything here fails quiet. No key, the switch off, a slow endpoint, a prompt too
    short to be a task: all of them return "" and the prompt goes on untouched.
    """
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return ""
    if not modes.current(session_id).get("jev", True) or not jev.configured():
        return ""

    # A blackholed endpoint outruns `urlopen`'s timeout -- that is per socket
    # operation, not per call -- and on a dead network the hook measured three and a
    # half seconds, on every prompt. The probe is cached, and caches its failures for
    # minutes, so an outage costs one slow prompt rather than all of them.
    health = jev.probe()
    if not health or not health.get("ok"):
        return ""

    guess = jev.classify(prompt, timeout=HINT_TIMEOUT)
    if guess is None or guess.kind_confidence < HINT_MIN_CONFIDENCE:
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


def main() -> int:
    prompt = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            prompt = str(payload.get("prompt") or "")
    except Exception:
        # The stdin must still be drained; a payload we cannot parse just means the
        # hint is skipped, not that the hook fails.
        pass

    try:
        context, _ = build_context(prompt)
    except Exception:
        # A hook that fails must not block the prompt.
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

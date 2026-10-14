"""What a prompt is asking for, judged with the message it answers -- once per prompt.

The classifier used to see only the user's prompt. A reply such as "yes, go ahead"
carries no task of its own: the task is what the assistant proposed just before it, so
short prompts were not classified at all and reused the last task instead. Worse, asked
on its own, a bare "да" comes back confidently near-trivial -- exactly the guess that
would cheapen the seat in the middle of hard work. Sent together with the assistant's
whole previous message, a reply is judged as what it actually agrees to.

Two callers need the answer for the same prompt, the prompt hook and the auto-seat mod
(through `ruti seat plan`), and they run in no fixed order. Asking twice would cost
little, but two answers can disagree, and then the hint and the seat drift apart. So the
first caller asks and stores the result under the session and the prompt's text; the
second reads it.

The hook is handed the transcript's path; the mod is not, so the path the hook saw is
remembered per session, with a search of Claude Code's projects folder behind it. A
session's first prompt has no previous message anyway. Every failure here returns None,
and the caller does what it did before: classify a long prompt alone, reuse the last
task for a short one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import config, jev, modes

# Without the message it answers, a prompt shorter than this is not a task description --
# "yes", "continue" -- and a guess from its words alone is worse than reusing the last task.
MIN_PROMPT_CHARS = 60

# Shared by the hook and the mod; the measured round trip is ~1.4s with a message attached.
CLASSIFY_TIMEOUT = 4.0

# A classification is for one prompt; this only has to outlive the second caller.
CACHE_TTL = 300.0

# A transcript can run to many MB; the last turn is always near the end.
TAIL_BYTES = 4 * 1024 * 1024

# Only a safeguard -- the whole message is wanted, and the proposal is at its end.
MAX_CONTEXT_CHARS = 30000

TRANSCRIPT_MAX_AGE = 7 * 86400

FENCE = re.compile(r"```.*?```", re.S)


def _transcripts_file() -> Path:
    return config.STATE_ROOT / "transcripts.json"


def _cache_file() -> Path:
    return config.STATE_ROOT / "prompt-classify.json"


def _texts(content: Any) -> str | None:
    """The text of a message's content, or None if it carries a tool result."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _user_prompt(record: dict[str, Any]) -> str | None:
    """The text of a prompt the user typed, or None for every other kind of record.

    Tool results, hook and command output, notifications and the compaction summary all
    arrive as `user` records too; what they share is that the user did not type them.
    """
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    text = _texts((record.get("message") or {}).get("content"))
    text = (text or "").strip()
    if not text or text.startswith("<") or text.startswith("This session is being continued"):
        return None
    return text


def _assistant_text(record: dict[str, Any]) -> str:
    if record.get("type") != "assistant":
        return ""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(block.get("text") or "") for block in content
                     if isinstance(block, dict) and block.get("type") == "text").strip()


def _tail_records(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - TAIL_BYTES))
        data = handle.read()
    if size > TAIL_BYTES:
        data = data[data.find(b"\n") + 1:]  # the first line was cut in half
    records = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and not record.get("isSidechain"):
            records.append(record)
    return records


def clean(text: str) -> str:
    """The message as the user read it, less what says nothing about the task: code
    blocks, and the seat recommendation every reply ends with."""
    text = FENCE.sub("[code omitted]", text)
    lines = [line for line in text.splitlines()
             if not line.lstrip().startswith("Recommendation:")]
    return "\n".join(lines).strip()


def previous_reply(transcript_path: str | None, prompt: str) -> str | None:
    """Everything the assistant said between the user's previous prompt and this one.

    Only its text: tool calls and their output are not part of what the user answered.
    The current prompt may or may not be in the file yet when a hook runs; if the last
    prompt there is this one, the message before it is the one being answered.
    """
    if not transcript_path:
        return None
    try:
        records = _tail_records(Path(transcript_path))
    except OSError:
        return None

    before: list[str] = []   # the turn that ended with the last prompt found
    current: list[str] = []  # the turn after it
    last_prompt: str | None = None
    for record in records:
        typed = _user_prompt(record)
        if typed is not None:
            before, current, last_prompt = current, [], typed
            continue
        text = _assistant_text(record)
        if text:
            current.append(text)

    turn = before if last_prompt is not None and last_prompt == prompt.strip() else current
    text = clean("\n\n".join(turn))
    return text[-MAX_CONTEXT_CHARS:] or None


def transcript_for(session_id: str | None, given: str | None = None) -> str | None:
    """The session's transcript path: the one given (remembered for the mod), the one
    remembered, or the one found under Claude Code's projects folder."""
    if not session_id:
        return None
    try:
        if given and Path(given).is_file():
            _remember(session_id, given)
            return given
        entry = (config.read_json(_transcripts_file(), default={}) or {}).get(session_id)
        if isinstance(entry, dict) and entry.get("path") and Path(entry["path"]).is_file():
            return str(entry["path"])
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        for found in (root / "projects").glob(f"*/{session_id}.jsonl"):
            _remember(session_id, str(found))
            return str(found)
    except Exception:
        pass
    return None


def _remember(session_id: str, path: str) -> None:
    now = time.time()
    known = config.read_json(_transcripts_file(), default={}) or {}
    known = {sid: entry for sid, entry in known.items()
             if isinstance(entry, dict) and now - float(entry.get("at") or 0) < TRANSCRIPT_MAX_AGE}
    known[session_id] = {"path": path, "at": now}
    config.write_json(_transcripts_file(), known)


def _ask(text: str, context: str | None, classify: Callable[..., Any],
         timeout: float) -> tuple[bool, Any]:
    """(whether anything was asked, the classification or None)."""
    if context:
        return True, classify(text, context=context, timeout=timeout)
    if len(text) >= MIN_PROMPT_CHARS:
        return True, classify(text, timeout=timeout)
    return False, None


def classify(session_id: str | None, prompt: str, *, transcript: str | None = None,
             classify: Callable[..., Any] | None = None,
             timeout: float = CLASSIFY_TIMEOUT) -> "jev.Classification | None":
    """The prompt's classification, asked once and shared by the hook and the mod.

    None when nothing could be asked (no session, the classifier off or unconfigured, a
    short prompt with no message to read it against) or the classifier failed. A caller
    that brings its own `classify` gets its own answer, outside the shared cache. Never
    raises.
    """
    try:
        text = (prompt or "").strip()
        if not session_id or not text or not modes.current(session_id).get("jev", True):
            return None
        if classify is None and not jev.configured():
            return None
        context = previous_reply(transcript_for(session_id, transcript), text)
        if classify is not None:
            return _ask(text, context, classify, timeout)[1]

        # The message is part of the key: the same "да" answers a different proposal
        # every time it is typed.
        key = hashlib.sha256(f"{session_id}\n{text}\n{context or ''}".encode("utf-8"))
        key_text = key.hexdigest()[:24]
        try:
            with config.file_lock(f"classify-{key_text}", timeout=timeout + 1.0):
                now = time.time()
                cache = config.read_json(_cache_file(), default={}) or {}
                entry = cache.get(key_text)
                if isinstance(entry, dict) and now - float(entry.get("at") or 0) < CACHE_TTL:
                    stored = entry.get("result")
                    return jev.Classification(**stored) if isinstance(stored, dict) else None
                asked, guess = _ask(text, context, jev.classify, timeout)
                if asked:
                    cache = {k: v for k, v in cache.items() if isinstance(v, dict)
                             and now - float(v.get("at") or 0) < CACHE_TTL}
                    cache[key_text] = {"at": now, "result": (dataclasses.asdict(guess)
                                                             if guess is not None else None)}
                    config.write_json(_cache_file(), cache)
                return guess
        except TimeoutError:
            # The other caller is stuck holding the lock; answer for ourselves.
            return _ask(text, context, jev.classify, timeout)[1]
    except Exception:
        return None

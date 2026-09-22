"""A typed-decision classifier, used to guess what a task *is* before routing it.

`ruti route` takes `--kind`, `--files` and `--loc` on faith: whoever calls it asserts
them, and in practice that caller is a Claude Code session classifying its own work
from memory. That is the one genuinely fuzzy judgement in this tool, and it is the
one that gets skipped -- the global CLAUDE.md carries a whole section about `route`
being dropped task after task once a session settles into a rhythm.

This module asks a model instead. Jev (TypeSafe's "System One") answers a map of
typed questions in a single pass: a Choice over the seven kinds, a Score for how much
judgement the work demands, a Noul for the documented "under ~40 lines" skip test.
Every answer carries a calibrated confidence, which is the part that makes it usable
as a gate rather than a suggestion.

Two rules hold everywhere in here, and the callers depend on both:

*   **Fail open.** No key, no network, a timeout, a malformed body -- every failure
    returns `None` and the caller does exactly what it did before this module
    existed. A classifier that can break a session is worse than no classifier.
*   **Tighten only.** A guess may make routing more conservative (raise the
    difficulty, call something `security`, deny that a task is trivial) on ordinary
    confidence. Making routing *less* conservative needs `HIGH_CONFIDENCE`. The
    asymmetry is deliberate: being wrong in the cautious direction costs a delegation
    that was not strictly necessary, being wrong the other way sends security work to
    a cheap remote model.

Transport is OpenRouter by default. The model is served there as a Labs entry
(`typesafe/jev-1.13`, absent from the public `/api/v1/models` catalogue) behind a
dedicated alpha endpoint, because it returns decisions rather than text. We already
hold an OpenRouter key and `ruti delegate` already ships whole briefs and repository
diffs through it, so classifying a task description there adds no trust boundary that
delegation has not already crossed. TypeSafe's own endpoint speaks the identical wire
format and is kept as a second transport, since the OpenRouter one is marked alpha.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config

# The call sits in front of work that has not started yet, so a slow answer is worth
# less than no answer. Measured round trip on this machine is ~0.7s.
DEFAULT_TIMEOUT = 2.5

# Below this a Choice or Score is not worth acting on at all; at or above it a guess
# may tighten routing. Both thresholds are TypeSafe's own published guidance.
MIN_CONFIDENCE = 0.6
HIGH_CONFIDENCE = 0.85

MODEL = "typesafe/jev-1.13"

TRANSPORTS: dict[str, dict[str, str]] = {
    "openrouter": {
        "url": "https://openrouter.ai/api/alpha/decisions",
        "key_env": "RUTI_OPENROUTER_KEY_1",
    },
    "typesafe": {
        "url": "https://api.typesafe.ai/v1/systemone",
        "key_env": "TYPESAFE_API_KEY",
    },
}
DEFAULT_TRANSPORT = "openrouter"

# Phrased as what the work *is*, not as what a router should do about it: the model is
# classifying a description, and criteria that leak the consequence bias the answer.
KIND_CRITERIA: dict[str, str] = {
    "boilerplate": "Repetitive scaffolding that follows an obvious existing template.",
    "implement": "Build a feature or capability that does not exist yet.",
    "refactor": "Restructure existing code without changing what it does.",
    "debug": "Find and fix the cause of a failure that has already been observed.",
    "analyze": "Investigate, explain or measure something; no code change is the goal.",
    "review": "Judge the correctness or quality of code that already exists.",
    "security": "Auth, secrets, permissions, row-level security, or anything an "
                "attacker would target.",
}

DIFFICULTY_LEVELS: list[str] = ["trivial", "routine", "involved", "demanding", "expert"]


@dataclass
class Classification:
    """What the model made of a task description. Confidences are 0..1."""

    kind: str
    kind_confidence: float
    difficulty: float  # normalised to the 0..1 scale Task.difficulty uses
    difficulty_confidence: float
    trivial: bool
    trivial_probability: float
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model: str = ""
    transport: str = DEFAULT_TRANSPORT
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def confident_kind(self) -> bool:
        return self.kind_confidence >= MIN_CONFIDENCE

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_confidence": round(self.kind_confidence, 3),
            "difficulty": round(self.difficulty, 3),
            "difficulty_confidence": round(self.difficulty_confidence, 3),
            "trivial": self.trivial,
            "trivial_probability": round(self.trivial_probability, 3),
            "cost_usd": self.cost_usd,
            "latency_ms": round(self.latency_ms),
            "model": self.model,
            "transport": self.transport,
        }


PROBE_CACHE = config.STATE_ROOT / "jev-probe.json"

# `ruti doctor` runs on every session start, and a live probe costs ~1.5s there. What a
# success establishes -- that the key works and the endpoint is where we left it --
# changes about once a day, so caching it is the honest trade. A *failure* is cached
# far more briefly: an outage that has since cleared should not keep being reported.
PROBE_MAX_AGE = 6 * 3600
PROBE_FAILURE_MAX_AGE = 300


def api_key(transport: str = DEFAULT_TRANSPORT) -> str | None:
    """The key for a transport, from the LiteLLM .env first and the environment second.

    The .env is where every other provider key on this machine lives; honouring the
    environment too keeps the module testable without touching that file.
    """
    spec = TRANSPORTS.get(transport)
    if not spec:
        return None
    name = spec["key_env"]
    value = config.load_dotenv().get(name) or os.environ.get(name)
    return value.strip() or None if value else None


def configured(transport: str = DEFAULT_TRANSPORT) -> bool:
    return api_key(transport) is not None


def _ssl_context() -> ssl.SSLContext:
    """Verify through ruti's merged CA bundle when there is one.

    This call does not go through the local LiteLLM proxy, so it does not inherit the
    proxy's trust store. On a machine whose TLS is intercepted by antivirus, the
    system default would reject the handshake outright.
    """
    bundle = config.load_dotenv().get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_FILE")
    if bundle and os.path.exists(bundle):
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


def _questions() -> dict[str, Any]:
    return {
        "kind": {
            "type": "choice",
            "instructions": "What kind of software task is this?",
            "criteria": KIND_CRITERIA,
        },
        "difficulty": {
            "type": "score",
            "instructions": (
                "How much judgement about code the author cannot see does this "
                "task demand?"
            ),
            "criteria": DIFFICULTY_LEVELS,
        },
        "trivial": {
            "type": "noul",
            "instructions": (
                "Is this small enough to just write -- roughly under 40 changed "
                "lines across at most two files?"
            ),
            "criteria": {
                "true": "A small, contained edit.",
                "false": "Larger, or spread across several files.",
            },
        },
    }


def _post(body: dict[str, Any], url: str, key: str, timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError, ValueError):
        return None


def _normalise_score(answer: dict[str, Any]) -> float:
    """Map a Score answer onto the 0..1 scale `Task.difficulty` speaks.

    Jev returns the value on the legend's own scale (0..n-1), so five levels give
    0..4 and 2.12 means "a bit past involved".
    """
    legend = answer.get("legend") or {}
    span = max(len(legend) - 1, 1)
    try:
        return min(1.0, max(0.0, float(answer.get("score", 0.0)) / span))
    except (TypeError, ValueError):
        return 0.0


def classify(description: str, *, transport: str = DEFAULT_TRANSPORT,
             timeout: float = DEFAULT_TIMEOUT) -> Classification | None:
    """Classify a task description, or return None if that could not be done.

    Every failure path is `None` on purpose -- see the module docstring.
    """
    description = (description or "").strip()
    if not description:
        return None
    spec = TRANSPORTS.get(transport)
    if not spec:
        return None
    key = api_key(transport)
    if not key:
        return None

    body = {"model": MODEL, "state": description, "questions": _questions()}
    started = time.monotonic()
    payload = _post(body, spec["url"], key, timeout)
    latency_ms = (time.monotonic() - started) * 1000
    if not isinstance(payload, dict):
        return None

    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None
    kind_answer = answers.get("kind") or {}
    difficulty_answer = answers.get("difficulty") or {}
    trivial_answer = answers.get("trivial") or {}

    kind = kind_answer.get("choice")
    if kind not in KIND_CRITERIA:
        return None

    # A Noul carries no confidence of its own: the probability *is* the answer, and
    # its distance from 0.5 is how sure the model is.
    try:
        trivial_probability = float(trivial_answer.get("noul", 0.0))
    except (TypeError, ValueError):
        trivial_probability = 0.0

    usage = payload.get("usage") or {}
    try:
        cost = float(usage.get("cost", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0

    return Classification(
        kind=str(kind),
        kind_confidence=float(kind_answer.get("confidence", 0.0) or 0.0),
        difficulty=_normalise_score(difficulty_answer),
        difficulty_confidence=float(difficulty_answer.get("confidence", 0.0) or 0.0),
        trivial=trivial_probability >= 0.5,
        trivial_probability=trivial_probability,
        cost_usd=cost,
        latency_ms=latency_ms,
        model=str(payload.get("model") or MODEL),
        transport=transport,
        probabilities=dict(kind_answer.get("probabilities") or {}),
    )


def probe(*, transport: str = DEFAULT_TRANSPORT,
          max_age: float = PROBE_MAX_AGE) -> dict[str, Any] | None:
    """Check that the endpoint answers, reusing a recent result rather than re-asking.

    `None` means there is no key at all. Otherwise the record carries `ok`, and on
    success the model, latency and cost of whichever call actually happened -- the
    cached one or a fresh one.
    """
    if not configured(transport):
        return None

    cached = config.read_json(PROBE_CACHE, default=None)
    if isinstance(cached, dict) and cached.get("transport") == transport:
        try:
            age = time.time() - float(cached.get("at", 0) or 0)
        except (TypeError, ValueError):
            age = float("inf")
        ceiling = max_age if cached.get("ok") else min(max_age, PROBE_FAILURE_MAX_AGE)
        if 0 <= age < ceiling:
            return {**cached, "cached": True, "age_seconds": round(age)}

    started = time.monotonic()
    guess = classify("Rename a variable in one file.", transport=transport, timeout=4.0)
    record: dict[str, Any] = {
        "ok": guess is not None,
        "transport": transport,
        "at": time.time(),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "model": guess.model if guess else "",
        "cost_usd": guess.cost_usd if guess else 0.0,
    }
    config.write_json(PROBE_CACHE, record)
    return {**record, "cached": False, "age_seconds": 0}

"""Ask several models the same hard question and show every answer side by side.

Not for mechanical work -- `ruti route` already picks the single cheapest capable
executor for that, and a council is the opposite move: pay for N opinions on purpose
because the question is genuinely ambiguous and a second (and third) opinion is worth
more than the tokens it costs. This is why nothing here calls this automatically --
CLAUDE.md keeps planning and judgement calls in the session, and a council is a
judgement call about a judgement call.

Deliberately smaller than the project that inspired it (github.com/karpathy/
llm-council): no cross-review stage where every model ranks every other model's
answer -- that is O(n^2) calls for a feature nothing here has measured as paying for
itself.

The "no chairman" rule was narrower than it first looked, and `jev.py` is the reason
it was revisited. The original objection was that a synthesising model would spend
real money to save context the manager was going to spend anyway. A typed judge costs
$0.000025, returns a Choice and a Noul rather than prose, and never enters the
manager's context at all -- so the objection does not reach it. What still holds is
the second half: **the raw opinions are always printed in full.** The judge points at
one and says whether the council agreed; it never replaces reading them, and a council
whose answers were hidden behind a verdict would have no reason to exist.

`auto` follows from the same shift. Deciding whether a question is worth N answers was
itself a judgement call, which is why nothing used to convene automatically; it is now
three Nouls and a threshold, and it refuses far more often than it agrees.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import jev
from .config import PROXY_BASE

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 1024

# Each opinion is cut to this before being shown to the judge. Jev holds 32k tokens of
# state; a handful of 1024-token answers fits easily, but a local model told to ramble
# should not be able to push the others out of the window.
JUDGE_EXCERPT_CHARS = 4000

# `auto` convenes only above this. A council costs N model calls against one cheap
# question, so the bar sits higher than the 0.6 a classification acts on.
AUTO_THRESHOLD = 0.6


@dataclass
class Opinion:
    model: str
    ok: bool
    text: str = ""
    error: str = ""
    duration_s: float = 0.0

    def summary(self) -> dict:
        return {
            "model": self.model, "ok": self.ok, "text": self.text,
            "error": self.error, "duration_s": round(self.duration_s, 1),
        }


@dataclass
class CouncilResult:
    question: str
    opinions: list[Opinion] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "question": self.question,
            "opinions": [o.summary() for o in self.opinions],
        }


def default_models() -> list[str]:
    """Every enabled remote provider. Not resident-but-unloaded local models: calling
    one would trigger LM Studio to load it on demand, which is slow and can fail --
    wrong trade for a tool meant to return several answers quickly. Pass --models to
    include a local one deliberately."""
    from . import providers

    registry = providers.load_registry()
    return [r["alias"] for r in registry["providers"] if r.get("enabled")]


def ask(model: str, question: str, *, timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS) -> Opinion:
    """One model's answer, via the LiteLLM proxy so this needs no provider-specific
    code -- the same reason `delegate.who_answers` talks to the proxy rather than the
    provider directly."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "max_tokens": max_tokens,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{PROXY_BASE}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
        return Opinion(model=model, ok=True, text=text,
                        duration_s=time.monotonic() - started)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return Opinion(model=model, ok=False, error=f"HTTP {exc.code}: {detail}",
                        duration_s=time.monotonic() - started)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError,
            json.JSONDecodeError) as exc:
        return Opinion(model=model, ok=False, error=f"{type(exc).__name__}: {exc}",
                        duration_s=time.monotonic() - started)


def convene(question: str, models: list[str], *,
            timeout: float = DEFAULT_TIMEOUT) -> CouncilResult:
    """Every model asked in parallel -- they do not see each other's answers, so wall
    time is the slowest single model, not the sum. No cross-review stage: see the
    module docstring for why that was cut rather than merely deferred."""
    with ThreadPoolExecutor(max_workers=max(1, len(models))) as pool:
        futures = [pool.submit(ask, model, question, timeout=timeout) for model in models]
        opinions = [future.result() for future in futures]
    return CouncilResult(question=question, opinions=opinions)


@dataclass
class Verdict:
    """What the judge made of a finished council. Never replaces the opinions."""

    best: str = ""            # the model whose answer was picked
    confidence: float = 0.0
    agreement: float = 0.0    # probability the answers substantively agree
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    @property
    def usable(self) -> bool:
        return bool(self.best) and self.confidence >= jev.MIN_CONFIDENCE

    @property
    def agreed(self) -> bool:
        return self.agreement >= jev.MIN_CONFIDENCE

    def summary(self) -> dict:
        return {
            "best": self.best,
            "confidence": round(self.confidence, 3),
            "agreement": round(self.agreement, 3),
            "agreed": self.agreed,
            "usable": self.usable,
            "cost_usd": self.cost_usd,
            "latency_ms": round(self.latency_ms),
        }


def judge(result: CouncilResult, *, timeout: float = 10.0) -> Verdict | None:
    """Point at the best-argued answer and say whether the council agreed.

    `None` whenever there is nothing useful to say: fewer than two answers came back,
    no key, or the judge did not respond. The caller prints the opinions either way.

    The weakest link in the whole feature is right here -- a fast decision model is
    being asked to rank reasoning from much larger ones, and it is judging how well an
    answer argues its case, not whether the answer is true. That is why the verdict is
    rendered as a pointer alongside the full text rather than as a conclusion, and why
    a confidence under `MIN_CONFIDENCE` is reported as undecided instead of a winner.
    """
    good = [o for o in result.opinions if o.ok and o.text.strip()]
    if len(good) < 2:
        return None

    labels = {}
    blocks = [f"QUESTION:\n{result.question}\n"]
    for index, opinion in enumerate(good):
        label = chr(ord("A") + index)
        labels[label] = opinion.model
        blocks.append(f"ANSWER {label} (from {opinion.model}):\n"
                      f"{opinion.text.strip()[:JUDGE_EXCERPT_CHARS]}\n")

    payload = jev.ask(
        "\n".join(blocks),
        {
            "best": {
                "type": "choice",
                "instructions": ("Which answer makes the best-reasoned case for "
                                 "itself: most specific, most internally consistent, "
                                 "and most responsive to what was actually asked?"),
                "criteria": {label: f"the answer labelled {label}" for label in labels},
            },
            "agreement": {
                "type": "noul",
                "instructions": "Do these answers substantively agree with each other?",
                "criteria": {
                    "true": "They reach the same conclusion, whatever the wording.",
                    "false": "They recommend materially different things.",
                },
            },
        },
        timeout=timeout,
    )
    if payload is None:
        return None

    answers = payload["answers"]
    chosen = (answers.get("best") or {}).get("choice")
    try:
        confidence = float((answers.get("best") or {}).get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    return Verdict(
        best=labels.get(str(chosen), ""),
        confidence=confidence,
        agreement=jev.noul_of(answers, "agreement"),
        cost_usd=jev.answer_cost(payload),
        latency_ms=payload.get("latency_ms", 0.0),
    )


@dataclass
class Worth:
    """Whether a question earns a council, and the reading behind the answer."""

    convene: bool
    score: float
    ambiguous: float
    consequential: float
    mechanical: float
    cost_usd: float = 0.0

    def summary(self) -> dict:
        return {
            "convene": self.convene,
            "score": round(self.score, 3),
            "ambiguous": round(self.ambiguous, 3),
            "consequential": round(self.consequential, 3),
            "mechanical": round(self.mechanical, 3),
            "cost_usd": self.cost_usd,
        }

    def reason(self) -> str:
        if self.convene:
            return (f"ambiguous {self.ambiguous:.2f}, consequential "
                    f"{self.consequential:.2f} -- worth more than one answer")
        if self.mechanical >= 0.5:
            return (f"mechanical ({self.mechanical:.2f}): this has one right answer, "
                    "so `ruti route` is the call, not a council")
        if self.consequential < AUTO_THRESHOLD:
            return (f"cheap to get wrong (consequential {self.consequential:.2f}) -- "
                    "decide it and move on; a council is for calls that are expensive "
                    "to undo")
        return (f"only one defensible answer (ambiguous {self.ambiguous:.2f}) -- "
                "nothing here for a second opinion to disagree with")


def worth_convening(question: str, *, timeout: float = 5.0) -> Worth | None:
    """Ask whether this question is worth N answers. `None` if that cannot be decided.

    Three readings, and all three must hold -- not averaged. Averaging was tried first
    and let "what should we name the new config flag?" through at 0.60: genuinely
    ambiguous (0.94) and not at all mechanical, with consequence of 0.25 drowned out by
    the other two. A council is N paid calls, so the question has to be one where a
    wrong answer actually costs something; ambiguity alone is what makes it *hard*, not
    what makes it *worth paying for*. The mean is still reported, but only as a
    summary of the reading.

    `None` means the caller should fall back to asking the user, never to convening
    silently -- spending money because a check failed would be the wrong default.
    """
    payload = jev.ask(
        (question or "").strip(),
        {
            "ambiguous": {
                "type": "noul",
                "instructions": ("Could a thoughtful expert defensibly answer this "
                                 "more than one way?"),
                "criteria": {"true": "Reasonable people would disagree.",
                             "false": "There is a single defensible answer."},
            },
            "consequential": {
                "type": "noul",
                "instructions": "Would a wrong answer here be expensive or hard to undo?",
                "criteria": {"true": "Costly, or hard to reverse later.",
                             "false": "Cheap to get wrong and correct afterwards."},
            },
            "mechanical": {
                "type": "noul",
                "instructions": ("Is this mechanical work with one correct answer, "
                                 "rather than a judgement call?"),
                "criteria": {"true": "Mechanical: execute it.",
                             "false": "It needs a judgement."},
            },
        },
        timeout=timeout,
    )
    if payload is None:
        return None

    answers = payload["answers"]
    ambiguous = jev.noul_of(answers, "ambiguous")
    consequential = jev.noul_of(answers, "consequential")
    mechanical = jev.noul_of(answers, "mechanical")
    score = (ambiguous + consequential + (1.0 - mechanical)) / 3.0
    convene = (ambiguous >= AUTO_THRESHOLD
               and consequential >= AUTO_THRESHOLD
               and mechanical < 0.5)
    return Worth(
        convene=convene,
        score=score,
        ambiguous=ambiguous,
        consequential=consequential,
        mechanical=mechanical,
        cost_usd=jev.answer_cost(payload),
    )

"""Ask several models the same hard question and show every answer side by side.

Not for mechanical work -- `ruti route` already picks the single cheapest capable
executor for that, and a council is the opposite move: pay for N opinions on purpose
because the question is genuinely ambiguous and a second (and third) opinion is worth
more than the tokens it costs. This is why nothing here calls this automatically --
CLAUDE.md keeps planning and judgement calls in the session, and a council is a
judgement call about a judgement call.

Deliberately smaller than the project that inspired it (github.com/karpathy/
llm-council): no cross-review stage where every model ranks every other model's
answer (that is O(n^2) calls for a feature this project has no measurement showing
pays for itself yet), and no separate "chairman" model to synthesise a final answer.
The manager reads the raw opinions and *is* the chairman -- an extra API call to do
that job would spend real money to save context the manager was going to spend
reading the opinions anyway.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import PROXY_BASE

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 1024


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

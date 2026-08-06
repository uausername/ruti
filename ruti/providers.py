"""Adding API providers, and proving a key works before writing anything down.

The whole value of this module is in the *failure* messages. On a machine where TLS is
intercepted, every provider call fails with a certificate error -- and a wizard that
reported "key rejected" would convince you that every key you own is invalid. So each
stage of the test maps to a distinct, actionable diagnosis, and nothing touches disk
until all of them pass.

The catalog is derived from `litellm.provider_list` rather than hand-maintained, and
`litellm.validate_environment()` supplies each provider's expected environment
variable, so support for a new provider arrives with a litellm upgrade.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import PROVIDERS_FILE, read_json, write_json

# Shown first in the picker. Everything in litellm.provider_list stays selectable.
FEATURED = (
    ("gemini", "Google AI Studio -- generous free tier"),
    ("openrouter", "One key, hundreds of models, including free ones"),
    ("groq", "Very fast inference, free tier"),
    ("cerebras", "Very fast inference, free tier"),
    ("deepseek", "Cheap and strong at code"),
    ("moonshot", "Kimi"),
    ("dashscope", "Alibaba Qwen API"),
    ("mistral", "Mistral"),
    ("xai", "Grok"),
    ("openai", "Paid"),
    ("anthropic", "Paid API key -- separate from a Claude subscription, so it does "
                  "not draw on the same limit"),
    ("together_ai", "Open models, hosted"),
)

# One trivial tool, used to prove the provider emits a structured call rather than
# describing one in prose. This is the difference between a model that works with
# opencode and one that appears to and then fails mid-task.
_PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "report_status",
            "description": "Report a status string.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        },
    }
]

PASS, FAIL, SKIP = "pass", "fail", "skip"

# Reasoning models spend their output budget on thinking before emitting anything, so
# a tight cap makes them return an empty response -- which looks like "this model
# cannot call tools" when it simply had no room to answer. A probe costs a fraction of
# a cent either way, so the budget is set high enough to be conclusive.
PROBE_MAX_TOKENS = 1024


@dataclass
class Stage:
    name: str
    result: str
    detail: str = ""
    latency_ms: int | None = None


@dataclass
class Verdict:
    stages: list[Stage] = field(default_factory=list)
    supports_tools: bool = False
    models_seen: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether the key authenticates. Tool support is reported, not required."""
        return all(s.result != FAIL for s in self.stages if s.name != "tools")

    @property
    def failure(self) -> Stage | None:
        return next((s for s in self.stages if s.result == FAIL), None)


def known_providers() -> list[str]:
    import litellm

    return sorted(
        p if isinstance(p, str) else getattr(p, "value", str(p)) for p in litellm.provider_list
    )


def env_var_for(provider: str) -> str:
    """The environment variable litellm expects this provider's key in."""
    import litellm

    try:
        info = litellm.validate_environment(model=f"{provider}/probe")
        missing = info.get("missing_keys") or []
        # Several providers accept more than one name; the last is usually the
        # provider-specific one rather than a generic fallback.
        for name in reversed(missing):
            if provider.split("_")[0].upper() in name.upper():
                return name
        if missing:
            return missing[0]
    except Exception:
        pass
    return f"{provider.upper()}_API_KEY"


def ruti_env_var(provider: str, index: int) -> str:
    """A unique variable name so several keys for one provider can rotate."""
    slug = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    return f"RUTI_{slug}_KEY_{index}"


def _classify(exc: Exception) -> tuple[str, str]:
    """Turn a litellm exception into (diagnosis, what to do about it)."""
    import litellm

    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "SSLCertVerificationError" in text:
        return (
            "TLS interception -- your key was never sent",
            "something on this machine (antivirus or a corporate proxy) is re-signing "
            "HTTPS. Run `ruti doctor --fix-tls`, then try again. The key itself is fine.",
        )
    if isinstance(exc, litellm.AuthenticationError):
        return ("the provider rejected the key",
                "check for a trailing newline, a truncated paste, or a key from a "
                "different project")
    if isinstance(exc, litellm.NotFoundError):
        return ("the key is valid, but that model is not available to it",
                "pick a different model")
    if isinstance(exc, litellm.RateLimitError):
        return ("rate limited", "the key works; this is not a failure")
    if isinstance(exc, litellm.Timeout):
        return ("no response before the timeout", "network trouble, or the region is blocked")
    if isinstance(exc, litellm.APIConnectionError):
        return ("could not reach the provider", "check the network and any api_base override")
    return (f"{type(exc).__name__}", text[:200])


def test_key(provider: str, model: str, api_key: str, *, timeout: float = 30.0) -> Verdict:
    """Four stages, each with a distinguishable failure. Costs roughly 35 tokens."""
    import litellm

    from . import tls
    from .config import CA_BUNDLE

    litellm.suppress_debug_info = True
    verdict = Verdict()
    qualified = model if "/" in model else f"{provider}/{model}"

    # 1. TLS. Checked first because when it is broken it breaks *everything*, and
    #    diagnosing it as a bad key is the single most misleading thing this tool
    #    could do.
    if CA_BUNDLE.exists() and tls.bundle_works():
        verdict.stages.append(Stage("tls", PASS, "verified through the merged bundle"))
    else:
        found = tls.detect()
        if found.intercepted:
            verdict.stages.append(Stage(
                "tls", FAIL,
                f"intercepted by {found.issuer!r} -- run `ruti doctor --fix-tls` first. "
                "Your key has not been sent anywhere and nothing was saved.",
            ))
            return verdict
        verdict.stages.append(Stage("tls", PASS, "certifi verifies the chain"))

    # 2. Model discovery. Best effort only: litellm swallows every error here and
    #    returns an empty list, so an empty result proves nothing and must not gate.
    try:
        seen = litellm.get_valid_models(
            check_provider_endpoint=True, custom_llm_provider=provider, api_key=api_key
        )
        verdict.models_seen = [str(m) for m in (seen or [])]
        verdict.stages.append(Stage(
            "discovery",
            PASS if verdict.models_seen else SKIP,
            f"{len(verdict.models_seen)} models listed" if verdict.models_seen
            else "provider does not expose a model list (not an error)",
        ))
    except Exception:
        verdict.stages.append(Stage("discovery", SKIP, "not supported by this provider"))

    # 3. A real completion. This is the actual authentication gate.
    started = time.monotonic()
    try:
        litellm.completion(
            model=qualified,
            messages=[{"role": "user", "content": "Reply with the word ok."}],
            api_key=api_key,
            max_tokens=PROBE_MAX_TOKENS,
            timeout=timeout,
        )
        verdict.stages.append(Stage("chat", PASS, "completed",
                                    int((time.monotonic() - started) * 1000)))
    except (IndexError, AttributeError):
        # The request authenticated and returned, but carried no choices -- litellm
        # trips over that rather than reporting it. Authentication is what this stage
        # tests, and it succeeded.
        verdict.stages.append(Stage("chat", PASS, "authenticated (provider returned no content)"))
    except Exception as exc:
        diagnosis, advice = _classify(exc)
        if "rate limited" in diagnosis:
            verdict.stages.append(Stage("chat", PASS, "rate limited, but the key is valid"))
        else:
            verdict.stages.append(Stage("chat", FAIL, f"{diagnosis} -- {advice}"))
            return verdict

    # 4. Structured tool calling. Reported rather than required: a provider without it
    #    is still useful for prose, it just cannot drive opencode.
    started = time.monotonic()
    try:
        response = litellm.completion(
            model=qualified,
            messages=[{"role": "user", "content": "Report the status 'ready'."}],
            tools=_PROBE_TOOL,
            tool_choice="required",
            api_key=api_key,
            max_tokens=PROBE_MAX_TOKENS,
            timeout=timeout,
        )
        choices = getattr(response, "choices", None) or []
        calls = getattr(choices[0].message, "tool_calls", None) if choices else None
        if calls:
            verdict.supports_tools = True
            verdict.stages.append(Stage("tools", PASS, f"returned {len(calls)} structured call(s)",
                                        int((time.monotonic() - started) * 1000)))
        elif not choices:
            verdict.stages.append(Stage(
                "tools", FAIL,
                f"returned nothing within {PROBE_MAX_TOKENS} tokens -- a reasoning model "
                "may need a larger budget; retry with a non-reasoning variant",
            ))
        else:
            verdict.stages.append(Stage(
                "tools", FAIL,
                "answered in prose instead of emitting a tool call -- usable for text, "
                "but it cannot drive `opencode`",
            ))
    except (IndexError, AttributeError):
        verdict.stages.append(Stage("tools", FAIL, "the provider returned no usable response"))
    except Exception as exc:
        diagnosis, _ = _classify(exc)
        verdict.stages.append(Stage("tools", FAIL, f"tool calling unsupported ({diagnosis})"))

    return verdict


# ------------------------------------------------------------------------- registry


def load_registry() -> dict[str, Any]:
    return read_json(PROVIDERS_FILE, default={"version": 1, "providers": []}) or {
        "version": 1, "providers": []
    }


def save_registry(registry: dict[str, Any]) -> None:
    write_json(PROVIDERS_FILE, registry)


def next_key_index(provider: str) -> int:
    registry = load_registry()
    used = [p for p in registry["providers"] if p["provider"] == provider]
    return len(used) + 1


def litellm_entries(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """LiteLLM model_list entries for every registered provider alias.

    Several keys for the same alias produce several entries with the same
    `model_name`, which is how the router rotates between them and cools down
    whichever one hits its limit.
    """
    registry = registry or load_registry()
    entries = []
    for record in registry["providers"]:
        if not record.get("enabled", True):
            continue
        params: dict[str, Any] = {
            "model": record["model"],
            "api_key": f"os.environ/{record['env_var']}",
        }
        if record.get("api_base"):
            params["api_base"] = record["api_base"]
        entries.append({"model_name": record["alias"], "litellm_params": params})
    return entries

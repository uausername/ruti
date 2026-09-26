"""Which model really did a delegation's work, and what it cost.

The alias a delegation asks for is not always the model that answers. For a router
such as OpenRouter's `pareto-code` it never is: the router picks a real model per
request, and that pick decides both the quality and the bill. A delegation reported
as "pareto-code, substituted: false" was once, in the OpenRouter dashboard, a frontier
model writing a 137-line helper script -- and nothing ruti printed could have said so,
because the proxy rewrites every response to carry the name the client asked for.

`litellm/ruti_usage.py` runs inside the proxy and records the name before it is
overwritten, one line per completion, in `usage.jsonl`. This module reads those lines
back for the time window a delegation occupied. Sources, most trusted first:

1. the provider's own response, as captured by that callback -- no extra request;
2. for OpenRouter, `GET /api/v1/generation?id=` by the generation id the callback
   also kept -- only for requests the first source could not name or price.

What it will not do is fill the gap with the alias. A field that says "pareto-code"
where a model name belongs is how the original problem stayed hidden; "unknown" at
least says there is something to find out.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import LITELLM_CONFIG, STATE_ROOT, load_dotenv

USAGE_LOG = STATE_ROOT / "usage.jsonl"
CALLBACK = "ruti_usage.handler"
UNKNOWN = "unknown"

# Generation lookups cost a round trip each; a long run can make dozens of requests.
# A sample of ten still names the model, and says it was a sample.
MAX_LOOKUPS = 10

# The proxy logs a completion a moment after the client has it, so the final request
# of a run can land after `opencode` has already exited.
SETTLE_SECONDS = 3.0

Lookup = Callable[[str], "dict[str, Any] | None"]


@dataclass
class Usage:
    alias: str
    model: str = UNKNOWN
    models: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    cost_usd: float | None = None
    costed_requests: int = 0
    note: str = ""
    # Requests of this run that another model group answered -- LiteLLM's fallback,
    # standing in once the alias failed -- and which groups those were.
    fallback_requests: int = 0
    fallback_groups: list[str] = field(default_factory=list)

    @property
    def cost_complete(self) -> bool:
        return self.requests > 0 and self.costed_requests == self.requests

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "requests": self.requests,
            "models": self.models,
            "cost_usd": self.cost_usd,
            "cost_complete": self.cost_complete,
        }
        if self.fallback_requests:
            out["fallback_requests"] = self.fallback_requests
            out["fallback_groups"] = self.fallback_groups
        if self.note:
            out["note"] = self.note
        return out


def served_by_fallback(entry: dict[str, Any], alias: str) -> bool:
    """Was this request answered by a group other than the one asked for?

    The group, not the model name: the proxy rewrites the name in the response to the
    alias that was requested, so a fallback answer looks like the real thing to the
    client -- measured on this machine, five runs against `laguna-s-2.1` and `free`
    were all answered by gemini-flash under their own names. A router is unaffected:
    whatever model it picks, its group is still its own alias.
    """
    group = entry.get("group")
    if group and group != alias:
        return True
    try:
        return int(entry.get("fallbacks") or 0) > 0
    except (TypeError, ValueError):
        return False


def recording_wired() -> bool:
    """Whether config.yaml loads the callback. Says nothing about a running proxy
    having been restarted since -- only the absence of lines can say that."""
    try:
        return CALLBACK in LITELLM_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return False


def read_log() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in (USAGE_LOG.with_suffix(".jsonl.1"), USAGE_LOG):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut short by a proxy that died mid-write
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _settle(timeout: float) -> None:
    """Wait until the proxy has stopped appending, or `timeout` passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            age = time.time() - USAGE_LOG.stat().st_mtime
        except OSError:
            return
        if age >= 0.75:
            return
        time.sleep(0.25)


def in_window(entries: list[dict[str, Any]], alias: str, started: float,
              finished: float) -> list[dict[str, Any]]:
    """Completions requested under `alias` that finished while the run was going.

    Matched on the name the client asked for, not on the group that served it, so a
    request a fallback answered is still counted against the run that made it.
    Two runs against the same alias at the same moment cannot be told apart here;
    `delegate` notes when that may have happened.
    """
    return [
        e for e in entries
        if e.get("requested") == alias
        and isinstance(e.get("at"), (int, float))
        and started <= e["at"] <= finished + 2.0
    ]


def openrouter_key(alias: str) -> str | None:
    """The alias's own OpenRouter key, else any: a generation belongs to the account."""
    from . import providers

    env = load_dotenv()
    records = [r for r in providers.load_registry()["providers"]
               if r.get("provider") == "openrouter"]
    records.sort(key=lambda r: r.get("alias") != alias)
    for record in records:
        name = record.get("env_var", "")
        key = env.get(name) or os.environ.get(name)
        if key:
            return key
    return None


def _fill_from_openrouter(entries: list[dict[str, Any]], lookup: Lookup | None,
                          alias: str) -> tuple[int, str]:
    """Ask OpenRouter about requests the proxy could not name or price. In place."""
    wanted = [
        e for e in entries
        if e.get("provider") == "openrouter"
        and str(e.get("id") or "").startswith("gen-")
        and (not e.get("model") or e.get("cost_usd") is None)
    ]
    if not wanted:
        return 0, ""
    if lookup is None:
        key = openrouter_key(alias)
        if not key:
            return 0, "no OpenRouter key on file to look the rest up with"
        from . import openrouter

        def lookup(gen_id: str) -> dict[str, Any] | None:
            return openrouter.generation(gen_id, key)

    sample = wanted[:MAX_LOOKUPS]
    filled = 0
    pending = list(sample)
    # OpenRouter indexes a generation a few seconds after it completes, so the last
    # requests of a run are often not there yet on the first ask.
    for attempt in range(2):
        missed = []
        for entry in pending:
            data = lookup(entry["id"])
            if not data:
                missed.append(entry)
                continue
            if not entry.get("model") and data.get("model"):
                entry["model"], entry["model_source"] = data["model"], "openrouter"
                entry["upstream"] = entry.get("upstream") or data.get("provider_name")
            if entry.get("cost_usd") is None and isinstance(data.get("total_cost"), (int, float)):
                entry["cost_usd"] = float(data["total_cost"])
            filled += 1
        pending = missed
        if not pending or attempt:
            break
        time.sleep(2.0)
    note = ""
    if len(wanted) > len(sample):
        note = f"looked up {len(sample)} of {len(wanted)} unnamed requests at OpenRouter"
    return filled, note


def aggregate(alias: str, entries: list[dict[str, Any]]) -> Usage:
    usage = Usage(alias=alias, requests=len(entries))
    by_model: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if served_by_fallback(entry, alias):
            usage.fallback_requests += 1
            group = str(entry.get("group") or "?")
            if group not in usage.fallback_groups:
                usage.fallback_groups.append(group)
        name = entry.get("model") or UNKNOWN
        row = by_model.setdefault(name, {
            "model": name, "upstream": entry.get("upstream"), "requests": 0,
            "cost_usd": None, "completion_tokens": 0,
        })
        row["requests"] += 1
        row["completion_tokens"] += entry.get("completion_tokens") or 0
        if isinstance(entry.get("cost_usd"), (int, float)):
            row["cost_usd"] = (row["cost_usd"] or 0.0) + entry["cost_usd"]
            usage.costed_requests += 1
            usage.cost_usd = (usage.cost_usd or 0.0) + entry["cost_usd"]

    ranked = sorted(by_model.values(),
                    key=lambda r: (r["model"] != UNKNOWN, r["requests"], r["completion_tokens"]),
                    reverse=True)
    for row in ranked:
        row.pop("completion_tokens", None)
        if row["cost_usd"] is not None:
            row["cost_usd"] = round(row["cost_usd"], 6)
    usage.models = ranked
    # The model that did most of the work. Never the alias -- see the module docstring.
    known = [r for r in ranked if r["model"] != UNKNOWN]
    usage.model = known[0]["model"] if known else UNKNOWN
    if usage.cost_usd is not None:
        usage.cost_usd = round(usage.cost_usd, 6)
    return usage


def collect(alias: str, started: float, finished: float, *,
            settle: float = SETTLE_SECONDS, lookup: Lookup | None = None) -> Usage:
    """The real model(s) and cost behind one run. Never raises."""
    try:
        if settle:
            _settle(settle)
        entries = in_window(read_log(), alias, started, finished)
    except Exception as exc:  # bookkeeping must not fail the delegation it describes
        return Usage(alias=alias, note=f"could not read the proxy's usage log: {exc}")

    if not entries:
        if not recording_wired():
            note = (f"the proxy is not recording which model answers -- add `callbacks: "
                    f"{CALLBACK}` under litellm_settings in litellm/config.yaml and "
                    "restart the proxy")
        else:
            note = ("the proxy logged no completion for this run -- if the usage "
                    "callback was added after the proxy started, restart the proxy")
        return Usage(alias=alias, note=note)

    notes = []
    try:
        _, lookup_note = _fill_from_openrouter(entries, lookup, alias)
        if lookup_note:
            notes.append(lookup_note)
    except Exception as exc:
        notes.append(f"OpenRouter lookup failed: {exc}")

    usage = aggregate(alias, entries)
    if usage.model == UNKNOWN:
        notes.insert(0, f"{usage.requests} request(s) logged, but none named its model")
    usage.note = "; ".join(notes)
    return usage


def _provider_of(event: dict[str, Any]) -> str:
    """Who bills a run recorded before delegations named their provider."""
    if event.get("tier") == "local":
        return "local"
    from . import providers

    alias = str(event.get("model") or "").split("/")[-1]
    for record in providers.load_registry()["providers"]:
        if record.get("alias") == alias and record.get("provider"):
            return str(record["provider"])
    return "unknown"


def summarise_costs(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Money spent by delegations, per billing provider and per model that answered.

    Only a price the provider itself stated is counted. A delegation recorded before
    ruti tracked cost, or on a provider that states none, is counted as a run but not
    as money -- and the report says how many of those there are rather than letting a
    partial sum pass for a total.
    """
    by_provider: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for event in events:
        provider = event.get("provider") or _provider_of(event)
        row = by_provider.setdefault(provider, {"runs": 0, "costed_runs": 0, "usd": 0.0})
        row["runs"] += 1
        cost = event.get("cost_usd")
        if cost is None and provider == "local" and not event.get("substituted"):
            cost = 0.0  # nothing bills a model running on this machine
        if isinstance(cost, (int, float)):
            row["costed_runs"] += 1
            row["usd"] += cost
        for model, detail in (event.get("models") or {}).items():
            m = by_model.setdefault(model, {"runs": 0, "requests": 0, "usd": None})
            m["runs"] += 1
            m["requests"] += detail.get("requests", 0)
            if isinstance(detail.get("cost_usd"), (int, float)):
                m["usd"] = (m["usd"] or 0.0) + detail["cost_usd"]
    for row in by_provider.values():
        row["usd"] = round(row["usd"], 6)
    for row in by_model.values():
        if row["usd"] is not None:
            row["usd"] = round(row["usd"], 6)
    costed = sum(r["costed_runs"] for r in by_provider.values())
    # A local run's $0 is known by construction, so it cannot be what makes money
    # "counted": that takes at least one remote run whose provider stated a price.
    billed_costed = sum(r["costed_runs"] for p, r in by_provider.items() if p != "local")
    return {
        "counted": billed_costed > 0,
        "costed_runs": costed,
        "uncosted_runs": sum(r["runs"] for r in by_provider.values()) - costed,
        "usd": round(sum(r["usd"] for r in by_provider.values()), 6),
        "by_provider": by_provider,
        "by_model": by_model,
    }

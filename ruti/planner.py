"""Deciding whether to load a model alongside the others or swap one out.

The order of preference is deliberate: keep what is already loaded if the newcomer
fits beside it, then try a smaller context window for the newcomer, and only then
start evicting. Unloading is the destructive option and goes last.

One rule has no exceptions: if a model does not fit even alone at the smallest usable
context, ruti refuses rather than quietly falling back to partial CPU offload. A model
that silently runs ten times slower still looks healthy to the router, which will keep
sending it work -- a clear refusal is strictly more useful than a degraded success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import lmstudio, vram
from .config import STATE_FILE, read_json, write_json
from .lmstudio import Model

# Models are loaded under a stable identifier that doubles as the LiteLLM model_name,
# so a swap needs no proxy change at all.
IDENTIFIER_PREFIX = "local-"


def identifier_for(model_key: str) -> str:
    """A stable, filesystem- and URL-safe identifier derived from the model key.

    Version dots collapse rather than becoming separators, so `qwen2.5-coder-14b`
    reads as `local-qwen25-coder-14b` instead of `local-qwen2-5-coder-14b`.
    """
    slug = model_key.split("/")[-1].lower()
    for noise in ("-instruct", "-gguf", "meta-"):
        slug = slug.replace(noise, "")
    slug = slug.replace(".", "")
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug)
    return IDENTIFIER_PREFIX + "-".join(filter(None, slug.split("-")))


@dataclass
class Plan:
    action: str  # "noop" | "alongside" | "swap" | "refuse"
    target: Model | None = None
    identifier: str = ""
    context: int = 0
    estimate: vram.Estimate | None = None
    evict: list[Model] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    deficit_mib: int = 0

    @property
    def ok(self) -> bool:
        return self.action != "refuse"


def footprint(model: Model) -> int:
    """What a loaded model is currently costing, best effort.

    Prefers a measured calibration sample at its actual context; falls back to the
    computed estimate. Used only to work out how much a candidate eviction frees.
    """
    return vram.estimate_for(model, model.loaded_context or 4096).total_mib


def plan_load(
    model: Model,
    *,
    preferred_context: int | None = None,
    min_context: int = vram.CONTEXT_FLOOR,
) -> Plan:
    """Work out how to make `model` resident, without executing anything."""
    gpu = vram.primary_gpu()
    identifier = identifier_for(model.key)
    loaded = lmstudio.loaded_models()

    if gpu is None:
        return Plan(
            action="refuse",
            target=model,
            reasons=[
                "no NVIDIA GPU detected; ruti cannot size a load safely. "
                "Load the model from the LM Studio UI and rerun `ruti models sync`."
            ],
        )

    # 1. Already resident with a big enough window? Leave it alone.
    #
    #    "Big enough" means at least what the caller asked for. A request for a larger
    #    window than is loaded has to go through a reload -- treating it as satisfied
    #    would silently hand back a model that cannot hold the prompt, which is exactly
    #    the failure this tool exists to prevent.
    needed = max(min_context, preferred_context or 0)
    resident = next((e for e in loaded if e.key == model.key), None)
    reload_reasons: list[str] = []
    forced: list[Model] = []

    if resident is not None:
        current = resident.loaded_context or 0
        if current >= needed:
            return Plan(
                action="noop",
                target=resident,
                identifier=resident.identifier or identifier,
                context=current,
                reasons=[f"already loaded as {resident.identifier!r} at {current} tokens"],
            )
        # Resident, but with too small a window. It has to come out and go back in --
        # and its memory is available to the reload, so count it as freed.
        forced = [resident]
        loaded = [m for m in loaded if m.key != model.key]
        reload_reasons = [
            f"loaded at {current} tokens, which is under the {needed} requested; reloading"
        ]

    ceiling = min(model.max_context, preferred_context or model.max_context)
    # An embedding model has no meaningful context knob; do not hold it to the floor
    # that exists to keep a chat model's window usable.
    if model.kind == "embedding":
        min_context = min(min_context, model.max_context)

    # 2. Does it fit beside everything else already loaded?
    available = vram.budget_mib(gpu) + sum(footprint(m) for m in forced)
    fit = vram.largest_fitting_context(model, available, ceiling=ceiling)
    if fit and fit[0] >= min_context:
        context, prediction = fit
        reasons = reload_reasons + [
            f"fits alongside {len(loaded)} loaded model(s) in {available} MiB of budget"
        ]
        if preferred_context and context < preferred_context:
            reasons.append(f"context stepped down {preferred_context} -> {context} to fit")
        action = "swap" if forced else "alongside"
        return Plan(action, model, identifier, context, prediction, forced, reasons)

    # 3. Evict, cheapest first, until it fits. Least-recently-used goes first so an
    #    idle model loses to one actively serving requests.
    order = sorted(loaded, key=lambda m: (m.status == "busy", _last_used(m.key)))
    evicted: list[Model] = list(forced)
    freed = 0
    for victim in order:
        if victim.key == model.key:
            continue
        evicted.append(victim)
        freed += footprint(victim)
        fit = vram.largest_fitting_context(model, available + freed, ceiling=ceiling)
        if fit and fit[0] >= min_context:
            context, prediction = fit
            names = ", ".join(v.identifier or v.key for v in evicted)
            reasons = reload_reasons + [
                f"does not fit alongside the current set; unloading {names} frees {freed} MiB",
            ]
            if preferred_context and context < preferred_context:
                reasons.append(f"context stepped down {preferred_context} -> {context} to fit")
            busy = [v for v in evicted if v.status == "busy"]
            if busy:
                reasons.append(
                    "warning: " + ", ".join(v.identifier or v.key for v in busy) + " is serving requests"
                )
            return Plan("swap", model, identifier, context, prediction, evicted, reasons)

    # 4. Nothing frees enough. Refuse, and say by how much.
    floor_estimate = vram.estimate_for(model, min_context)
    deficit = floor_estimate.total_mib - (available + freed)
    return Plan(
        action="refuse",
        target=model,
        identifier=identifier,
        context=min_context,
        estimate=floor_estimate,
        deficit_mib=max(0, deficit),
        reasons=[
            f"needs {floor_estimate.total_mib} MiB even at the {min_context}-token floor, "
            f"but only {available + freed} MiB can be made available on a "
            f"{gpu.total_mib} MiB card -- short by {max(0, deficit)} MiB",
            "partial CPU offload would load it but run roughly an order of magnitude "
            "slower; ask for it explicitly with --gpu <ratio> if that is what you want",
        ],
    )


def execute(plan: Plan, *, ttl_seconds: int | None = None) -> dict:
    """Carry out a plan and measure what it actually cost.

    Returns a report including the measured VRAM delta next to the predicted one, so a
    silent spill into shared system memory is visible instead of being mistaken for a
    successful load.
    """
    if plan.action == "noop":
        return {"action": "noop", "identifier": plan.identifier, "context": plan.context}
    if plan.action == "refuse":
        raise RuntimeError("; ".join(plan.reasons))
    assert plan.target is not None

    for victim in plan.evict:
        if victim.identifier:
            lmstudio.unload(victim.identifier)

    before = vram.primary_gpu()
    started = time.monotonic()
    lmstudio.load(
        plan.target.key,
        identifier=plan.identifier,
        context_length=plan.context,
        ttl_seconds=ttl_seconds,
    )
    elapsed = time.monotonic() - started
    after = vram.primary_gpu()

    measured = None
    if before and after:
        measured = after.used_mib - before.used_mib
        vram.record_measurement(plan.target.key, plan.context, "max", measured)

    predicted = plan.estimate.total_mib if plan.estimate else None
    spilled = bool(
        measured is not None
        and predicted
        and after is not None
        and after.free_mib < 64
        and measured < predicted * 0.9
    )

    _remember_load(plan.target.key, plan.identifier, plan.context, measured)

    return {
        "action": plan.action,
        "identifier": plan.identifier,
        "context": plan.context,
        "evicted": [v.identifier for v in plan.evict if v.identifier],
        "predicted_mib": predicted,
        "measured_mib": measured,
        "load_seconds": round(elapsed, 1),
        "spilled": spilled,
    }


def _state() -> dict:
    return read_json(STATE_FILE, default={"version": 1, "loads": {}}) or {"version": 1, "loads": {}}


def _last_used(model_key: str) -> float:
    return float((_state().get("loads", {}).get(model_key) or {}).get("at", 0))


def _remember_load(model_key: str, identifier: str, context: int, measured: int | None) -> None:
    state = _state()
    state.setdefault("loads", {})[model_key] = {
        "identifier": identifier,
        "context": context,
        "measured_mib": measured,
        "at": time.time(),
    }
    write_json(STATE_FILE, state)

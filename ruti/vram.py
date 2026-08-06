"""How much memory a model needs, and how much this machine actually has.

Written to be hardware-agnostic. The development box is a 6 GiB laptop GPU where
almost nothing fits alongside anything else, but the same code runs on a 24 GiB card
where co-residency is the normal case -- so every threshold is a fraction of the
detected total with a floor, never a constant tuned to one machine.

Predictions are always crosschecked against measurement. `lms load --estimate-only`
cannot help here (it returns the file size verbatim regardless of context length), so
`ruti model use` samples VRAM before and after a load and files the delta in
`calibration.json`. Measured numbers then override the formula, which absorbs
everything the formula cannot know: flash attention, KV quantization, how much of the
embedding table llama.cpp decided to leave in system RAM, the CUDA context itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import proc
from .config import CALIBRATION_FILE, read_json, write_json
from .gguf import geometry

if TYPE_CHECKING:
    from .lmstudio import Model

# Headroom left for the desktop compositor, the browser, and anything else sharing the
# card. A fraction rather than a constant so a 24 GiB card doesn't reserve a laughable
# 384 MiB while a 6 GiB one doesn't reserve half its memory.
RESERVE_FRACTION = 0.06
RESERVE_FLOOR_MIB = 320

# Extra guard against overcommitting. On Windows' WDDM driver model, exceeding VRAM
# does not fail the allocation -- it silently spills into shared system memory and runs
# roughly an order of magnitude slower, which is far worse than a refusal because the
# router keeps sending work to a model that looks healthy.
MARGIN_FRACTION = 0.04
MARGIN_FLOOR_MIB = 192

# Context ladder, walked downward until something fits.
CONTEXT_STEPS = (131072, 65536, 32768, 24576, 16384, 12288, 8192, 6144, 4096, 3072, 2048)
CONTEXT_FLOOR = 2048


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    total_mib: int
    used_mib: int
    free_mib: int


@dataclass(frozen=True)
class Estimate:
    weights_mib: int
    kv_mib: int
    overhead_mib: int
    context: int
    source: str  # "measured" | "computed"
    confidence: str = "high"  # "high" when read from the weights, "low" when guessed

    @property
    def total_mib(self) -> int:
        return self.weights_mib + self.kv_mib + self.overhead_mib


def probe_gpus(samples: int = 3, interval_s: float = 0.2) -> list[Gpu]:
    """Query the NVIDIA GPUs, taking the *minimum* free memory across samples.

    Free VRAM fluctuates as the desktop and browser allocate and release; planning
    against a momentary peak is how you end up spilling. Returns an empty list when
    there is no NVIDIA GPU -- an integrated or AMD card, or a CPU-only box -- which
    callers must handle rather than treat as an error.
    """
    query = "index,name,memory.total,memory.used,memory.free"
    readings: list[list[Gpu]] = []
    for attempt in range(samples):
        try:
            result = proc.run(
                ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                timeout=10.0,
            )
        except (proc.ToolNotFound, proc.ToolTimeout):
            return []
        if not result.ok:
            return []

        batch = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                batch.append(Gpu(int(parts[0]), parts[1], int(parts[2]), int(parts[3]), int(parts[4])))
            except ValueError:
                continue
        if batch:
            readings.append(batch)
        if attempt < samples - 1:
            time.sleep(interval_s)

    if not readings:
        return []

    merged = []
    for position in range(len(readings[0])):
        candidates = [batch[position] for batch in readings if position < len(batch)]
        worst = min(candidates, key=lambda g: g.free_mib)
        merged.append(worst)
    return merged


def primary_gpu() -> Gpu | None:
    """The GPU LM Studio will use. With several cards, the one with most free memory."""
    gpus = probe_gpus()
    return max(gpus, key=lambda g: g.free_mib) if gpus else None


def budget_mib(gpu: Gpu) -> int:
    """Free memory we are willing to allocate into, after reserve and margin."""
    reserve = max(RESERVE_FLOOR_MIB, int(gpu.total_mib * RESERVE_FRACTION))
    margin = max(MARGIN_FLOOR_MIB, int(gpu.total_mib * MARGIN_FRACTION))
    return max(0, gpu.free_mib - reserve - margin)


def estimate(
    model_path: Path | None,
    architecture: str,
    size_bytes: int,
    context: int,
    *,
    model_key: str | None = None,
    kind: str = "llm",
) -> Estimate:
    """Predict the VRAM a load will consume, preferring measured data when we have it."""
    weights_mib = round(size_bytes / 1024 / 1024)

    if model_key:
        sample = _best_calibration(model_key, context)
        if sample is not None:
            return Estimate(
                weights_mib=weights_mib,
                kv_mib=max(0, sample - weights_mib),
                overhead_mib=0,
                context=context,
                source="measured",
            )

    # Compute buffers and the CUDA context, beyond weights and cache. Scaled off the
    # weights because larger models allocate larger intermediate buffers, with a floor
    # because the CUDA context alone costs a couple of hundred MiB.
    overhead_mib = max(160, round(weights_mib * 0.05))

    # Embedding models run a single forward pass with no autoregressive cache to grow,
    # so charging them a per-token KV cost overstates them by an order of magnitude --
    # enough to evict a resident model to make room that was never needed.
    if kind == "embedding":
        return Estimate(weights_mib, 0, overhead_mib, context, "computed", "high")

    geo = geometry(model_path, architecture) if model_path else None
    if geo is None:
        from .gguf import Geometry, _UNKNOWN_FALLBACK

        geo = Geometry(*_UNKNOWN_FALLBACK, _UNKNOWN_FALLBACK[1] * _UNKNOWN_FALLBACK[2],
                       source="fallback")
    kv_mib = round(context * geo.kv_bytes_per_token() / 1024 / 1024)
    confidence = "high" if geo.source == "gguf" else "low"
    return Estimate(weights_mib, kv_mib, overhead_mib, context, "computed", confidence)


def estimate_for(model: "Model", context: int) -> Estimate:
    """Estimate for a model as reported by LM Studio, resolving its weights first."""
    from . import lmstudio

    return estimate(
        lmstudio.resolve_file(model),
        model.architecture,
        model.size_bytes,
        context,
        model_key=model.key,
        kind=model.kind,
    )


def largest_fitting_context(
    model: "Model",
    available_mib: int,
    *,
    ceiling: int | None = None,
) -> tuple[int, Estimate] | None:
    """Walk the context ladder down until one rung fits. None if even the floor doesn't."""
    limit = min(model.max_context, ceiling) if ceiling else model.max_context
    for context in CONTEXT_STEPS:
        if context > limit or context < CONTEXT_FLOOR:
            continue
        prediction = estimate_for(model, context)
        if prediction.total_mib <= available_mib:
            return context, prediction
    # An embedding model's window is not a memory knob, so the ladder may skip past its
    # tiny maximum entirely. Fall back to asking about its own limit.
    if model.kind == "embedding" or limit < CONTEXT_FLOOR:
        prediction = estimate_for(model, limit)
        if prediction.total_mib <= available_mib:
            return limit, prediction
    return None


def record_measurement(model_key: str, context: int, gpu: str, delta_mib: int) -> None:
    """File a real before/after VRAM delta so future planning stops guessing."""
    data = read_json(CALIBRATION_FILE, default={}) or {}
    entry = data.setdefault(model_key, {"samples": []})
    entry["samples"].append(
        {
            "context": context,
            "gpu": gpu,
            "delta_mib": delta_mib,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    # Keep the history short; old samples describe a machine state that has moved on.
    entry["samples"] = entry["samples"][-12:]
    write_json(CALIBRATION_FILE, data)


def _best_calibration(model_key: str, context: int) -> int | None:
    """Total MiB a load at `context` is expected to take, from measured samples.

    An exact context match wins. Otherwise scale the nearest sample by the context
    ratio, since the KV cache is the only part that grows with context.
    """
    data = read_json(CALIBRATION_FILE, default={}) or {}
    samples = (data.get(model_key) or {}).get("samples") or []
    if not samples:
        return None

    exact = [s for s in samples if s.get("context") == context]
    if exact:
        return round(sum(s["delta_mib"] for s in exact) / len(exact))

    nearest = min(samples, key=lambda s: abs(s.get("context", 0) - context))
    if not nearest.get("context"):
        return None
    # Only extrapolate within a factor of four; beyond that the linear KV assumption
    # stops being a better guess than recomputing from the header.
    ratio = context / nearest["context"]
    if not 0.25 <= ratio <= 4.0:
        return None
    return None if ratio == 1 else round(nearest["delta_mib"] * (0.5 + 0.5 * ratio))

"""Talking to LM Studio: the `lms` CLI for control, the REST API for live state.

Two facts about this integration were established by experiment and shape everything
here:

* **`lms load --estimate-only` is useless as a fit oracle.** It returns exactly the
  model's file size, unchanged whether you ask for 4096 or 131072 tokens of context
  and unchanged between `--gpu off` and `--gpu max`, and it reports `Confidence: LOW`.
  It will happily say an 8.4 GB model "may be loaded" on a 6 GB card. Memory planning
  therefore lives in `vram.py`, computed from GGUF headers and corrected by measurement.
* **The LM Studio GUI being open does not mean the server is running.** The desktop
  app and the OpenAI-compatible server on :1234 start independently, and a stopped
  server looks exactly like a broken model to everything downstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import proc
from .config import LMSTUDIO_BASE

# `lms` installs outside PATH in some setups; look here before giving up.
_EXTRA_DIRS = (
    Path.home() / ".cache" / "lm-studio" / "bin",
    Path.home() / ".lmstudio" / "bin",
)

# Models live under several roots: downloads in `models/`, and the embedding model LM
# Studio ships with under `.internal/bundled-models/`.
MODEL_ROOTS = (
    Path.home() / ".cache" / "lm-studio" / "models",
    Path.home() / ".lmstudio" / "models",
    Path.home() / ".cache" / "lm-studio" / ".internal" / "bundled-models",
)

LOAD_TIMEOUT = 300.0
UNLOAD_TIMEOUT = 60.0


@dataclass(frozen=True)
class Model:
    """A model on disk, plus its live state when it happens to be loaded."""

    key: str
    kind: str  # "llm" | "embedding" | ...
    size_bytes: int
    max_context: int
    architecture: str
    quantization: str
    path: str
    tool_use: bool
    vision: bool
    display_name: str
    # Populated only for loaded models.
    identifier: str | None = None
    loaded_context: int | None = None
    status: str | None = None
    queued: int = 0

    @property
    def loaded(self) -> bool:
        return self.identifier is not None

    @property
    def size_mib(self) -> int:
        return round(self.size_bytes / 1024 / 1024)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Model":
        # Records are not uniform: embedding models carry no `trainedForToolUse`,
        # `vision`, or `paramsString`, and only loaded ones carry `identifier`.
        quant = raw.get("quantization") or {}
        return cls(
            key=raw.get("modelKey", "?"),
            kind=raw.get("type", "unknown"),
            size_bytes=int(raw.get("sizeBytes") or 0),
            max_context=int(raw.get("maxContextLength") or 0),
            architecture=raw.get("architecture", "unknown"),
            quantization=quant.get("name", "?") if isinstance(quant, dict) else str(quant),
            path=raw.get("path", ""),
            tool_use=bool(raw.get("trainedForToolUse", False)),
            vision=bool(raw.get("vision", False)),
            display_name=raw.get("displayName") or raw.get("modelKey", "?"),
            identifier=raw.get("identifier"),
            loaded_context=raw.get("contextLength"),
            status=raw.get("status"),
            queued=int(raw.get("queued") or 0),
        )


def _lms(args: list[str], *, timeout: float = 30.0) -> proc.Result:
    return proc.run(["lms", *args], timeout=timeout, extra_dirs=_EXTRA_DIRS)


def available() -> bool:
    try:
        proc.resolve("lms", _EXTRA_DIRS)
    except proc.ToolNotFound:
        return False
    return True


def list_models() -> list[Model]:
    """Every model on disk. Works with the server stopped."""
    result = _lms(["ls", "--json"]).check()
    return [Model.from_json(entry) for entry in json.loads(result.stdout or "[]")]


def loaded_models() -> list[Model]:
    """Models currently resident in memory."""
    result = _lms(["ps", "--json"]).check()
    return [Model.from_json(entry) for entry in json.loads(result.stdout or "[]")]


def server_running() -> bool:
    result = _lms(["server", "status", "--json"])
    if not result.ok:
        return False
    try:
        return bool(json.loads(result.stdout).get("running"))
    except (json.JSONDecodeError, AttributeError):
        return False


def start_server(port: int = 1234) -> None:
    _lms(["server", "start", "-p", str(port)], timeout=60.0).check()


def load(
    key: str,
    *,
    identifier: str,
    context_length: int,
    gpu: str = "max",
    ttl_seconds: int | None = None,
) -> proc.Result:
    """Load a model under a stable identifier.

    The identifier matters beyond convenience: it is also the `model_name` LiteLLM
    routes to, so keeping the two in sync is what lets a model swap happen without
    touching the proxy at all.

    `-y` is always passed. Loading a model that has multiple downloaded variants
    otherwise prompts for a choice and would hang forever with stdin closed.
    """
    args = [
        "load", key,
        "--identifier", identifier,
        "--gpu", gpu,
        "-c", str(context_length),
        "-y",
    ]
    if ttl_seconds:
        args += ["--ttl", str(ttl_seconds)]
    return _lms(args, timeout=LOAD_TIMEOUT).check()


def unload(identifier: str) -> proc.Result:
    """Unload one model by identifier.

    Never call `lms unload` bare and never pass `--all` implicitly: with a number of
    models loaded other than one, a bare unload drops into an interactive picker.
    """
    if not identifier:
        raise ValueError("refusing to unload without an explicit identifier")
    return _lms(["unload", identifier], timeout=UNLOAD_TIMEOUT).check()


def _normalise(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def resolve_file(model: Model) -> Path | None:
    """Locate a model's weights on disk.

    `lms ls --json` reports `path` inconsistently: for a plain download it is the
    relative path to the .gguf, but for a model with multiple quantized variants it is
    just the model key again, and bundled models live under a different root entirely.
    Guessing wrong is not harmless -- it silently drops memory planning back to a
    coarse per-architecture table, which over-estimates badly enough to evict a
    resident model for no reason.

    So: try the literal path under each root, then fall back to matching .gguf files
    by name.
    """
    for root in MODEL_ROOTS:
        candidate = root / model.path
        if candidate.is_file():
            return candidate

    wanted = _normalise(model.key.split("/")[-1])
    # A variant string like "qwen/qwen2.5-coder-14b@q4_k_m" names the exact quant.
    variant = getattr(model, "variant", None)
    quant_hint = _normalise(model.quantization) if model.quantization != "?" else ""

    best: tuple[int, Path] | None = None
    for root in MODEL_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.gguf"):
            name = _normalise(path.stem)
            if wanted not in name:
                continue
            # Prefer a file whose name also carries the right quantization, so a model
            # with several downloaded variants resolves to the one actually in use.
            score = 2 if quant_hint and quant_hint in name else 1
            if best is None or score > best[0]:
                best = (score, path)
    return best[1] if best else None


def rest_models(timeout: float = 5.0) -> list[dict[str, Any]]:
    """LM Studio's own richer model list, which the OpenAI-compatible route lacks.

    Adds `state` (loaded / not-loaded), `loaded_context_length`, and `capabilities`
    (notably `tool_use`). Returns an empty list if the server is down -- callers
    should treat this as an enhancement over `list_models()`, never a dependency.
    """
    try:
        response = httpx.get(f"{LMSTUDIO_BASE}/api/v0/models", timeout=timeout)
        response.raise_for_status()
        return response.json().get("data", [])
    except (httpx.HTTPError, ValueError):
        return []

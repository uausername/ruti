"""Read just enough of a GGUF header to size a model's KV cache.

Neither `lms ls --json` nor `lms load --estimate-only` reports layer counts or head
geometry, and the KV cache is what actually decides whether a model fits: for an 8B
model it costs roughly 128 KiB per token of context, so the difference between a 4k
and a 32k window is several gigabytes -- far more than the weights differ between
quantizations.

GGUF files describe themselves in a key-value block at the very start, so this reads
a few kilobytes and stops. Parsing failures are never fatal: `kv_bytes_per_token()`
falls back to a per-architecture table, because refusing to plan is worse than
planning approximately.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_MAGIC = b"GGUF"

# GGUF value type tags.
(_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32, _FLOAT32,
 _BOOL, _STRING, _ARRAY, _UINT64, _INT64, _FLOAT64) = range(13)

_SCALARS: dict[int, tuple[str, int]] = {
    _UINT8: ("<B", 1), _INT8: ("<b", 1),
    _UINT16: ("<H", 2), _INT16: ("<h", 2),
    _UINT32: ("<I", 4), _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4), _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8), _INT64: ("<q", 8),
    _FLOAT64: ("<d", 8),
}

# Fallback KV geometry when the header cannot be read, keyed by the architecture
# string LM Studio reports. Values are (block_count, kv_heads, head_dim).
_ARCH_FALLBACK: dict[str, tuple[int, int, int]] = {
    "llama": (32, 8, 128),
    "qwen2": (36, 4, 128),
    "qwen3": (36, 8, 128),
    "gemma2": (26, 4, 256),
    "phi3": (32, 32, 96),
    "mistral": (32, 8, 128),
    "nomic-bert": (12, 12, 64),
    "bert": (12, 12, 64),
}

# Used only when the weights cannot be found or parsed. Modelled on a contemporary
# grouped-query 7-8B, which is the shape most models being run locally have.
#
# The temptation is to pick something pessimistic so a load never spills. That is the
# wrong trade: an over-estimate makes the planner evict a resident model to make room
# that was never needed, which is a real and immediate harm, while an under-estimate is
# caught after the fact by comparing measured VRAM against the prediction. Estimates
# built on this carry `confidence="low"` so callers can weigh them accordingly.
_UNKNOWN_FALLBACK = (32, 8, 128)


@dataclass(frozen=True)
class Geometry:
    block_count: int
    kv_heads: int
    head_dim: int
    embedding_length: int
    source: str  # "gguf" | "fallback"

    def kv_bytes_per_token(self, element_bytes: int = 2) -> int:
        """Bytes of KV cache one token occupies. Both K and V, hence the factor of 2."""
        return 2 * self.block_count * self.kv_heads * self.head_dim * element_bytes


def _read_scalar(fh: BinaryIO, type_id: int) -> Any:
    if type_id == _STRING:
        (length,) = struct.unpack("<Q", fh.read(8))
        return fh.read(length).decode("utf-8", errors="replace")
    fmt, size = _SCALARS[type_id]
    return struct.unpack(fmt, fh.read(size))[0]


def _read_value(fh: BinaryIO, type_id: int) -> Any:
    if type_id != _ARRAY:
        return _read_scalar(fh, type_id)
    elem_type, count = struct.unpack("<IQ", fh.read(12))
    # Arrays here are token vocabularies of hundreds of thousands of entries. We never
    # need one, so skip cheaply where the layout allows and bail out where it doesn't.
    if elem_type in _SCALARS:
        fh.seek(_SCALARS[elem_type][1] * count, 1)
        return None
    if elem_type == _STRING:
        for _ in range(count):
            (length,) = struct.unpack("<Q", fh.read(8))
            fh.seek(length, 1)
        return None
    raise ValueError(f"unsupported GGUF array element type {elem_type}")


def read_metadata(path: Path, *, max_keys: int = 4096) -> dict[str, Any]:
    """Parse the GGUF key-value block. Raises on anything that isn't a GGUF file."""
    with path.open("rb") as fh:
        magic = fh.read(4)
        if magic != _MAGIC:
            raise ValueError(f"{path.name} is not a GGUF file (magic {magic!r})")
        version, _tensor_count, kv_count = struct.unpack("<IQQ", fh.read(20))
        if version not in (2, 3):
            raise ValueError(f"unsupported GGUF version {version}")

        metadata: dict[str, Any] = {}
        for _ in range(min(kv_count, max_keys)):
            (key_len,) = struct.unpack("<Q", fh.read(8))
            key = fh.read(key_len).decode("utf-8", errors="replace")
            (type_id,) = struct.unpack("<I", fh.read(4))
            value = _read_value(fh, type_id)
            if value is not None:
                metadata[key] = value
        return metadata


def geometry(path: Path, architecture: str) -> Geometry:
    """Attention geometry for a model file, falling back to a table on any failure."""
    try:
        meta = read_metadata(path)
        arch = meta.get("general.architecture", architecture)
        blocks = int(meta[f"{arch}.block_count"])
        heads = int(meta[f"{arch}.attention.head_count"])
        embedding = int(meta[f"{arch}.embedding_length"])
        # Multi-query and grouped-query models publish a smaller KV head count; models
        # without it use one KV head per attention head.
        kv_heads = int(meta.get(f"{arch}.attention.head_count_kv", heads))
        head_dim = int(meta.get(f"{arch}.attention.key_length", embedding // heads))
        return Geometry(blocks, kv_heads, head_dim, embedding, source="gguf")
    except (OSError, KeyError, ValueError, struct.error):
        blocks, kv_heads, head_dim = _ARCH_FALLBACK.get(architecture, _UNKNOWN_FALLBACK)
        return Geometry(blocks, kv_heads, head_dim, kv_heads * head_dim, source="fallback")

"""Independent running markers: finishing one job never clears another."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .config import read_json, write_json


def _path(marker: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,64}", run_id):
        raise ValueError("invalid run ID")
    return marker.with_suffix(".d") / f"{run_id}.json"


def active(marker: Path) -> list[dict[str, Any]]:
    found = []
    seen = set()
    for path in [*marker.with_suffix(".d").glob("*.json"), marker]:
        try:
            entry = read_json(path, default=None)
            if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                continue
            if not isinstance(entry.get("expires_at"), (int, float)):
                continue
            if entry["expires_at"] <= time.time():
                continue
            identity = entry.get("run_id")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            found.append(entry)
        except (OSError, TypeError):
            continue
    return sorted(found, key=lambda item: item.get("started_at", 0)
                  if isinstance(item.get("started_at"), (int, float)) else 0, reverse=True)


def start(marker: Path, model: str, timeout: float, run_id: str,
          session_id: str | None = None) -> None:
    now = time.time()
    write_json(_path(marker, run_id), {
        "run_id": run_id, "model": model, "session": session_id,
        "started_at": now, "expires_at": now + timeout,
    })


def finish(marker: Path, run_id: str) -> None:
    _path(marker, run_id).unlink(missing_ok=True)

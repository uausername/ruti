"""A project's own rule for the cheapest model that may hold the seat.

The classifier reads the prompt and the assistant's message before it, and knows nothing
of a project's rules: that a change under `supabase/migrations/` is Opus work is in that
project's CLAUDE.md, not in anything the classifier is shown. So a project can state it
once, in `<project>/.ruti/seat-floor.json`, and `advise` treats a model below the floor as
blocked -- the way it already treats `security` and `review` work -- whatever the task
was classified as:

    {"floors": [
      {"model": "opus", "why": "migrations, RLS and reducer parity are Opus work",
       "match": ["supabase/migrations", "\\bRLS\\b", "EventReducer"]}
    ]}

`match` entries are regular expressions, tried case-insensitively against the prompt and
the assistant's previous message together (a reply of "go ahead" is judged by what it
agrees to). The highest floor that matches wins. A floor only raises the seat; it never
asks for a cheaper one, and a file that cannot be read or parsed changes nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE = Path(".ruti") / "seat-floor.json"

# Cheapest first. Fable is priced off Opus, so it counts at least as high.
RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}


@dataclass(frozen=True)
class Floor:
    model: str
    why: str

    def allows(self, model: str) -> bool:
        return RANK.get(model, 0) >= RANK[self.model]


def load(project: Path | str | None) -> list[dict[str, Any]]:
    """The project's floor rules, or none."""
    if not project:
        return []
    try:
        data = json.loads((Path(project) / FILE).read_text(encoding="utf-8"))
        rules = data.get("floors") if isinstance(data, dict) else None
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
    except (OSError, ValueError):
        return []


def check(project: Path | str | None, text: str) -> Floor | None:
    """The highest floor whose `match` hits `text`, or None."""
    best: Floor | None = None
    for rule in load(project):
        model = rule.get("model")
        if model not in RANK:
            continue
        patterns = rule.get("match")
        if not isinstance(patterns, list):
            continue
        for pattern in patterns:
            try:
                hit = isinstance(pattern, str) and re.search(pattern, text, re.IGNORECASE)
            except re.error:
                continue
            if hit:
                floor = Floor(model, str(rule.get("why") or f"project rule: {pattern}"))
                if best is None or RANK[floor.model] > RANK[best.model]:
                    best = floor
                break
    return best


def for_prompt(session_id: str | None, prompt: str, context: str | None = None,
               project: str | None = None) -> Floor | None:
    """The floor for this prompt, in the project the session runs in. Never raises."""
    try:
        from . import manager

        project = project or manager.session_cwd(session_id)
        if not project:
            return None
        return check(project, f"{prompt or ''}\n{context or ''}")
    except Exception:
        return None

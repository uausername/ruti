"""Console output.

Two audiences read this: a person at a terminal, and Claude parsing `--json`. The
rule is that nothing decorative ever reaches stdout in JSON mode -- a stray progress
line would break the parse and cost a turn to recover from.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.theme import Theme

__all__ = [
    "console", "emit_json", "say", "ok", "warn", "bad", "heading",
    "table", "mask", "human_mib", "literal",
]


def literal(text: str) -> str:
    """Neutralise rich markup in text that came from a tool, a path, or a user.

    Windows paths end in backslashes often enough that this matters: a trailing `\\`
    escapes the next `[`, which silently swallows the closing style tag and leaks
    `[/muted]` into the output.
    """
    return escape(text)

_THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "bad": "bold red",
        "muted": "dim",
        "head": "bold cyan",
    }
)

# Diagnostics go to stderr so `ruti route --json | jq` keeps working.
console = Console(theme=_THEME, stderr=True, no_color=bool(os.environ.get("NO_COLOR")))


def emit_json(payload: Any) -> None:
    """The machine-readable result. Always stdout, always the only thing there."""
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def say(message: str) -> None:
    console.print(message)


def ok(message: str) -> None:
    console.print(f"[ok]OK[/ok]  {message}")


def warn(message: str) -> None:
    console.print(f"[warn]!![/warn]  {message}")


def bad(message: str) -> None:
    console.print(f"[bad]XX[/bad]  {message}")


def heading(message: str) -> None:
    console.print(f"\n[head]{message}[/head]")


def table(*columns: str) -> Table:
    built = Table(show_edge=False, box=None, pad_edge=False, header_style="head")
    for column in columns:
        built.add_column(column, overflow="fold")
    return built


def mask(secret: str, keep: int = 4) -> str:
    """Render a credential safely for display."""
    if not secret:
        return "(empty)"
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:6]}…{secret[-keep:]}"


def human_mib(mib: int | None) -> str:
    if mib is None:
        return "?"
    if mib >= 1024:
        return f"{mib / 1024:.1f}G"
    return f"{mib}M"

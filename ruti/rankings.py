"""What programmers are using right now, per language, from OpenRouter's own traffic.

Model standings change weekly. A shortlist written last month cannot know that the model
most TypeScript is being written with today did not exist then, and a fixed bonus for
"coding" models cannot tell a leader from an also-ran. OpenRouter publishes the answer:
for each of 17 programming languages, how many tokens each model served per day, from a
classified sample of all prompts. This module reads it, and ruti uses it two ways --
coding mode nudges the executors already registered by their current share of the
project's language (`router.rank`), and `ruti openrouter suggest` lists the leaders not
registered yet. Registering one stays the user's decision.

The source is `/api/frontend/v1/rankings/programming-language?tag=<Language>`, the
endpoint the rankings page itself calls: undocumented, so it can change or vanish
without notice. Everything here fails open -- no data means every score stays exactly
what it was without this module. Verified 2026-09-28: 30 days, oldest first, each day
the top nine models by tokens plus an "Others" bucket, keyed by dated permaslugs
(`deepseek/deepseek-v4.1-flash-20260910`) that the public catalogue carries as
`canonical_slug`.

Three decisions, each the user's:

* **The last three days.** "Current" has to mean recent -- a model launched on Monday
  is at the top by Wednesday -- but one day is too noisy to reorder executors on.
* **The exact endpoint only.** A `:free` alias does not inherit the share its paid
  sibling earned: same weights, but a different endpoint with its own limits and
  uptime, and the ranking lists them separately.
* **Up to x1.30.** The language's leader gets it, the rest in proportion to their share
  of the leader's. Unranked is x1.0, not a penalty: only nine models a day are named.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from . import openrouter
from .config import STATE_ROOT, read_json, write_json

RANKINGS_URL = "https://openrouter.ai/api/frontend/v1/rankings/programming-language"
# {"<Language>": {"at": <epoch>, "days": [...], "failed_at": <epoch, optional>}}
RANKINGS_CACHE = STATE_ROOT / "openrouter-rankings.json"
# {"<Language>": <epoch of the last prompt-hook hint>}
HINTS_FILE = STATE_ROOT / "rankings-hints.json"

# The data is daily; twice a day is fresh enough and costs nothing.
RANKINGS_TTL_SECONDS = 12 * 3600
# After a failed fetch, how long to trust the stale cache (or nothing) before asking
# again. Without it a dead network costs every `route` call the full timeout.
FAILURE_BACKOFF_SECONDS = 30 * 60
HINT_INTERVAL_SECONDS = 24 * 3600
WINDOW_DAYS = 3
MAX_FACTOR = 1.30

# A suggestion the prompt hook names unprompted must be worth an interruption: a real
# share of the language, and well clear of the best model already registered.
HINT_MIN_PERCENT = 5.0
HINT_LEAD = 1.5

# OpenRouter's `ProgrammingLanguageTag`, in its own order. The tag is case-sensitive:
# anything else answers 400.
LANGUAGES: tuple[str, ...] = (
    "Python", "JavaScript", "TypeScript", "Java", "Ruby", "C", "C++", "C#", "Go", "Rust",
    "SQL", "Perl", "Visual Basic", "Fortran", "MATLAB", "Swift", "Lua",
)

_ALIASES: dict[str, str] = {
    "py": "Python", "js": "JavaScript", "node": "JavaScript", "ts": "TypeScript",
    "cpp": "C++", "cxx": "C++", "cs": "C#", "csharp": "C#", "golang": "Go", "rs": "Rust",
    "rb": "Ruby", "vb": "Visual Basic", "vbnet": "Visual Basic",
}

_EXTENSIONS: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".java": "Java", ".rb": "Ruby", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".hxx": "C++",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".sql": "SQL", ".pl": "Perl", ".pm": "Perl",
    ".vb": "Visual Basic", ".bas": "Visual Basic",
    ".f": "Fortran", ".f90": "Fortran", ".f95": "Fortran", ".f03": "Fortran", ".for": "Fortran",
    ".m": "MATLAB", ".swift": "Swift", ".lua": "Lua",
}

# In the project root only: a manifest says what the project *is*, where a stray
# script in a subdirectory does not. Worth ten source files.
_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "Python", "setup.py": "Python", "requirements.txt": "Python",
    "tsconfig.json": "TypeScript", "package.json": "JavaScript", "Cargo.toml": "Rust",
    "go.mod": "Go", "Gemfile": "Ruby", "pom.xml": "Java", "build.gradle": "Java",
    "build.gradle.kts": "Java", "Package.swift": "Swift",
}
_MANIFEST_WEIGHT = 10

_PRUNED = frozenset({
    "node_modules", "venv", "env", "__pycache__", "dist", "build", "target", "out",
    "vendor", "site-packages", "coverage",
})
_MAX_FILES = 5000
_MAX_DEPTH = 8


def normalize_language(name: str | None) -> str | None:
    """OpenRouter's tag for a language name, ignoring case; common short forms too."""
    if not name:
        return None
    wanted = name.strip().lower()
    for tag in LANGUAGES:
        if tag.lower() == wanted:
            return tag
    return _ALIASES.get(wanted)


# ------------------------------------------------------------------------ fetching


def fetch_days(language: str, *, force: bool = False, cache_only: bool = False,
               timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """The language's daily token counts, cached twelve hours. Never raises.

    `cache_only` is for the prompt hook, which must not wait on the network: whatever is
    cached, however old, or None. A failed fetch answers with the stale cache if there
    is one -- yesterday's ranking beats none -- and is not retried for half an hour.
    """
    if language not in LANGUAGES:
        return None
    cache = read_json(RANKINGS_CACHE, default=None)
    cache = cache if isinstance(cache, dict) else {}
    entry = cache.get(language)
    entry = entry if isinstance(entry, dict) else {}
    days_cached = entry.get("days")
    cached: list[dict[str, Any]] | None = days_cached if isinstance(days_cached, list) else None
    now = time.time()

    if cache_only:
        return cached
    if not force:
        if cached is not None and now - _number(entry.get("at")) < RANKINGS_TTL_SECONDS:
            return cached
        if now - _number(entry.get("failed_at")) < FAILURE_BACKOFF_SECONDS:
            return cached

    request = urllib.request.Request(
        f"{RANKINGS_URL}?{urlencode({'tag': language})}", headers={"User-Agent": "ruti"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            days = _valid_days(json.loads(response.read().decode("utf-8")))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        days = None

    if days is None:
        cache[language] = {**entry, "failed_at": now}
    else:
        cache[language] = {"at": now, "days": days}
    try:
        write_json(RANKINGS_CACHE, cache)
    except OSError:
        pass
    return days if days is not None else cached


def _valid_days(payload: Any) -> list[dict[str, Any]] | None:
    """The days that have the expected shape, or None when none do.

    Anything unexpected is dropped rather than trusted: this is a frontend endpoint, and
    a changed shape must read as "no data", not as a ranking made of garbage.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    days: list[dict[str, Any]] = []
    for day in data:
        if not isinstance(day, dict) or not isinstance(day.get("x"), str):
            continue
        ys = day.get("ys")
        if not isinstance(ys, dict):
            continue
        tokens = {key: float(value) for key, value in ys.items()
                  if isinstance(key, str) and _is_number(value) and value >= 0}
        if tokens:
            days.append({"x": day["x"], "ys": tokens})
    return days or None


# ------------------------------------------------------------------ interpreting


@dataclass(frozen=True)
class Share:
    slug: str  # the catalogue id, e.g. `deepseek/deepseek-v4.1-flash` or `x/y:free`
    percent: float  # of all the language's tokens in the window, "Others" included
    rank: int  # 1-based among the named models


@dataclass
class Ranking:
    language: str
    days: list[str]  # the dates the window covers, oldest first
    shares: dict[str, Share]  # by lower-cased slug
    others: float  # percent held by models too small to be named

    @property
    def leader(self) -> float:
        return max((share.percent for share in self.shares.values()), default=0.0)

    def ordered(self) -> list[Share]:
        return sorted(self.shares.values(), key=lambda share: share.rank)

    def share_of(self, litellm_model: str) -> Share | None:
        """This exact endpoint's share. `x:free` and `x` are different entries."""
        return self.shares.get(openrouter.slug_of(litellm_model).lower())

    def factor(self, litellm_model: str) -> float:
        """The score multiplier: MAX_FACTOR for the leader, 1.0 for anything unranked."""
        share = self.share_of(litellm_model)
        if share is None or self.leader <= 0:
            return 1.0
        return round(1 + (MAX_FACTOR - 1) * share.percent / self.leader, 3)


def resolve_slug(key: str, catalog: list[dict[str, Any]]) -> str:
    """The catalogue id a ranking key names.

    Keys are dated permaslugs, optionally with a variant: `minimax/minimax-m3-20260531:free`.
    The catalogue lists one entry per endpoint, several sharing a `canonical_slug` --
    `openai/gpt-6-luna` and `openai/gpt-6-luna:batch` -- so the plain id is preferred,
    and a variant is kept as a variant. A key the catalogue does not know (a stealth
    model gone since) is kept as it is, which still names it for a registered alias.
    """
    base, _, variant = key.partition(":")
    ids = [str(entry["id"]) for entry in catalog
           if entry.get("canonical_slug") == base and entry.get("id")]
    plain = [slug for slug in ids if ":" not in slug]
    if variant:
        exact = [slug for slug in ids if slug.endswith(":" + variant)]
        if exact:
            return exact[0]
        return f"{plain[0]}:{variant}" if plain else key
    if plain:
        return plain[0]
    others = sorted((slug for slug in ids if not slug.endswith(":free")), key=len)
    return others[0] if others else key


def build(language: str, days: list[dict[str, Any]], catalog: list[dict[str, Any]], *,
          window_days: int = WINDOW_DAYS) -> Ranking:
    """Shares over the last `window_days` days, as percentages of everything served."""
    window = sorted(days, key=lambda day: str(day.get("x", "")))[-window_days:]
    tokens: dict[str, float] = {}
    others = total = 0.0
    for day in window:
        for key, value in (day.get("ys") or {}).items():
            if not _is_number(value):
                continue
            total += value
            if key == "Others":
                others += value
            else:
                slug = resolve_slug(key, catalog)
                tokens[slug] = tokens.get(slug, 0.0) + value

    dates = [str(day.get("x")) for day in window]
    if total <= 0:
        return Ranking(language, dates, {}, 0.0)
    ranked = sorted(tokens.items(), key=lambda item: (-item[1], item[0]))
    shares = {
        slug.lower(): Share(slug=slug, percent=100 * count / total, rank=rank)
        for rank, (slug, count) in enumerate(ranked, start=1)
    }
    return Ranking(language, dates, shares, 100 * others / total)


def load(language: str | None, *, cache_only: bool = False,
         catalog: list[dict[str, Any]] | None = None) -> Ranking | None:
    """The current ranking for a language, or None. Never raises -- the router and the
    prompt hook both call it, and neither may fail because OpenRouter changed a page."""
    try:
        tag = normalize_language(language)
        if tag is None:
            return None
        days = fetch_days(tag, cache_only=cache_only)
        if not days:
            return None
        if catalog is None:
            catalog = openrouter.fetch_catalog(timeout=5.0)
        return build(tag, days, catalog)
    except Exception:
        return None


# ----------------------------------------------------------------- the project


def detect_language(root: Path | str) -> str | None:
    """The project's main language, from its files and its root manifests. Never raises.

    Source files count one each, a manifest in the root ten. Dependency, build and
    hidden directories are skipped, and the walk stops after 5000 files or eight levels
    -- enough to see what a project is written in without reading a monorepo. `.d.ts`
    files are declarations shipped with JavaScript packages, not TypeScript written here.
    """
    try:
        root = Path(root)
        counts: dict[str, int] = {}
        for name in os.listdir(root):
            tag = _MANIFESTS.get(name)
            if tag is None and name.endswith((".csproj", ".sln")):
                tag = "C#"
            if tag is not None:
                counts[tag] = counts.get(tag, 0) + _MANIFEST_WEIGHT

        seen = 0
        for directory, subdirs, files in os.walk(root):
            depth = len(Path(directory).relative_to(root).parts)
            subdirs[:] = [] if depth >= _MAX_DEPTH else [
                name for name in subdirs if name not in _PRUNED and not name.startswith(".")]
            for name in files:
                seen += 1
                if seen > _MAX_FILES:
                    break
                if name.lower().endswith(".d.ts"):
                    continue
                tag = _EXTENSIONS.get(os.path.splitext(name)[1].lower())
                if tag is not None:
                    counts[tag] = counts.get(tag, 0) + 1
            if seen > _MAX_FILES:
                break
    except (OSError, ValueError):
        return None
    if not counts:
        return None
    # Ties go to the language listed first, which is also the more common one.
    return max(LANGUAGES, key=lambda tag: (counts.get(tag, 0), -LANGUAGES.index(tag)))


# ------------------------------------------------------------------ suggestions


def suggestions(ranking: Ranking, catalog: list[dict[str, Any]],
                registered_models: Iterable[str], *, free_level: str = "off",
                limit: int = 5) -> list[dict[str, Any]]:
    """Ranked models ruti does not have yet and could actually use, best first.

    Skipped: anything registered (the exact endpoint), anything the catalogue no longer
    lists, and anything that cannot drive `opencode` -- no tool calling, or a window too
    small for its 8k-token system prompt. Free mode `hard` drops paid ones outright;
    `soft` keeps them, marked, so the user is warned before choosing one.
    """
    registered = {openrouter.slug_of(model).lower() for model in registered_models}
    by_id = {str(entry.get("id")).lower(): entry for entry in catalog if entry.get("id")}
    rows: list[dict[str, Any]] = []
    for share in ranking.ordered():
        slug = share.slug
        entry = by_id.get(slug.lower())
        if slug.lower() in registered or entry is None:
            continue
        if "tools" not in (entry.get("supported_parameters") or []):
            continue
        context = openrouter._context(entry)
        if context < openrouter.MIN_CONTEXT:
            continue
        free = openrouter.is_free(slug, entry)
        if free_level == "hard" and not free:
            continue
        pricing = entry.get("pricing") or {}
        rows.append({
            "slug": slug,
            "alias": openrouter.alias_for(slug),
            "name": entry.get("name") or slug,
            "percent": round(share.percent, 2),
            "rank": share.rank,
            "free": free,
            "price_in": _per_million(pricing.get("prompt")),
            "price_out": _per_million(pricing.get("completion")),
            "context_length": context,
            # Zero-priced and temporary, and its prompts are the price: fine for public
            # code, not for anything that is not.
            "stealth": slug.startswith("stealth/"),
            "needs_warning": free_level == "soft" and not free,
            "setup": f"ruti openrouter setup --models {slug}",
        })
        if len(rows) >= limit:
            break
    return rows


def hint(language: str | None, registered_models: list[str], *, free_level: str,
         now: float | None = None) -> str | None:
    """One line for the prompt hook, when a clearly better model is not registered.

    Offline: the rankings and the catalogue come from their caches only. At most once a
    day per language, and only for a model holding at least 5% of the language that
    also leads every registered alias by half again -- a hint that fires on every close
    call is a hint that gets ignored. Never raises.
    """
    try:
        tag = normalize_language(language)
        if tag is None:
            return None
        now = time.time() if now is None else now
        hints = read_json(HINTS_FILE, default=None)
        hints = hints if isinstance(hints, dict) else {}
        if now - _number(hints.get(tag)) < HINT_INTERVAL_SECONDS:
            return None

        cached = read_json(openrouter.CATALOG_CACHE, default=None)
        models = cached.get("models") if isinstance(cached, dict) else None
        catalog = models if isinstance(models, list) else []
        ranking = load(tag, cache_only=True, catalog=catalog)
        if ranking is None:
            return None
        rows = suggestions(ranking, catalog, registered_models, free_level=free_level, limit=1)
        if not rows:
            return None
        best = rows[0]
        ours = max((share.percent for model in registered_models
                    if (share := ranking.share_of(model)) is not None), default=0.0)
        if best["percent"] < HINT_MIN_PERCENT or best["percent"] < HINT_LEAD * ours:
            return None

        try:
            write_json(HINTS_FILE, {**hints, tag: now})
        except OSError:
            pass
        if best["free"]:
            cost = "zero-cost"
        elif best["price_in"] is not None and best["price_out"] is not None:
            cost = f"metered ${best['price_in']:g}/${best['price_out']:g} per M tokens"
        else:
            cost = "metered"
        return (f"ruti rankings: {best['name']} (`{best['slug']}`) holds "
                f"{best['percent']:.1f}% of {tag} tokens on OpenRouter (#{best['rank']}), "
                f"ahead of every registered alias -- {cost}; `ruti openrouter suggest` "
                "lists the leaders, and registering one is the user's call.")
    except Exception:
        return None


def _per_million(price: Any) -> float | None:
    """USD per token as the catalogue gives it (a string) -> USD per million tokens."""
    try:
        return round(float(price) * 1e6, 4)
    except (TypeError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0

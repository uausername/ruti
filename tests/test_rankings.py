"""OpenRouter's per-language rankings: fetching, caching, and what a ranking means.

The slugs and catalogue entries below are real ones, as the endpoint and the catalogue
returned them on 2026-09-28 -- the dated permaslugs are the whole difficulty here, and
made-up names would test a mapping that does not exist.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from ruti import rankings

CATALOG = [
    {"id": "deepseek/deepseek-v4.1-flash", "canonical_slug": "deepseek/deepseek-v4.1-flash-20260910",
     "name": "DeepSeek V4.1 Flash", "context_length": 1_048_576, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
    {"id": "deepseek/deepseek-v4.1-flash:batch",
     "canonical_slug": "deepseek/deepseek-v4.1-flash-20260910", "name": "DeepSeek V4.1 Flash (batch)",
     "context_length": 1_048_576, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
    {"id": "deepseek/deepseek-v4-flash-0731", "canonical_slug": "deepseek/deepseek-v4-flash-20260731",
     "name": "DeepSeek V4 Flash 0731", "context_length": 1_310_720, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.000000021", "completion": "0.00000032"}},
    {"id": "dots-studio/dots-3-note-preview:free",
     "canonical_slug": "dots-studio/dots-3-note-preview-20260813", "name": "dots.3 note (free)",
     "context_length": 131_072, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "minimax/minimax-m3", "canonical_slug": "minimax/minimax-m3-20260531",
     "name": "MiniMax M3", "context_length": 1_048_576, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
    {"id": "stealth/space-bunny-alpha", "canonical_slug": "stealth/space-bunny-alpha",
     "name": "Space Bunny Alpha", "context_length": 262_144, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "z-ai/glm-5.3-flash", "canonical_slug": "z-ai/glm-5.3-flash-20260826",
     "name": "GLM 5.3 Flash", "context_length": 1_048_576, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.0000002", "completion": "0.000001"}},
    {"id": "tiny/no-tools", "canonical_slug": "tiny/no-tools-20260101", "name": "No Tools",
     "context_length": 1_048_576, "supported_parameters": [],
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "tiny/small-window", "canonical_slug": "tiny/small-window-20260101",
     "name": "Small Window", "context_length": 16_000, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0", "completion": "0"}},
]


def ranking_days() -> list[dict]:
    return [
        {"x": "2026-09-22", "ys": {"z-ai/glm-5.3-flash-20260826": 1000.0, "Others": 0.0}},
        {"x": "2026-09-23", "ys": {"deepseek/deepseek-v4.1-flash-20260910": 40.0,
                                   "z-ai/glm-5.3-flash-20260826": 20.0, "Others": 40.0}},
        {"x": "2026-09-24", "ys": {"deepseek/deepseek-v4.1-flash-20260910": 40.0,
                                   "stealth/space-bunny-alpha": 10.0, "Others": 50.0}},
        {"x": "2026-09-25", "ys": {"deepseek/deepseek-v4.1-flash-20260910": 40.0,
                                   "minimax/minimax-m3-20260531:free": 10.0,
                                   "tiny/no-tools-20260101": 10.0,
                                   "tiny/small-window-20260101": 10.0, "Others": 30.0}},
    ]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def served(monkeypatch):
    """Replace the network: `served.payload` is what it answers, `served.calls` counts."""
    class Server:
        payload: object = {"data": ranking_days()}
        error: Exception | None = None
        calls: list[str] = []

    Server.calls = []

    def urlopen(request, timeout=None):
        Server.calls.append(request.full_url)
        if Server.error is not None:
            raise Server.error
        return FakeResponse(json.dumps(Server.payload).encode("utf-8"))

    monkeypatch.setattr(rankings, "RANKINGS_URL", "https://example.test/rankings")
    monkeypatch.setattr(rankings.urllib.request, "urlopen", urlopen)
    return Server


# ------------------------------------------------------------------------ names


@pytest.mark.parametrize("given, tag", [
    ("TypeScript", "TypeScript"), ("typescript", "TypeScript"), ("ts", "TypeScript"),
    ("c#", "C#"), ("csharp", "C#"), ("C++", "C++"), ("cpp", "C++"), ("golang", "Go"),
    ("visual basic", "Visual Basic"), ("matlab", "MATLAB"), (" Python ", "Python"),
])
def test_language_names_are_normalised(given, tag):
    assert rankings.normalize_language(given) == tag


@pytest.mark.parametrize("given", [None, "", "Klingon", "Kotlin"])
def test_unknown_languages_are_none(given):
    assert rankings.normalize_language(given) is None


@pytest.mark.parametrize("key, slug", [
    # The plain endpoint, not its `:batch` twin under the same canonical slug.
    ("deepseek/deepseek-v4.1-flash-20260910", "deepseek/deepseek-v4.1-flash"),
    # A dated catalogue id is still just the id.
    ("deepseek/deepseek-v4-flash-20260731", "deepseek/deepseek-v4-flash-0731"),
    # The only id is already the `:free` one -- not `...:free:free`.
    ("dots-studio/dots-3-note-preview-20260813:free", "dots-studio/dots-3-note-preview:free"),
    # A `:free` variant the catalogue no longer lists keeps its variant.
    ("minimax/minimax-m3-20260531:free", "minimax/minimax-m3:free"),
    ("stealth/space-bunny-alpha", "stealth/space-bunny-alpha"),
    ("stealth/union-alpha", "stealth/union-alpha"),
])
def test_permaslugs_resolve_to_catalogue_ids(key, slug):
    assert rankings.resolve_slug(key, CATALOG) == slug


def test_the_batch_twin_listed_first_is_still_passed_over():
    reordered = [CATALOG[1], CATALOG[0]]
    assert rankings.resolve_slug("deepseek/deepseek-v4.1-flash-20260910", reordered) == \
        "deepseek/deepseek-v4.1-flash"
    assert rankings.resolve_slug("deepseek/deepseek-v4.1-flash-20260910:free", reordered) == \
        "deepseek/deepseek-v4.1-flash:free"


# --------------------------------------------------------------------- the maths


def test_only_the_last_three_days_count_and_others_is_in_the_total():
    ranking = rankings.build("Go", ranking_days(), CATALOG)
    # 2026-09-22, where glm had everything, is outside the window.
    assert ranking.days == ["2026-09-23", "2026-09-24", "2026-09-25"]
    deepseek = ranking.share_of("openrouter/deepseek/deepseek-v4.1-flash")
    assert deepseek is not None and deepseek.rank == 1
    assert deepseek.percent == pytest.approx(40.0)  # 120 of 300 tokens
    assert ranking.others == pytest.approx(40.0)
    assert ranking.share_of("openrouter/z-ai/glm-5.3-flash").percent == pytest.approx(20 / 3)


def test_keys_resolving_to_one_endpoint_are_summed():
    days = [{"x": "2026-09-25", "ys": {"deepseek/deepseek-v4.1-flash-20260910": 30.0,
                                       "deepseek/deepseek-v4.1-flash": 20.0, "Others": 50.0}}]
    ranking = rankings.build("Go", days, CATALOG)
    assert list(ranking.shares) == ["deepseek/deepseek-v4.1-flash"]
    assert ranking.leader == pytest.approx(50.0)


def test_the_factor_runs_from_one_to_the_maximum():
    ranking = rankings.build("Go", ranking_days(), CATALOG)
    assert ranking.factor("openrouter/deepseek/deepseek-v4.1-flash") == 1.3
    # 10 of 300 tokens against the leader's 120: 1 + 0.3 * (10 / 120).
    assert ranking.factor("openrouter/stealth/space-bunny-alpha") == 1.025
    assert ranking.factor("openrouter/some/unranked-model") == 1.0


def test_a_free_variant_and_its_paid_twin_are_different_entries():
    ranking = rankings.build("Go", ranking_days(), CATALOG)
    assert ranking.share_of("openrouter/minimax/minimax-m3:free") is not None
    assert ranking.share_of("openrouter/minimax/minimax-m3") is None
    assert ranking.factor("openrouter/minimax/minimax-m3") == 1.0


def test_an_empty_window_is_an_empty_ranking():
    ranking = rankings.build("Go", [{"x": "2026-09-25", "ys": {"Others": 0.0}}], CATALOG)
    assert ranking.shares == {} and ranking.leader == 0.0
    assert ranking.factor("openrouter/deepseek/deepseek-v4.1-flash") == 1.0


# ------------------------------------------------------------------ the network


def test_a_fetch_is_cached_and_the_cache_is_used_while_fresh(served):
    assert rankings.fetch_days("C#") == served.payload["data"]
    assert served.calls == ["https://example.test/rankings?tag=C%23"]
    assert rankings.fetch_days("C#") == served.payload["data"]
    assert len(served.calls) == 1


def test_a_stale_cache_is_fetched_again(served, monkeypatch):
    rankings.fetch_days("Go")
    real_time = rankings.time.time
    monkeypatch.setattr(rankings.time, "time",
                        lambda: real_time() + rankings.RANKINGS_TTL_SECONDS + 1)
    rankings.fetch_days("Go")
    assert len(served.calls) == 2


def test_a_failure_answers_with_the_stale_cache_and_backs_off(served, monkeypatch):
    rankings.fetch_days("Go")
    served.error = urllib.error.URLError("down")
    real_time = rankings.time.time
    later = real_time() + rankings.RANKINGS_TTL_SECONDS + 1
    monkeypatch.setattr(rankings.time, "time", lambda: later)

    assert rankings.fetch_days("Go") == served.payload["data"]
    assert len(served.calls) == 2
    # Within the backoff the network is not asked again, however stale the cache is.
    assert rankings.fetch_days("Go") == served.payload["data"]
    assert len(served.calls) == 2
    monkeypatch.setattr(rankings.time, "time",
                        lambda: later + rankings.FAILURE_BACKOFF_SECONDS + 1)
    rankings.fetch_days("Go")
    assert len(served.calls) == 3


def test_a_failure_with_nothing_cached_is_none(served):
    served.error = urllib.error.URLError("down")
    assert rankings.fetch_days("Rust") is None


@pytest.mark.parametrize("payload", [
    {"error": {"message": "Invalid value", "code": 400}},
    {"data": []},
    {"data": [{"x": 1, "ys": {}}, {"x": "2026-09-25", "ys": {"a/b": "lots"}}]},
    ["not", "a", "dict"],
])
def test_an_unexpected_shape_is_no_data(served, payload):
    served.payload = payload
    assert rankings.fetch_days("Rust") is None


def test_an_unknown_language_never_reaches_the_network(served):
    assert rankings.fetch_days("Klingon") is None
    assert served.calls == []


def test_cache_only_never_reaches_the_network(served):
    assert rankings.fetch_days("Lua", cache_only=True) is None
    assert served.calls == []
    rankings.fetch_days("Lua")
    assert rankings.fetch_days("Lua", cache_only=True) == served.payload["data"]
    assert len(served.calls) == 1


def test_load_never_raises(served):
    served.error = RuntimeError("something nobody expected")
    assert rankings.load("Python", catalog=CATALOG) is None
    assert rankings.load("Klingon") is None


def test_load_builds_from_the_fetched_days(served):
    ranking = rankings.load("go", catalog=CATALOG)
    assert ranking is not None and ranking.language == "Go"
    assert ranking.ordered()[0].slug == "deepseek/deepseek-v4.1-flash"


# ----------------------------------------------------------------- the project


def write(root, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def test_a_typescript_project_is_typescript(tmp_path):
    write(tmp_path, "tsconfig.json", "package.json", "src/a.ts", "src/b.tsx",
          "eslint.config.js", "types/c.d.ts", "types/d.d.ts", "types/e.d.ts")
    write(tmp_path, *(f"node_modules/pkg/f{i}.js" for i in range(50)))
    assert rankings.detect_language(tmp_path) == "TypeScript"


def test_declaration_files_are_not_typescript_written_here(tmp_path):
    write(tmp_path, "index.js", "lib.js", *(f"types/t{i}.d.ts" for i in range(5)))
    assert rankings.detect_language(tmp_path) == "JavaScript"


def test_a_python_project_is_python(tmp_path):
    write(tmp_path, "pyproject.toml", "ruti/a.py", "ruti/b.py", "web/app.js",
          ".venv/lib/x.js", ".venv/lib/y.js", ".venv/lib/z.js")
    assert rankings.detect_language(tmp_path) == "Python"


def test_a_csharp_solution_is_recognised_by_its_manifest(tmp_path):
    write(tmp_path, "App.sln", "build.sql", "seed.sql")
    assert rankings.detect_language(tmp_path) == "C#"


def test_a_manifest_below_the_root_is_just_a_file(tmp_path):
    write(tmp_path, "main.go", "tools/scripts/pyproject.toml")
    assert rankings.detect_language(tmp_path) == "Go"


def test_nothing_recognisable_is_none(tmp_path):
    write(tmp_path, "README.md", "notes.txt")
    assert rankings.detect_language(tmp_path) is None
    assert rankings.detect_language(tmp_path / "does-not-exist") is None


def test_a_huge_tree_still_answers_from_what_was_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(rankings, "_MAX_FILES", 10)
    write(tmp_path, *(f"src/f{i}.rs" for i in range(30)))
    assert rankings.detect_language(tmp_path) == "Rust"


# ---------------------------------------------------------------- suggestions


def test_suggestions_skip_what_ruti_has_or_cannot_use():
    ranking = rankings.build("Go", ranking_days(), CATALOG)
    rows = rankings.suggestions(ranking, CATALOG,
                                ["openrouter/deepseek/deepseek-v4.1-flash"], limit=10)
    slugs = [row["slug"] for row in rows]
    assert "deepseek/deepseek-v4.1-flash" not in slugs  # registered
    assert "tiny/no-tools" not in slugs and "tiny/small-window" not in slugs
    assert "minimax/minimax-m3:free" not in slugs  # not in the catalogue any more
    assert slugs == ["z-ai/glm-5.3-flash", "stealth/space-bunny-alpha"]


def test_suggestions_carry_price_and_the_free_mode_verdict():
    ranking = rankings.build("Go", ranking_days(), CATALOG)
    soft = {row["slug"]: row for row in
            rankings.suggestions(ranking, CATALOG, [], free_level="soft", limit=10)}
    glm, bunny = soft["z-ai/glm-5.3-flash"], soft["stealth/space-bunny-alpha"]
    assert glm["free"] is False and glm["needs_warning"] is True
    assert (glm["price_in"], glm["price_out"]) == (0.2, 1.0)
    assert bunny["free"] is True and bunny["needs_warning"] is False and bunny["stealth"]
    assert glm["setup"] == "ruti openrouter setup --models z-ai/glm-5.3-flash"

    hard = rankings.suggestions(ranking, CATALOG, [], free_level="hard", limit=10)
    assert all(row["free"] for row in hard)
    assert rankings.suggestions(ranking, CATALOG, [], limit=1)[0]["rank"] == 1


# ------------------------------------------------------------------------ hint


@pytest.fixture
def cached_ranking(served):
    """A Go ranking and the catalogue, both sitting in their caches."""
    rankings.fetch_days("Go")
    rankings.write_json(rankings.openrouter.CATALOG_CACHE, {"at": 0, "models": CATALOG})
    served.calls.clear()
    return served


def test_a_clear_leader_is_hinted_once_a_day(cached_ranking):
    line = rankings.hint("go", ["openrouter/stealth/space-bunny-alpha"], free_level="off",
                         now=1_000_000.0)
    assert line is not None
    assert "DeepSeek V4.1 Flash" in line and "40.0% of Go tokens" in line and "(#1)" in line
    assert "metered $0.3/$1.2 per M tokens" in line
    assert cached_ranking.calls == []  # the hint never touches the network
    assert rankings.hint("Go", [], free_level="off", now=1_000_000.0 + 3600) is None
    assert rankings.hint("Go", [], free_level="off",
                         now=1_000_000.0 + rankings.HINT_INTERVAL_SECONDS + 1) is not None


def test_no_hint_when_the_leader_is_already_registered(cached_ranking):
    # The best left is glm at 6.7%, far behind the registered 40%.
    assert rankings.hint("Go", ["openrouter/deepseek/deepseek-v4.1-flash"],
                         free_level="off", now=1.0) is None


def test_no_hint_when_a_registered_alias_is_close_behind(cached_ranking, monkeypatch):
    # deepseek at 40% against a registered model at 30%: ahead, but not by half again.
    days = [{"x": "2026-09-25", "ys": {"deepseek/deepseek-v4.1-flash-20260910": 40.0,
                                       "z-ai/glm-5.3-flash-20260826": 30.0, "Others": 30.0}}]
    monkeypatch.setattr(rankings, "fetch_days", lambda *_a, **_k: days)
    assert rankings.hint("Go", ["openrouter/z-ai/glm-5.3-flash"], free_level="off",
                         now=1.0) is None


def test_no_hint_under_hard_free_mode_for_a_paid_leader(cached_ranking):
    # With paid ones dropped, the best left is space-bunny at 3.3% -- under 5%.
    assert rankings.hint("Go", [], free_level="hard", now=1.0) is None


def test_no_hint_without_a_cached_ranking(served):
    assert rankings.hint("Go", [], free_level="off", now=1.0) is None
    assert served.calls == []

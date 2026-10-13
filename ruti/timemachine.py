#!/usr/bin/env python3
"""Time machine: letters from a project's future, delivered to the session that needs them.

A project's future mostly repeats its past: the same classes of mistake come back
(parallel sessions duplicating work, two implementations drifting apart, "CI green,
phone red", access decided from the wrong source...), and most of them are guarded
only by prose in CLAUDE.md -- a protocol that depends on the model remembering it,
which is a protocol with a failure rate.

So the lessons live as data in `docs/future-letters.yaml`, each with evidence (the PRs
where it already happened), and this module delivers them mechanically:

    brief       SessionStart: time since the last session + letters for the work in
                the queue + conditions that fire now + ticking bombs + expedition backlog
    warn        PreToolUse: the letter for a file, right before it is edited (once per
                session, never blocks)
    travel      the chronicle of the future: queue, path to the goal, bombs, prose debts
    learn       which letters this branch's diff touches; a fix should feed its letter
    expedition  fixes merged since the last expedition: evidence for known letters,
                candidates for new classes, unguarded hot spots (`--mark` moves the marker)
    backtest    would `warn` have fired on the files of each letter's evidence PRs?
    check       CI: letters are honest -- schema, evidence, live globs, real node ids
    status      where the letters are, which graph is used, whether a vendored copy drifted
    vendor      copy this engine into a project (`scripts/time-machine/tm.py`)

`brief` and `warn` fail open: any error is silence and exit 0. A session that cannot
start, or cannot edit a file, because of an advisor is worse than one without advice.

ONE SOURCE, VENDORED COPIES. The source of truth is `ruti/timemachine.py` in the ruti
repository (`ruti mode timemachine`, `ruti timemachine ...`). A project whose sessions
may run where ruti is not installed (a cloud container) carries a byte-identical copy
at `scripts/time-machine/tm.py`; `ruti timemachine vendor` refreshes it and
`ruti timemachine status` -- and ruti's own SessionStart brief -- say when it drifted.
Edit the engine in ruti, never in a vendored copy. Everything project-specific lives in
the `settings:` block of the letters file, not here. Spec: docs/time-machine.md in a
project that uses it.
"""
from __future__ import annotations

import collections
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ENGINE_VERSION = "2.0"
LETTERS_REL = "docs/future-letters.yaml"
VENDORED_REL = "scripts/time-machine/tm.py"

SEVERITY = {"high": 3, "medium": 2, "low": 1}
CLASSES = {"process", "prod-drift", "device", "parity", "security", "silent-loss",
           "assumption", "docs-lie", "ci", "hotspot", "bomb"}
GUARDS = {"hook", "ci", "test", "prose", "none"}
TOOLS = {"Write", "Edit"}
KEYS_REQUIRED = ("id", "title", "class", "severity", "warning", "precaution",
                 "evidence", "guard")
KEYS_OPTIONAL = ("paths", "tools", "nodes", "while_open", "when", "mechanize")
TOP_KEYS = {"version", "settings", "expedition", "letters"}
SETTINGS_KEYS = {"lang", "tasks", "graph_module", "goal_node", "activity_refs", "main_ref"}
TEXT_LIMIT = 400          # warning/precaution: a letter, not a novel
BRIEF_LETTERS = 5         # letters in the brief, bombs aside
WARN_PER_CALL = 2
PROSE_DEBT = 3            # this much evidence under guard prose|none is a debt
EXPEDITION_DEFAULT_DAYS = 30
EXPEDITION_BRIEF_CAP = 40
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
PR_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+) ")
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|[._]test\.|_spec\.|\.spec\.")
MANIFEST_RE = re.compile(r"\.lock$|(^|/)(pubspec\.yaml|package(-lock)?\.json|pyproject\.toml|"
                         r"requirements[^/]*\.txt|deno\.jsonc?|go\.(mod|sum)|Cargo\.toml)$")
FIX_RE = re.compile(r"^(fix|hotfix)\b|^revert\b|^Revert \"|\bregress", re.IGNORECASE)

# A Windows console defaults to a legacy code page (cp1251) with no room for
# guillemets, arrows or emoji; say UTF-8 out loud instead of crashing the brief.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- strings

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "d": "{d} d {h} h", "h": "{h} h {m} min", "m": "{m} min",
        "brief_time": "⏳ Time machine: {delta} since the last session (last commit {stamp}: “{subject}”). Tell the user.",
        "brief_time_unknown": "⏳ Time machine: time of the last session unknown (no git history).",
        "brief_letters": "Letters from the future — this already happened here and will again:",
        "brief_stale": "⚠ Stale letters (glob matches nothing — file renamed?): {items}. Fix {letters} in the next PR.",
        "brief_bombs": "Ticking bombs (defused when the node closes): {items}",
        "brief_expedition": "🔭 Expedition: {n} fix(es) merged since {date}, {m} with no letter — `{cmd} expedition`, decide each, then `--mark`.",
        "brief_vendored": "⚠ The vendored engine {path} differs from ruti's — `ruti timemachine vendor`.",
        "brief_footer": "More: `{cmd} travel`. Before you edit a file under a letter, the hook will remind you.",
        "for_node": "for node {id}", "daily": "lesson of the day",
        "stale_claim": "{id} (last mark {date})",
        "warn_head": "⏳ Letter from the future for {rel} — this already happened here:",
        "warn_do": "What to do", "warn_ev": "Evidence",
        "travel_title": "⏳ TIME MACHINE — chronicle of the future\n",
        "travel_now": "Now: {now}; {delta} since the last session ({stamp}, “{subject}”).",
        "travel_counts": "Agent front: {front} · claimed: {claimed} · waiting on the owner: {waiting}\n",
        "travel_queue": "── Agent queue: what is coming and what waits there ──",
        "st_taken": "TAKEN", "st_ready": "READY", "st_waits": "waits",
        "travel_star": "  (* — owner's node)\n",
        "travel_goal": "── Path to {goal}: {n} open node(s) ──",
        "travel_fired": "\n── Conditions firing now ──",
        "travel_bombs": "\n── Bombs ──", "ticking": "TICKING", "defused": "defused",
        "travel_stale": "\n⚠ Stale letter {id}: glob `{glob}` matches nothing",
        "travel_debts": "\n── Prose debts: repeated ≥3 times, guarded only by text ──",
        "debt_mech": " → mechanize: {node}", "debt_none": " → no mechanism",
        "learn_head": "Diff against {base}: {files} file(s), letters touched: {n}",
        "learn_fix": "\nThis branch is a fix. If it fixes a recurrence of a class above, add this PR to that letter's evidence in this same diff. A new class gets a new letter. That is how the machine learns.",
        "bt_head": "Backtest: would `warn` have fired on the files of each letter's evidence PRs?",
        "bt_cols": "  {c:>8}  {n:>8}  letter   (noise — fires on the last {k} commits)",
        "bt_caught": "caught", "bt_noise": "noise",
        "bt_recall": "Recall: {c}/{t} = {r:.2f}", "bt_nohist": " (no history — git fetch --unshallow)",
        "exp_head": "🔭 Expedition since {since} → {head}: {n} fix commit(s) of {all} merged",
        "exp_ev": "\nEvidence for known letters (add the PR to `evidence`):",
        "exp_new": "\nNo letter covers these fixes — a new class, or a one-off? Decide each:",
        "exp_hot": "\nHot spots: fixed ≥2 times in this window, no letter on them:",
        "exp_none": "\nNothing new: no fix commit since the marker.",
        "exp_tail": "\nWhen decided: add evidence / letters, then `{cmd} expedition --mark` in the same PR.",
        "exp_marked": "expedition marker → {sha} ({date})",
        "ok": "OK: {n} letter(s), no violations.",
        "e_field": "{w}: missing `{k}`", "e_unknown": "{w}: unknown field(s) {keys}",
        "e_id": "{w}: id must be kebab-case", "e_dup": "{w}: duplicate id",
        "e_enum": "{w}: {k} ∉ {allowed}", "e_long": "{w}: `{k}` longer than {n} characters — a letter, not a novel",
        "e_ev": "{w}: evidence must be a non-empty list (a letter without evidence is a guess)",
        "e_ev_ref": "{w}: evidence {e}: needs an integer `pr` or a `commit`",
        "e_ev_date": "{w}: evidence {e}: needs `date` YYYY-MM-DD",
        "e_trigger": "{w}: no trigger (paths/nodes/while_open/when) — the letter would never arrive",
        "e_stale": "{w}: glob `{g}` matches no file — the letter went stale",
        "e_node": "{w}: {k}: no node `{id}` in the task graph",
        "e_nograph": "{w}: {k} refers to nodes, but the project has no task graph",
        "e_when": "{w}: when `{when}` — unknown condition",
        "e_top": "unknown top-level key(s) {keys}", "e_settings": "settings: unknown key(s) {keys}",
        "e_version": "version: must be 1",
        "w_debt": "{w}: ×{n} under guard {g} — a prose debt; add a node and put it in `mechanize`",
        "w_mech": "{w}: node {id} is closed — update `guard`",
        "w_defused": "{w}: bomb defused ({id} closed) — the letter can go",
        "st_root": "root          : {v}", "st_letters": "letters       : {v}",
        "st_graph": "task graph    : {v}", "st_wired": "project hook  : {v}",
        "st_vendored": "vendored copy : {v}", "st_marker": "expedition    : {v}",
        "same": "byte-identical to this engine", "self": "this file — run `ruti timemachine status` to compare it with ruti", "differs": "DIFFERS from this engine — `ruti timemachine vendor`",
        "absent": "none", "yes_self": "yes — the project wires its own hook; ruti's hooks stay silent here",
        "no_self": "no — ruti's hooks deliver the letters (when `ruti mode timemachine` is on)",
        "v_done": "vendored {src} → {dst}", "v_self": "this is the vendored copy itself; run it from ruti",
    },
    "ru": {
        "d": "{d} д {h} ч", "h": "{h} ч {m} мин", "m": "{m} мин",
        "brief_time": "⏳ Машина времени: с прошлой сессии прошло {delta} (последний коммит {stamp}: «{subject}»). Скажи это пользователю.",
        "brief_time_unknown": "⏳ Машина времени: время прошлой сессии неизвестно (нет git-истории).",
        "brief_letters": "Письма из будущего — это уже случалось здесь и случится снова:",
        "brief_stale": "⚠ Протухли письма (glob ни с чем не совпадает — файл переименовали?): {items}. Почини {letters} в ближайшем PR.",
        "brief_bombs": "Тикают бомбы (обезвредятся, когда закроют узел): {items}",
        "brief_expedition": "🔭 Экспедиция: с {date} влито фиксов: {n}, без письма: {m} — `{cmd} expedition`, реши по каждому, потом `--mark`.",
        "brief_vendored": "⚠ Vendored-движок {path} разошёлся с ruti — `ruti timemachine vendor`.",
        "brief_footer": "Подробно: `{cmd} travel`. Перед правкой файла под письмом хук напомнит сам.",
        "for_node": "к узлу {id}", "daily": "урок дня",
        "stale_claim": "{id} (последняя отметка {date})",
        "warn_head": "⏳ Письмо из будущего для {rel} — это уже случалось здесь:",
        "warn_do": "Что сделать", "warn_ev": "Доказательства",
        "travel_title": "⏳ МАШИНА ВРЕМЕНИ — хроника будущего\n",
        "travel_now": "Сейчас: {now}; с прошлой сессии прошло {delta} ({stamp}, «{subject}»).",
        "travel_counts": "Фронт агента: {front} · взято: {claimed} · ждут владельца: {waiting}\n",
        "travel_queue": "── Очередь агента: что придёт и что там ждёт ──",
        "st_taken": "ВЗЯТ", "st_ready": "ГОТОВ", "st_waits": "ждёт",
        "travel_star": "  (* — узел владельца)\n",
        "travel_goal": "── Путь к {goal}: {n} незакрытых узлов ──",
        "travel_fired": "\n── Сработавшие условия ──",
        "travel_bombs": "\n── Бомбы ──", "ticking": "ТИКАЕТ", "defused": "обезврежена",
        "travel_stale": "\n⚠ Протухло письмо {id}: glob `{glob}` ни с чем не совпадает",
        "travel_debts": "\n── Долги прозы: повторялось ≥3 раз, а защищено только текстом ──",
        "debt_mech": " → механизировать: {node}", "debt_none": " → механизма нет",
        "learn_head": "Дифф против {base}: {files} файлов, писем задето: {n}",
        "learn_fix": "\nВетка — фикс. Если он чинит повторение класса из письма выше — добавь в его evidence этот PR в этом же диффе. Если класс новый — заведи письмо. Так машина учится.",
        "bt_head": "Бэктест: сработал бы warn на файлы PR из доказательств письма?",
        "bt_cols": "  {c:>8}  {n:>8}  письмо   (шум — срабатываний на {k} последних коммитов)",
        "bt_caught": "поймано", "bt_noise": "шум",
        "bt_recall": "Recall: {c}/{t} = {r:.2f}", "bt_nohist": " (нет истории — нужен git fetch --unshallow)",
        "exp_head": "🔭 Экспедиция с {since} → {head}: фикс-коммитов {n} из {all} влитых",
        "exp_ev": "\nДоказательства к известным письмам (добавь PR в `evidence`):",
        "exp_new": "\nНи одно письмо не покрывает эти фиксы — новый класс или разовый случай? Реши по каждому:",
        "exp_hot": "\nГорячие точки: чинились ≥2 раз в этом окне, письма на них нет:",
        "exp_none": "\nНового нет: с отметки ни одного фикса.",
        "exp_tail": "\nРешил — допиши evidence / письма и `{cmd} expedition --mark` в том же PR.",
        "exp_marked": "отметка экспедиции → {sha} ({date})",
        "ok": "OK: {n} писем, нарушений нет.",
        "e_field": "{w}: нет поля `{k}`", "e_unknown": "{w}: неизвестные поля {keys}",
        "e_id": "{w}: id должен быть kebab-case", "e_dup": "{w}: id повторяется",
        "e_enum": "{w}: {k} ∉ {allowed}", "e_long": "{w}: `{k}` длиннее {n} символов — письмо, не роман",
        "e_ev": "{w}: evidence — непустой список (письмо без доказательств — гадание)",
        "e_ev_ref": "{w}: evidence {e}: нужен целый `pr` или `commit`",
        "e_ev_date": "{w}: evidence {e}: нужна `date` YYYY-MM-DD",
        "e_trigger": "{w}: нет триггера (paths/nodes/while_open/when) — письмо не дойдёт",
        "e_stale": "{w}: glob `{g}` не совпадает ни с одним файлом — письмо протухло",
        "e_node": "{w}: {k}: узла `{id}` нет в графе задач",
        "e_nograph": "{w}: {k} ссылается на узлы, а графа задач в проекте нет",
        "e_when": "{w}: when `{when}` — неизвестное условие",
        "e_top": "неизвестные ключи верхнего уровня {keys}", "e_settings": "settings: неизвестные ключи {keys}",
        "e_version": "version: должно быть 1",
        "w_debt": "{w}: ×{n} при guard {g} — долг прозы, заведи узел и впиши его в `mechanize`",
        "w_mech": "{w}: узел {id} закрыт — обнови `guard`",
        "w_defused": "{w}: бомба обезврежена ({id} закрыт) — письмо можно убрать",
        "st_root": "корень        : {v}", "st_letters": "письма        : {v}",
        "st_graph": "граф задач    : {v}", "st_wired": "свой хук      : {v}",
        "st_vendored": "vendored-копия: {v}", "st_marker": "экспедиция    : {v}",
        "same": "побайтово совпадает с этим движком", "self": "это и есть она — сравнить с ruti: `ruti timemachine status`", "differs": "РАСХОДИТСЯ с этим движком — `ruti timemachine vendor`",
        "absent": "нет", "yes_self": "да — проект подключил свой хук; хуки ruti здесь молчат",
        "no_self": "нет — письма доставляют хуки ruti (при `ruti mode timemachine` on)",
        "v_done": "скопировано {src} → {dst}", "v_self": "это и есть vendored-копия; запускай из ruti",
    },
}


def T(key: str, **kw) -> str:
    table = STRINGS.get(project().lang, STRINGS["en"])
    return (table.get(key) or STRINGS["en"][key]).format(**kw)


# ---------------------------------------------------------------- project

def _git_in(root: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, encoding="utf-8", timeout=10).stdout
    except Exception:
        return ""


def _strict_loader():
    """A SafeLoader that refuses a mapping with the same key twice.

    PyYAML silently keeps the last duplicate. In haccp (#448) a node appended without a
    trailing newline glued itself onto the node above, and its keys overwrote that
    node's -- a closed node turned in_progress under a stranger's title, and the graph
    still "validated". A duplicate key in a letters file is always that glue.
    """
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def unique_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue  # `<<: *anchor` is not a duplicate; construct_mapping expands it
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r} — something glued onto the entry "
                    "above it (missing newline?)", key_node.start_mark)
            seen.add(key)
        return loader.construct_mapping(node, deep=deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    return StrictLoader


class Project:
    """Everything that differs between projects: where they live and their settings."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.letters_path = root / LETTERS_REL
        self.doc: dict = {}
        if self.letters_path.exists():
            import yaml
            self.doc = yaml.safe_load(self.letters_path.read_text(encoding="utf-8")) or {}
        settings = self.doc.get("settings") or {}
        self.settings = settings if isinstance(settings, dict) else {}
        self.lang = str(self.settings.get("lang") or "en")
        tasks = self.settings.get("tasks", "docs/tasks.yaml")
        self.tasks_path = (root / tasks) if tasks else None
        gm = self.settings.get("graph_module")
        self.graph_module = (root / gm) if gm else None
        self.goal_node = self.settings.get("goal_node")
        self.activity_refs = list(self.settings.get("activity_refs") or [])
        self.main_ref = self.settings.get("main_ref") or self._first_ref("origin/main", "main", "HEAD")
        self._graph = None

    def _first_ref(self, *refs: str) -> str:
        for ref in refs:
            if _git_in(self.root, "rev-parse", "--verify", "--quiet", ref).strip():
                return ref
        return "HEAD"

    def letters(self) -> list[dict]:
        return [x for x in (self.doc.get("letters") or []) if isinstance(x, dict)]

    def has_letters(self) -> bool:
        return self.letters_path.exists()

    def self_wired(self) -> bool:
        """The project delivers letters through its own hook (a vendored copy)."""
        for name in ("settings.json", "settings.local.json"):
            f = self.root / ".claude" / name
            try:
                if f.exists() and "time-machine" in f.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
        return False

    def has_graph(self) -> bool:
        return bool(self.graph_module and self.graph_module.exists()) or \
            bool(self.tasks_path and self.tasks_path.exists())

    def graph(self) -> tuple[dict, list, list, list]:
        """(by_id, front, waiting, claimed). A project's own validator wins, so the
        front has one definition; otherwise the task-graph schema, read directly."""
        if self._graph is None:
            self._graph = self._load_graph()
        return self._graph

    def _load_graph(self):
        if self.graph_module and self.graph_module.exists():
            spec = importlib.util.spec_from_file_location("tm_graph_module", self.graph_module)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            by_id = mod.load_nodes()
            front, waiting, claimed = mod.ready_front(by_id)
            return by_id, front, waiting, claimed
        if not (self.tasks_path and self.tasks_path.exists()):
            return {}, [], [], []
        import yaml
        doc = yaml.safe_load(self.tasks_path.read_text(encoding="utf-8")) or {}
        by_id = {n["id"]: n for n in (doc.get("nodes") or [])
                 if isinstance(n, dict) and isinstance(n.get("id"), str)}
        return (by_id, *builtin_front(by_id))


def builtin_front(by_id: dict) -> tuple[list, list, list]:
    """docs/task-graph.md semantics: ready = open + agent-owned + every blocker resolved."""
    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    resolved = {i for i, n in by_id.items() if n.get("status") in ("done", "dropped")}
    front = [n for n in by_id.values()
             if n.get("status") == "open" and n.get("owner") == "claude"
             and all(d in resolved for d in n.get("blocked_by") or [])]
    waiting = [n for n in by_id.values() if n.get("status") == "open" and n.get("owner") != "claude"]
    claimed = [n for n in by_id.values() if n.get("status") == "in_progress"]
    key = lambda n: order.get(n.get("priority"), 9)  # noqa: E731
    return sorted(front, key=key), sorted(waiting, key=key), sorted(claimed, key=key)


def resolve_root(explicit: str | None = None) -> pathlib.Path:
    """--root > $TIMEMACHINE_ROOT > the project this copy is vendored into >
    $CLAUDE_PROJECT_DIR > the git checkout around the working directory."""
    if explicit:
        return pathlib.Path(explicit).resolve()
    if os.environ.get("TIMEMACHINE_ROOT"):
        return pathlib.Path(os.environ["TIMEMACHINE_ROOT"]).resolve()
    here = pathlib.Path(__file__).resolve()
    if here.parent.name == "time-machine" and here.parent.parent.name == "scripts":
        return here.parents[2]
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return pathlib.Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()
    top = _git_in(pathlib.Path.cwd(), "rev-parse", "--show-toplevel").strip()
    return pathlib.Path(top).resolve() if top else pathlib.Path.cwd().resolve()


_PROJECT: Project | None = None


def project(root: str | pathlib.Path | None = None) -> Project:
    global _PROJECT
    if root is not None:
        _PROJECT = Project(pathlib.Path(root).resolve())
    elif _PROJECT is None:
        _PROJECT = Project(resolve_root())
    return _PROJECT


def is_vendored_copy() -> bool:
    here = pathlib.Path(__file__).resolve()
    return here.parent.name == "time-machine" and here.parent.parent.name == "scripts"


def command() -> str:
    """How a reader of the brief runs the machine in this project."""
    return f"python3 {VENDORED_REL}" if is_vendored_copy() else "ruti timemachine"


# ---------------------------------------------------------------- compatibility

def load_letters(path: pathlib.Path | None = None) -> list[dict]:
    if path is None:
        return project().letters()
    import yaml
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [x for x in (doc.get("letters") or []) if isinstance(x, dict)]


def load_graph() -> tuple[dict, list, list, list]:
    return project().graph()


def git(*args: str) -> str:
    return _git_in(project().root, *args)


# ---------------------------------------------------------------- globs

_glob_cache: dict[str, re.Pattern] = {}


def glob_re(glob: str) -> re.Pattern:
    """`**/` — any number of directories, `*` — within one, `dir/` — everything under it."""
    if glob in _glob_cache:
        return _glob_cache[glob]
    g = glob + "**" if glob.endswith("/") else glob
    out, i = [], 0
    while i < len(g):
        if g.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif g.startswith("**", i):
            out.append(".*"); i += 2
        elif g[i] == "*":
            out.append("[^/]*"); i += 1
        elif g[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(g[i])); i += 1
    rx = re.compile("".join(out) + r"\Z")
    _glob_cache[glob] = rx
    return rx


def path_matches(rel: str, globs: list[str]) -> bool:
    return any(glob_re(g).match(rel) for g in globs or [])


def touches_overlap(touch: str, glob: str) -> bool:
    """Does a node's `touches` (prefix or file) fall inside a letter's glob?

    Only node-inside-glob counts (`app/lib/ui/chat/` ⊂ `app/lib/ui/**`). The reverse --
    "the node touches the directory a letter's file lives in" -- would make a broad
    `touches` like `.github/workflows/` pick up a letter for every file in it; the
    moment of editing such a file is what `warn` is for.
    """
    if glob_re(glob).match(touch):
        return True
    cut = min([glob.find(c) for c in "*?[" if c in glob] or [len(glob)])
    return cut < len(glob) and touch.startswith(glob[:cut])


def letter_tools(letter: dict) -> list[str]:
    return letter.get("tools") or ["Write", "Edit"]


# ---------------------------------------------------------------- git facts

def last_activity() -> tuple[int, str, str] | None:
    """The newest commit among the checkout, main and the activity refs:
    (epoch, date with offset, subject)."""
    best = None
    refs = ["HEAD", "main", "origin/main", *project().activity_refs]
    for ref in dict.fromkeys(refs):
        line = git("log", "-1", "--format=%ct|%ci|%s", ref).strip()
        if not line:
            continue
        ct, ci, subject = line.split("|", 2)
        if best is None or int(ct) > best[0]:
            best = (int(ct), f"{ci[:16]} {ci[20:]}".strip(), subject)
    return best


def human_delta(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return T("d", d=d, h=h)
    if h:
        return T("h", h=h, m=m)
    return T("m", m=m)


def pr_of(subject: str) -> int | None:
    m = PR_RE.search(subject)
    return int(m.group(1) or m.group(2)) if m else None


def pr_commit(pr: int) -> str | None:
    """A PR's commit on main: squash (`… (#N)`) or merge (`Merge pull request #N …`)."""
    out = git("log", project().main_ref, "--format=%H %s")
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if pr_of(subject) == pr:
            return sha
    return None


def commit_files(sha: str) -> list[str]:
    """Files of a commit against its first parent — so a merge commit is not empty."""
    out = git("diff", "--name-only", f"{sha}^1", sha) or git("show", "--name-only", "--format=", sha)
    return [f for f in out.splitlines() if f]


def _bookkeeping(files: list[str]) -> list[str]:
    """Files minus the graph and the letters: a PR that only closes a node or adds
    evidence says nothing about where code breaks."""
    p = project()
    skip = {LETTERS_REL}
    if p.tasks_path:
        try:
            skip.add(p.tasks_path.relative_to(p.root).as_posix())
        except ValueError:
            pass
    return [f for f in files if f not in skip]


# ---------------------------------------------------------------- conditions

def _node_last_date(node: dict, today: dt.date) -> dt.date | None:
    """The newest date in `notes`. Future dates ("close by 2026-11-01") and impossible
    ones (2026-02-30) do not count — they would hide an old claim."""
    dates = []
    for x in DATE_RE.findall(str(node.get("notes") or "")):
        try:
            d = dt.date.fromisoformat(x)
        except ValueError:
            continue
        if d <= today:
            dates.append(d)
    return max(dates, default=None)


def stale_claims(claimed: list[dict], today: dt.date) -> list[str]:
    out = []
    for n in claimed:
        last = _node_last_date(n, today)
        if last and (today - last).days >= 1:
            out.append(T("stale_claim", id=n["id"], date=last.isoformat()))
    return out


def condition(when: str, ctx: dict) -> str | None:
    """A detail string when the letter's condition holds now, else None."""
    if when == "always":
        return ""
    if when == "claim-older-than-24h":
        stale = stale_claims(ctx["claimed"], ctx["today"])
        return "; ".join(stale) if stale else None
    if when.startswith("node-active:"):
        node = ctx["by_id"].get(when.split(":", 1)[1])
        return "" if node and node.get("status") == "in_progress" else None
    return None


def known_condition(when: str, by_id: dict) -> bool:
    if when in ("always", "claim-older-than-24h"):
        return True
    return when.startswith("node-active:") and when.split(":", 1)[1] in by_id


def ticking(letter: dict, by_id: dict) -> bool:
    node = by_id.get(letter.get("while_open") or "")
    return bool(node) and node.get("status") in ("open", "in_progress")


# ---------------------------------------------------------------- formatting

def evidence_refs(letter: dict) -> str:
    refs = []
    for e in letter.get("evidence") or []:
        if "pr" in e:
            refs.append(f"#{e['pr']}")
        elif "commit" in e:
            refs.append(str(e["commit"])[:9])
    return ", ".join(refs)


def evidence_prs(letter: dict) -> set[int]:
    return {e["pr"] for e in letter.get("evidence") or [] if isinstance(e.get("pr"), int)}


def latest_evidence(letter: dict) -> str:
    return max((str(e.get("date", "")) for e in letter.get("evidence") or []), default="")


def one_line(letter: dict) -> str:
    return (f"[{letter['severity']}] {letter['id']}: {letter['warning'].strip()} "
            f"→ {letter['precaution'].strip()} ({evidence_refs(letter)})")


def letters_for_node(node: dict, letters: list[dict]) -> list[dict]:
    touches = node.get("touches") or []
    return [x for x in letters
            if node["id"] in (x.get("nodes") or [])
            or any(touches_overlap(t, g) for t in touches for g in x.get("paths") or [])]


# ---------------------------------------------------------------- expedition

def expedition_marker() -> dict:
    m = project().doc.get("expedition") or {}
    return m if isinstance(m, dict) else {}


def expedition(letters: list[dict], since: str | None = None, cap: int | None = None) -> dict:
    """Fix commits merged since the marker, sorted into: evidence for a known letter,
    candidates for a new class, and files fixed twice with no letter on them."""
    p = project()
    head = git("rev-parse", "--short", p.main_ref).strip()
    since = since or expedition_marker().get("last")
    if since and git("rev-parse", "--verify", "--quiet", f"{since}^{{commit}}").strip():
        date = git("log", "-1", "--format=%cs", since).strip()
        rng, label = [f"{since}..{p.main_ref}"], f"{since[:9]} ({date})"
    else:
        rng = [p.main_ref, f"--since={EXPEDITION_DEFAULT_DAYS}.days"]
        label = f"-{EXPEDITION_DEFAULT_DAYS}d"
    log = git("log", "--first-parent", "--format=%H\x1f%cs\x1f%s", *rng).splitlines()
    fixes, evidence, new, counts = [], [], [], collections.Counter()
    for line in log:
        parts = line.split("\x1f")
        if len(parts) != 3 or not FIX_RE.search(parts[2]):
            continue
        sha, date, subject = parts
        fixes.append(sha)
        if cap and len(fixes) > cap:
            break
        files = _bookkeeping(commit_files(sha))
        pr = pr_of(subject)
        counts.update(files)
        hit = [x for x in letters if any(path_matches(f, x.get("paths") or []) for f in files)]
        item = {"sha": sha[:9], "pr": pr, "date": date, "subject": subject, "files": files[:6]}
        fresh = [x["id"] for x in hit if pr is None or pr not in evidence_prs(x)]
        if fresh:
            evidence.append({**item, "letters": fresh})
        if not hit and files:
            new.append(item)
    covered = lambda f: any(path_matches(f, x.get("paths") or []) for x in letters)  # noqa: E731
    # A regression test next to a fix is the cure, not the symptom; a lockfile bump
    # rides along with whatever was fixed.
    hot = [{"file": f, "fixes": n} for f, n in counts.most_common()
           if n >= 2 and not covered(f) and not TEST_RE.search(f) and not MANIFEST_RE.search(f)]
    return {"since": label, "head": head, "all": len(log), "fixes": len(fixes),
            "evidence": evidence, "new": new, "hot": hot}


def mark_expedition(sha: str, date: str) -> None:
    """Move the marker in the letters file, keeping the rest of it byte for byte."""
    path = project().letters_path
    text = path.read_text(encoding="utf-8")
    block = f"expedition:\n  last: {sha}\n  date: {date}\n"
    rx = re.compile(r"^expedition:\n(?:[ \t]+.*\n)*", re.MULTILINE)
    if rx.search(text):
        text = rx.sub(block, text, count=1)
    else:
        text = re.sub(r"^(version: *1\n)", r"\1" + block.replace("\\", "\\\\"), text,
                      count=1, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def cmd_expedition(argv: list[str]) -> int:
    letters = load_letters()
    since = argv[argv.index("--since") + 1] if "--since" in argv else None
    if "--mark" in argv:
        sha = git("rev-parse", project().main_ref).strip()
        date = git("log", "-1", "--format=%cs", project().main_ref).strip()
        mark_expedition(sha, date)
        print(T("exp_marked", sha=sha[:9], date=date))
        return 0
    rep = expedition(letters, since)
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    print(T("exp_head", since=rep["since"], head=rep["head"], n=rep["fixes"], all=rep["all"]))
    if not rep["fixes"]:
        print(T("exp_none"))
        return 0
    ref = lambda i: f"#{i['pr']}" if i["pr"] else i["sha"]  # noqa: E731
    if rep["evidence"]:
        print(T("exp_ev"))
        for i in rep["evidence"]:
            print(f"  {ref(i)} {i['date']} — {i['subject'][:80]}")
            print(f"      → {', '.join(i['letters'])}")
    if rep["new"]:
        print(T("exp_new"))
        for i in rep["new"]:
            print(f"  {ref(i)} {i['date']} — {i['subject'][:90]}")
            print(f"      {', '.join(i['files'])}")
    if rep["hot"]:
        print(T("exp_hot"))
        for h in rep["hot"]:
            print(f"  ×{h['fixes']} {h['file']}")
    print(T("exp_tail", cmd=command()))
    return 0


# ---------------------------------------------------------------- brief

def stale_globs(letters: list[dict], tracked: list[str]) -> list[tuple[str, str]]:
    """(letter id, glob) pairs that match nothing in the tree."""
    return [(x.get("id"), g) for x in letters for g in x.get("paths") or []
            if not any(glob_re(g).match(f) for f in tracked)]


def vendored_state(root: pathlib.Path | None = None) -> str:
    """'same' | 'differs' | 'absent' | 'self' — the project's vendored copy against
    this file ('self' when this file *is* that copy)."""
    if is_vendored_copy():
        return "self"
    root = root or project().root
    copy = root / VENDORED_REL
    if not copy.exists():
        return "absent"
    try:
        # Line endings do not count: on Windows git checks either repo out with CRLF
        # or LF depending on its own autocrlf, and that is not drift.
        same = (copy.read_bytes().replace(b"\r\n", b"\n")
                == pathlib.Path(__file__).read_bytes().replace(b"\r\n", b"\n"))
    except OSError:
        return "absent"
    return "same" if same else "differs"


def brief_extras(letters: list[dict]) -> list[str]:
    """Lines that need git: stale letters, the expedition backlog, a drifted copy."""
    lines = []
    tracked = git("ls-files").splitlines()
    stale = stale_globs(letters, tracked) if tracked else []
    if stale:
        lines.append(T("brief_stale", items=", ".join(f"{i} `{g}`" for i, g in stale),
                       letters=LETTERS_REL))
    marker = expedition_marker()
    if marker.get("last"):
        rep = expedition(letters, cap=EXPEDITION_BRIEF_CAP)
        if rep["fixes"]:
            lines.append(T("brief_expedition", date=marker.get("date", "?"), n=rep["fixes"],
                           m=len(rep["new"]), cmd=command()))
    if not is_vendored_copy() and vendored_state() == "differs":
        lines.append(T("brief_vendored", path=VENDORED_REL))
    return lines


def build_brief(letters: list[dict], by_id: dict, front: list, claimed: list,
                today: dt.date, now: float, activity, extras: list[str] | None = None) -> str:
    lines = []
    if activity:
        ct, stamp, subject = activity
        lines.append(T("brief_time", delta=human_delta(now - ct), stamp=stamp, subject=subject[:80]))
    else:
        lines.append(T("brief_time_unknown"))

    ctx = {"by_id": by_id, "claimed": claimed, "today": today}
    picked: dict[str, tuple[int, str, dict]] = {}
    # 1. conditions that hold right now (except `always`)
    for x in letters:
        when = x.get("when")
        if when and when != "always":
            detail = condition(when, ctx)
            if detail is not None:
                picked[x["id"]] = (100 + SEVERITY[x["severity"]], detail, x)
    # 2. letters for the work in the queue
    for weight, nodes in ((2, claimed), (1, front)):
        for n in nodes:
            for x in letters_for_node(n, letters):
                if x.get("while_open"):
                    continue
                score = SEVERITY[x["severity"]] * 10 + weight
                if x["id"] not in picked or picked[x["id"]][0] < score:
                    picked[x["id"]] = (score, T("for_node", id=n["id"]), x)
    # 3. one `always` letter — the lesson of the day, in rotation
    always = [x for x in letters if x.get("when") == "always" and x["id"] not in picked]
    if always:
        x = always[today.toordinal() % len(always)]
        picked[x["id"]] = (0, T("daily"), x)

    chosen = sorted(picked.values(), key=lambda t: (t[0], latest_evidence(t[2])), reverse=True)
    if chosen:
        lines.append(T("brief_letters"))
        for _, detail, x in chosen[:BRIEF_LETTERS]:
            tail = f" [{detail}]" if detail else ""
            lines.append(f" • {one_line(x)}{tail}")

    lines += brief_extras(letters) if extras is None else extras
    bombs = [x for x in letters if x.get("while_open") and ticking(x, by_id)]
    if bombs:
        lines.append(T("brief_bombs", items=" · ".join(f"{x['id']} ⇐ {x['while_open']}"
                                                      for x in bombs)))
    lines.append(T("brief_footer", cmd=command()))
    return "\n".join(lines)


def brief_text(root: str | pathlib.Path | None = None) -> str | None:
    """The brief for a project, or None when it has no letters. For ruti's hook."""
    p = project(root)
    if not p.has_letters():
        return None
    by_id, front, _waiting, claimed = p.graph()
    return build_brief(p.letters(), by_id, front, claimed, dt.date.today(),
                       dt.datetime.now().timestamp(), last_activity())


def cmd_brief() -> int:
    try:
        text = brief_text()
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": text}},
                             ensure_ascii=False))
    except Exception:
        pass  # fail open: a session without advice beats a session that did not start
    return 0


# ---------------------------------------------------------------- warn

def _seen_path(session_id: str) -> pathlib.Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80]
    proj = re.sub(r"[^A-Za-z0-9_.-]", "_", project().root.name)[:40]
    return pathlib.Path(tempfile.gettempdir()) / f"time-machine-{proj}" / f"{safe}.json"


def build_warning(event: dict, letters: list[dict], root: pathlib.Path | None = None,
                  seen: set[str] | None = None) -> tuple[str, list[str]]:
    root = root or project().root
    tool = event.get("tool_name") or ""
    fp = (event.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return "", []
    p = pathlib.Path(fp)
    try:
        rel = p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else p.as_posix()
    except ValueError:
        return "", []                   # outside the project
    seen = seen or set()
    hits = [x for x in letters
            if tool in letter_tools(x) and x["id"] not in seen
            and path_matches(rel, x.get("paths") or [])]
    hits.sort(key=lambda x: -SEVERITY[x["severity"]])
    hits = hits[:WARN_PER_CALL]
    if not hits:
        return "", []
    parts = [T("warn_head", rel=rel)]
    for x in hits:
        parts.append(f"[{x['severity']}] {x['id']}: {x['warning'].strip()}\n"
                     f"{T('warn_do')}: {x['precaution'].strip()}\n"
                     f"{T('warn_ev')}: {evidence_refs(x)}")
    return "\n".join(parts), [x["id"] for x in hits]


def _read_seen(path: pathlib.Path | None) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else []
        return {x for x in data if isinstance(x, str)}
    except Exception:
        return set()  # a broken state file reads as empty; the next write heals it


def _write_seen(path: pathlib.Path | None, seen: set[str]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        os.replace(tmp, path)  # atomic: concurrent hooks cannot interleave the file
    except Exception:
        pass  # not written: the letter repeats once more, no harm


def live_letters(letters: list[dict]) -> list[dict]:
    """Without defused bombs. The graph is loaded only when a bomb matched — `warn`
    runs on every edit and must stay fast."""
    if not any(x.get("while_open") for x in letters):
        return letters
    by_id = project().graph()[0]
    return [x for x in letters if not x.get("while_open") or ticking(x, by_id)]


def warn_text(event: dict, root: str | pathlib.Path | None = None) -> str | None:
    """The additionalContext for a PreToolUse event, or None. Records what was shown."""
    p = project(root)
    if not p.has_letters():
        return None
    session = str(event.get("session_id") or "")
    seen_file = _seen_path(session) if session else None
    seen = _read_seen(seen_file)
    letters = p.letters()
    text, ids = build_warning(event, letters, root=p.root, seen=seen)
    if text and any(x.get("while_open") for x in letters if x["id"] in ids):
        text, ids = build_warning(event, live_letters(letters), root=p.root, seen=seen)
    if not text:
        return None
    _write_seen(seen_file, seen | set(ids))
    return text


def cmd_warn() -> int:
    try:
        # Claude Code sends UTF-8; on Windows sys.stdin is cp1251, and a Cyrillic path
        # turned to mojibake and silently never matched the project root.
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        text = warn_text(event) if isinstance(event, dict) else None
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "additionalContext": text}},
                             ensure_ascii=False))
    except Exception:
        pass  # fail open: the advisor has no right to block an edit
    return 0


# ---------------------------------------------------------------- travel

def goal_path(by_id: dict, target: str | None) -> list[dict]:
    """Every open node `target` depends on, transitively."""
    out, stack, seen = [], list(by_id.get(target or "", {}).get("blocked_by") or []), set()
    while stack:
        i = stack.pop()
        if i in seen or i not in by_id:
            continue
        seen.add(i)
        n = by_id[i]
        if n.get("status") not in ("done", "dropped"):
            out.append(n)
            stack.extend(n.get("blocked_by") or [])
    return out


def cmd_travel() -> int:
    p = project()
    letters = load_letters()
    by_id, front, waiting, claimed = load_graph()
    today = dt.date.today()
    act = last_activity()
    print(T("travel_title"))
    if act:
        print(T("travel_now", now=f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M %z}",
                delta=human_delta(dt.datetime.now().timestamp() - act[0]),
                stamp=act[1], subject=act[2][:70]))
    print(T("travel_counts", front=len(front), claimed=len(claimed), waiting=len(waiting)))

    resolved = {i for i, n in by_id.items() if n.get("status") in ("done", "dropped")}
    queue = [n for n in by_id.values()
             if n.get("owner") == "claude" and n.get("status") in ("open", "in_progress")]
    queue.sort(key=lambda n: ({"in_progress": 0}.get(n.get("status"), 1),
                              0 if all(d in resolved for d in n.get("blocked_by") or []) else 1,
                              {"blocker": 0, "high": 1, "medium": 2, "low": 3}.get(n.get("priority"), 9)))
    if queue:
        print(T("travel_queue"))
        quiet = [x for x in letters if not x.get("while_open")]
        for n in queue:
            open_deps = [d for d in n.get("blocked_by") or [] if d not in resolved]
            waits = ", ".join(f"{d}{'*' if by_id.get(d, {}).get('owner') not in (None, 'claude') else ''}"
                              for d in open_deps)
            state = T("st_taken") if n.get("status") == "in_progress" else (
                T("st_ready") if not open_deps else T("st_waits"))
            print(f"[{state:5}] {n['id']} ({n.get('priority')})" + (f" ← {waits}" if waits else ""))
            for x in letters_for_node(n, quiet):
                print(f"          ⚠ {x['id']}: {x['precaution'].strip()[:150]}")
        print(T("travel_star"))

    if p.goal_node:
        path = goal_path(by_id, p.goal_node)
        print(T("travel_goal", goal=p.goal_node, n=len(path)))
        for n in sorted(path, key=lambda n: n.get("owner") or ""):
            print(f"  {n.get('owner'):8} {n.get('status'):11} {n['id']}")

    ctx = {"by_id": by_id, "claimed": claimed, "today": today}
    print(T("travel_fired"))
    for x in letters:
        if x.get("when") and x["when"] != "always":
            d = condition(x["when"], ctx)
            if d is not None:
                print(f"  {x['id']}: {x['warning'].strip()} [{d}]")

    print(T("travel_bombs"))
    for x in letters:
        if x.get("while_open"):
            state = T("ticking") if ticking(x, by_id) else T("defused")
            print(f"  [{state}] {x['id']} ⇐ {x['while_open']}: {x['warning'].strip()[:160]}")

    tracked = git("ls-files").splitlines()
    for lid, g in stale_globs(letters, tracked) if tracked else []:
        print(T("travel_stale", id=lid, glob=g))

    print(T("travel_debts"))
    for x in letters:
        if x["guard"] in ("prose", "none") and len(x.get("evidence") or []) >= PROSE_DEBT:
            mech = x.get("mechanize")
            print(f"  {x['id']} (×{len(x['evidence'])}, guard: {x['guard']})"
                  + (T("debt_mech", node=mech) if mech else T("debt_none")))
    return 0


# ---------------------------------------------------------------- learn

def cmd_learn(base: str) -> int:
    letters = load_letters()
    files = set(git("diff", "--name-only", f"{base}...HEAD").splitlines())
    files |= set(git("diff", "--name-only", "HEAD").splitlines())
    files |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    subjects = git("log", "--format=%s", f"{base}..HEAD").splitlines()
    hits = [x for x in letters if any(path_matches(f, x.get("paths") or []) for f in files)]
    print(T("learn_head", base=base, files=len(files), n=len(hits)))
    for x in hits:
        print(f"  ⚠ {one_line(x)}")
    if any(FIX_RE.search(s) for s in subjects):
        print(T("learn_fix"))
    return 0


# ---------------------------------------------------------------- backtest

def backtest(letters: list[dict]) -> dict[str, tuple[int, int]]:
    """letter id → (caught, checked) over evidence PRs that have files."""
    result = {}
    for x in letters:
        if not x.get("paths"):
            continue
        caught = total = 0
        for e in x.get("evidence") or []:
            if e.get("replay") is False:
                continue
            sha = pr_commit(e["pr"]) if "pr" in e else e.get("commit")
            if not sha:
                continue
            files = _bookkeeping(commit_files(sha))
            if not files:
                continue                # a PR that only closed a node
            total += 1
            caught += any(path_matches(f, x["paths"]) for f in files)
        result[x["id"]] = (caught, total)
    return result


def noise(letters: list[dict], window: int = 100) -> tuple[dict[str, int], int]:
    """letter id → on how many of the last `window` commits `warn` would have fired.

    Recall on a letter's own evidence is high almost by construction — the globs were
    drawn from those PRs. Noise is the other half of the truth: a letter that fires on
    every other commit stops being read.
    """
    out = git("log", project().main_ref, f"-{window}", "--format=%H")
    commits = [_bookkeeping(commit_files(h)) for h in out.split()]
    commits = [c for c in commits if c]
    return ({x["id"]: sum(any(path_matches(f, x["paths"]) for f in c) for c in commits)
             for x in letters if x.get("paths")}, len(commits))


def cmd_backtest(min_recall: float | None) -> int:
    letters = load_letters()
    res = backtest(letters)
    fired, n = noise(letters)
    caught = sum(c for c, _ in res.values())
    total = sum(t for _, t in res.values())
    print(T("bt_head"))
    print(T("bt_cols", c=T("bt_caught"), n=T("bt_noise"), k=n))
    for lid, (c, t) in sorted(res.items()):
        mark = "—" if not t else f"{c}/{t}"
        print(f"  {mark:>8}  {fired.get(lid, 0):>8}  {lid}")
    recall = caught / total if total else 0.0
    print(T("bt_recall", c=caught, t=total, r=recall) + ("" if total else T("bt_nohist")))
    if min_recall is not None and total and recall < min_recall:
        print(f"::error::time-machine backtest recall {recall:.2f} < {min_recall}")
        return 1
    return 0


# ---------------------------------------------------------------- check

def check_letters(letters: list[dict], by_id: dict, tracked: list[str] | None,
                  stale_is_error: bool = True, has_graph: bool = True) -> tuple[list, list]:
    """stale_is_error=False — the PR does not touch the letters: a glob went stale
    through a rename in another PR, and a one-line claim must not go red for it."""
    errors, warnings, seen = [], [], set()
    for i, x in enumerate(letters):
        lid = x.get("id")
        w = f"letter {lid or '#' + str(i)}"
        for k in KEYS_REQUIRED:
            if not x.get(k):
                errors.append(T("e_field", w=w, k=k))
        unknown = set(x) - set(KEYS_REQUIRED) - set(KEYS_OPTIONAL)
        if unknown:
            errors.append(T("e_unknown", w=w, keys=sorted(unknown)))
        if not isinstance(lid, str) or not ID_RE.match(lid):
            errors.append(T("e_id", w=w))
        elif lid in seen:
            errors.append(T("e_dup", w=w))
        seen.add(lid)
        for k, allowed in (("class", CLASSES), ("severity", set(SEVERITY)), ("guard", GUARDS)):
            if x.get(k) not in allowed:
                errors.append(T("e_enum", w=w, k=k, allowed=sorted(allowed)))
        for k in ("warning", "precaution"):
            if len(str(x.get(k) or "")) > TEXT_LIMIT:
                errors.append(T("e_long", w=w, k=k, n=TEXT_LIMIT))
        ev = x.get("evidence")
        if not isinstance(ev, list) or not ev:
            errors.append(T("e_ev", w=w))
        else:
            for e in ev:
                if not isinstance(e, dict) or not (isinstance(e.get("pr"), int) or e.get("commit")):
                    errors.append(T("e_ev_ref", w=w, e=repr(e)))
                elif not DATE_RE.match(str(e.get("date", ""))):
                    errors.append(T("e_ev_date", w=w, e=repr(e)))
        if not any(x.get(k) for k in ("paths", "nodes", "while_open", "when")):
            errors.append(T("e_trigger", w=w))
        for t in x.get("tools") or []:
            if t not in TOOLS:
                errors.append(T("e_enum", w=w, k="tools", allowed=sorted(TOOLS)))
        for g in x.get("paths") or []:
            if tracked is not None and not any(glob_re(g).match(f) for f in tracked):
                (errors if stale_is_error else warnings).append(T("e_stale", w=w, g=g))
        refs = [("nodes", nid) for nid in x.get("nodes") or []]
        refs += [(k, x[k]) for k in ("while_open", "mechanize") if x.get(k)]
        when = str(x.get("when") or "")
        if when.startswith("node-active:"):
            refs.append(("when", when.split(":", 1)[1]))
        for k, nid in refs:
            if not has_graph:
                errors.append(T("e_nograph", w=w, k=k))
            elif nid not in by_id:
                errors.append(T("e_node", w=w, k=k, id=nid))
        if when and not known_condition(when, by_id) and not when.startswith("node-active:"):
            errors.append(T("e_when", w=w, when=when))
        if (x.get("guard") in ("prose", "none") and isinstance(ev, list)
                and len(ev) >= PROSE_DEBT and not x.get("mechanize")):
            warnings.append(T("w_debt", w=w, n=len(ev), g=x.get("guard")))
        mech = by_id.get(x.get("mechanize") or "")
        if mech and mech.get("status") == "done" and x.get("guard") in ("prose", "none"):
            warnings.append(T("w_mech", w=w, id=mech["id"]))
        if x.get("while_open") and by_id.get(x["while_open"], {}).get("status") in ("done", "dropped"):
            warnings.append(T("w_defused", w=w, id=x["while_open"]))
    return errors, warnings


def cmd_check(base: str | None = None) -> int:
    import yaml
    p = project()
    try:
        doc = yaml.load(p.letters_path.read_text(encoding="utf-8"), Loader=_strict_loader()) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {LETTERS_REL}: {exc}")
        return 1
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    errors, warnings = [], []
    if doc.get("version") != 1:
        errors.append(T("e_version"))
    if set(doc) - TOP_KEYS:
        errors.append(T("e_top", keys=sorted(set(doc) - TOP_KEYS)))
    if set(p.settings) - SETTINGS_KEYS:
        errors.append(T("e_settings", keys=sorted(set(p.settings) - SETTINGS_KEYS)))
    by_id = p.graph()[0]
    # --others: a new, not yet committed file is not a stale target
    tracked = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines() or None
    own = True
    if base:
        own = LETTERS_REL in git("diff", "--name-only", f"{base}...HEAD").splitlines()
    e, w = check_letters(p.letters(), by_id, tracked, stale_is_error=own, has_graph=p.has_graph())
    errors += e
    warnings += w
    for m in warnings:
        print(f"::warning file={LETTERS_REL}::{m}" if gh else f"WARNING: {m}")
    for m in errors:
        print(f"::error file={LETTERS_REL}::{m}" if gh else f"ERROR: {m}")
    if errors:
        return 1
    print(T("ok", n=len(p.letters())))
    return 0


# ---------------------------------------------------------------- status / vendor

def cmd_status() -> int:
    p = project()
    n = len(p.letters()) if p.has_letters() else 0
    graph = (str(p.graph_module.relative_to(p.root)) if p.graph_module and p.graph_module.exists()
             else str(p.tasks_path.relative_to(p.root)) if p.tasks_path and p.tasks_path.exists()
             else T("absent"))
    marker = expedition_marker()
    print(T("st_root", v=p.root))
    print(T("st_letters", v=f"{LETTERS_REL} ({n})" if p.has_letters() else T("absent")))
    print(T("st_graph", v=graph))
    print(T("st_wired", v=T("yes_self") if p.self_wired() else T("no_self")))
    print(T("st_vendored", v=T(vendored_state())))
    print(T("st_marker", v=f"{str(marker.get('last', ''))[:9]} {marker.get('date', '')}".strip()
            or T("absent")))
    print(f"engine        : {ENGINE_VERSION} ({pathlib.Path(__file__).resolve()})")
    return 0


def cmd_vendor(target: str | None) -> int:
    if is_vendored_copy():
        print(T("v_self"))
        return 2
    root = pathlib.Path(target).resolve() if target else project().root
    dst = root / VENDORED_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pathlib.Path(__file__).resolve(), dst)
    print(T("v_done", src=pathlib.Path(__file__).resolve(), dst=dst))
    return 0


# ---------------------------------------------------------------- main

def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    root = _opt(args, "--root")
    if root:
        i = args.index("--root")
        del args[i:i + 2]
        project(root)
    cmd = args[0] if args else "travel"
    if cmd == "brief":
        return cmd_brief()
    if cmd == "warn":
        return cmd_warn()
    if cmd == "travel":
        return cmd_travel()
    if cmd == "learn":
        return cmd_learn(_opt(args, "--base") or project().main_ref)
    if cmd == "expedition":
        return cmd_expedition(args)
    if cmd == "backtest":
        mr = _opt(args, "--min-recall")
        return cmd_backtest(float(mr) if mr else None)
    if cmd == "check":
        return cmd_check(_opt(args, "--base"))
    if cmd == "status":
        return cmd_status()
    if cmd == "vendor":
        return cmd_vendor(args[1] if len(args) > 1 else None)
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

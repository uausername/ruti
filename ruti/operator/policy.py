"""Which Codex "run this command?" prompts may be answered "yes, once" automatically.

The Codex TUI hard-wraps long commands at the pane width, sometimes in the middle of
a word, so the command is judged with *all whitespace removed*. A command is allowed
only when that stripped text is entirely a sequence of the blocks below, each
optionally followed by `;`. Anything else -- including `&&`, `||`, pipes other than
`2>&1 | tee <own log>`, command substitution, `..` and any block not listed -- is
refused and left for a person.

Every path, log name and database a block may name is tied to the pane's own
worktree (`codex-dco` -> logs `/tmp/codex-dco-*`, databases `codex_dco*`), so one
worker can never be approved to touch another worker's evidence or database.

Deliberately NOT allowed, unlike the hand-made approver this replaces:
* `deno test --allow-all` / `-A` / `--allow-run|write|net|ffi|sys` -- only
  `--allow-read` and `--allow-env`, which is what CI uses;
* `psql -f /tmp/...` -- an SQL file the agent wrote itself is arbitrary SQL;
* free-form `SELECT` against the shared `postgres` database, and any SELECT that
  calls file, program, large-object or dblink functions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WORKTREE_NAME = re.compile(r"codex-[a-z0-9][a-z0-9-]{0,40}")

# Raw-text red flags, checked before stripping whitespace.
_RAW_FORBIDDEN = (
    ("..", "path traversal"),
    ("`", "backtick substitution"),
    ("$(", "command substitution"),
    ("<(", "process substitution"),
    (">(", "process substitution"),
    ("&&", "command chaining"),
    ("||", "command chaining"),
)

# SQL that must never pass inside an otherwise allowed SELECT.
_SQL_FORBIDDEN = re.compile(
    r"pg_read|pg_ls_dir|pg_stat_file|pg_file|lo_|dblink|copy|program|pg_terminate|"
    r"pg_cancel|pg_reload|pg_rotate|set_config|pg_sleep|nextval|setval|insert|update|"
    r"delete|drop|alter|create|truncate|grant|revoke|vacuum|analyze|refresh|"
    r"\bdo\b|do\$|call|execute|pg_promote|pg_switch|;.",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Layout:
    """Where things live on this machine (WSL paths)."""

    haccp_root: str = "/mnt/c/mycode/haccp"
    env_sh: str = "/mnt/c/mycode/haccp/.ruti/harness-local-20261103/env.sh"
    tool_root: str = "/home/dima/actions-runner/_work/_tool"

    @property
    def worktrees_re(self) -> str:
        return re.escape(self.haccp_root) + r"/\.ruti/harness-local-\d+/worktrees/"

    @property
    def runs_re(self) -> str:
        return re.escape(self.haccp_root) + r"/\.ruti/harness-local-\d+/codex-runs/"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def strip(text: str) -> str:
    return re.sub(r"\s+", "", text)


_select = r"(?:\"(?i:SELECT)[^\"]*\"|'(?i:SELECT)[^']*')"


def _blocks(worktree: str, layout: Layout) -> list[str]:
    short = worktree.removeprefix("codex-")
    s = re.escape(short)
    db = r"codex_" + re.escape(short.replace("-", "_")) + r"(?:_[a-z0-9_]+)?"
    # own name, optionally with a short run suffix (codex-wm -> /tmp/codex-wm2-analyze.log)
    own_tmp = r"/tmp/(?:codex-|haccp-)?" + s + r"\d*[a-z]?(?:[-_][\w.-]+)?(?:/[\w.-]+)*"
    log = own_tmp  # file name for redirects / tee
    redirect = r"(?:2>&1\|tee(?:-a)?" + log + r"|>" + log + r"(?:2>&1|2>" + log + r")?)?"
    tools = re.escape(layout.tool_root)
    flutter = r"(?:" + tools + r"/flutter/stable-[\d.]+-x64/flutter/bin/)?"
    deno = r"(?:" + tools + r"/deno/[\d.]+/x64/)?"
    pg_env = r"PG(?:HOST=(?:localhost|127\.0\.0\.1)|PORT=55432|USER=postgres|PASSWORD=postgres|DATABASE=" + db + r")"
    sql_file = r"supabase/(?:tests|migrations)/[\w/.-]+\.sql|supabase/seed\.sql"
    psql = r"psql(?:-X)?(?:-vON_ERROR_STOP=1)?(?:-X)?"

    prelude = [
        r"source" + re.escape(layout.env_sh),
        r"cd" + layout.worktrees_re + re.escape(worktree) + r"(?:/app)?",
        r"cdapp",
        r"pwd",
        r"exportDENO_DIR=" + own_tmp,
        r"cd" + own_tmp,
        r'exportPATH="?' + tools + r"/flutter/stable-[\d.]+-x64/flutter/bin:(?:"
        + tools + r'/deno/[\d.]+/x64:)?\$PATH"?',
        r"export(?:" + pg_env + r")+",
        r"(?:" + pg_env + r")+(?=psql)",
        r"set-e", r"set-o?pipefail", r"set-opipefail", r"set-eo?pipefail", r"set-e-opipefail",
        r"[a-z0-9]+_(?:status|exit)=\$\?",
        r'exit"?\$[a-z0-9_]+"?',
    ]
    finals = [
        flutter + r"flutteranalyze(?:--no-pub|--no-fatal-infos)*" + redirect,
        flutter + r"fluttertest(?:--no-pub|--reporter(?:json|expanded|compact)|--concurrency=\d+|(?:app/)?test/[\w/.-]+\.dart)*" + redirect,
        flutter + r"flutterpubget(?:--offline)?" + redirect,
        r"dartformat(?:--set-exit-if-changed|--output=none)*(?:(?:app/)?(?:lib|test)/[\w/.-]+\.dart|lib|test)+" + redirect,
        r"dartrunbuild_runnerbuild(?:--delete-conflicting-outputs)?" + redirect,
        deno + r"denocheck(?:--cached-only)?(?:supabase/functions/[\w/.*-]+\.ts)+" + redirect,
        deno + r"denotest(?:--allow-(?:read|env)(?:=[\w,/.-]+)?|--cached-only|--no-check)*(?:supabase/functions/[\w/.*-]+(?:\.ts|/))*" + redirect,
        r"cat" + own_tmp,
        r"git(?:--no-optional-locks)?(?:rev-parse(?:--short)?HEAD|status(?:--short|--branch|--porcelain)*|diff(?:--stat|--name-only|--check)*|log-\d+(?:--oneline)?)",
        r"dockerps(?:-a)?(?:--filtername=haccp-pgtest)?(?:--format'[{}.\w ]+')?",
        r"dropdb--if-exists" + db,
        r"createdb" + db,
        psql + r"-d" + db + r"(?:-f(?:" + sql_file + r"))+" + redirect,
        psql + r"-f(?:" + sql_file + r")" + redirect,
        psql + r"-dpostgres-c['\"](?:CREATEDATABASE|DROPDATABASEIFEXISTS)" + db + r";?['\"]",
        psql + r"-dpostgres-(?:Atc|tAc|At-c|c)['\"]SELECT(?:version\(\);SELECT)?datnameFROMpg_databaseWHEREdatname(?:=|LIKE)'codex_[a-z0-9_%]+'(?:ORDERBY(?:datname|1))?;?['\"]",
        psql + r"-dpostgres-(?:Atc|tAc|At-c|c)['\"]SELECTversion\(\);?['\"]",
        # free-form read-only SELECT, own database only (checked again by _SQL_FORBIDDEN):
        # either `-d <own db>` or a `PGDATABASE=<own db>` prefix on the same command
        psql + r"-d" + db + r"(?:-Ppager=off)?(?:(?:-At|-A|-t)?-c|-Atc|-tAc)" + _select,
        r"(?:" + pg_env + r")*PGDATABASE=" + db + r"(?:" + pg_env + r")*" + psql
        + r"(?:-Ppager=off)?(?:(?:-At|-A|-t)?-c|-Atc|-tAc)" + _select,
        r"cp(?:-R)?/tmp/codex-" + s + r"-[\w.-]+(?:/[\w.-]+)*/?\.?" + layout.runs_re + s + r"/(?:[\w.-]+)?",
    ]
    return prelude + finals


def _normalise(stripped: str) -> str:
    c = re.sub(r"PGPASSWORD=postgres(?=psql)", "", stripped)
    # connection flags for the disposable local server are equivalent to the env vars
    c = re.sub(r"(psql(?:-[XAtq]+)*)-h(?:localhost|127\.0\.0\.1)-p55432-Upostgres", r"\1", c)
    c = c.replace("-Atqc", "-Atc")
    return c


def decide(command: str, worktree: str, layout: Layout | None = None) -> Decision:
    """Whether `command`, shown in the pane of `worktree`, may be approved once."""
    layout = layout or Layout()
    if not WORKTREE_NAME.fullmatch(worktree or ""):
        return Decision(False, f"not a Codex worker worktree: {worktree!r}")
    if not command or not command.strip():
        return Decision(False, "empty command")
    for token, why in _RAW_FORBIDDEN:
        if token in command:
            return Decision(False, why)
    stripped = _normalise(strip(command))
    if _SQL_FORBIDDEN.search(_sql_parts(stripped)):
        return Decision(False, "forbidden SQL in a SELECT")
    pattern = r"(?:(?:" + "|".join(_blocks(worktree, layout)) + r");?)+"
    if re.fullmatch(pattern, stripped):
        return Decision(True, "every part matches the allowlist")
    return Decision(False, "not on the allowlist")


def _sql_parts(stripped: str) -> str:
    """The quoted SQL of every `psql -c` in the command, for the forbidden-SQL check.

    The fixed CREATE/DROP DATABASE and catalogue queries are allowlisted verbatim, so
    they are removed first; what remains is free-form text an agent chose.
    """
    quoted = re.findall(r"-[A-Za-z]*c(?:\"([^\"]*)\"|'([^']*)')", stripped)
    out = []
    for a, b in quoted:
        q = a or b
        if any(re.fullmatch(f, q) for f in _FIXED_SQL):
            continue
        out.append(q)
    return " ".join(out)


_FIXED_SQL = (
    r"(?:CREATEDATABASE|DROPDATABASEIFEXISTS)codex_[a-z0-9_]+;?",
    r"SELECT(?:version\(\);SELECT)?datnameFROMpg_databaseWHEREdatname(?:=|LIKE)'codex_[a-z0-9_%]+'(?:ORDERBY(?:datname|1))?;?",
    r"SELECTversion\(\);?",
)

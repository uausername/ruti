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
    psql = r"psql(?:-X|-w|-q|-vON_ERROR_STOP=1)*"
    own_wt = layout.worktrees_re + re.escape(worktree)
    flutter_bin = tools + r"/flutter/stable-[\d.]+-x64/flutter/bin/"

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
        r"git(?:--no-optional-locks)?(?:rev-parse(?:(?:--short)?HEAD|--show-toplevel|--abbrev-refHEAD)|branch--show-current|status(?:--short|--branch|--porcelain)*|diff(?:--stat|--name-only|--check)*|log(?:--oneline|-n?\d+|--stat|--decorate)*)",
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
        # hand results over: own /tmp files or files of the own worktree -> own codex-runs dir
        r"cp(?:-R|-r|-p|-n|--no-clobber|--)*(?:" + own_tmp + r"|" + own_wt + r"(?:/[\w.-]+)+)+"
        + layout.runs_re + s + r"/(?:[\w.-]+)?",
        # server version on the shared local server (fixed SQL, no data)
        r"(?:" + pg_env + r"|PGDATABASE=postgres)*" + psql + r"(?:-dpostgres)?(?:-X|-w|-q|-vON_ERROR_STOP=1)*"
        + r"-(?:Atc|tAc|At-c|c)['\"]SELECT(?:current_database\(\),)?version\(\);?['\"]",
        # the project's own check runner, rooted in the own worktree; never its `exec` stage
        r"python3?scripts/agent-pilot/run_checks\.py(?:--root" + own_wt + r"/?|--expected-head[0-9a-f]{7,40}"
        r"|--role[a-z]+|--run-id[\w-]+|--log-dir" + own_tmp + r"|--flutter-bin" + flutter_bin + r"flutter"
        r"|--dart-bin" + flutter_bin + r"dart)*(?:taskgraph|codegen|format|analyze|unittest|test"
        r"(?:test/[\w/]+_test\.dart)?)" + redirect,
        # switching Ruti delegation off for the own session (Administrator's request, 17.11)
        r"rutioff",
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
    for special in (gh_api_read(command), docker_sandbox(command, worktree), tmp_script(command)):
        if special is not None:
            return special
    stripped = _normalise(strip(command))
    if "run_checks" in stripped and "exec" in stripped.lower():
        # its `exec` stage runs whatever follows; the pane may wrap the word, so look stripped
        return Decision(False, "run_checks exec stage")
    if _SQL_FORBIDDEN.search(_sql_parts(stripped)):
        return Decision(False, "forbidden SQL in a SELECT")
    pattern = r"(?:(?:" + "|".join(_blocks(worktree, layout)) + r");?)+"
    if re.fullmatch(pattern, stripped):
        return Decision(True, "every part matches the allowlist")
    return Decision(False, "not on the allowlist")


# --- `gh api` reads of PR discussion ------------------------------------------------
# Codex reviewers read PR comments with ever-changing flags (Administrator, 23.11), so
# this one family is judged word by word (shlex), not as stripped text: any order of the
# flags below, nothing else. Only GET, only this repository, only comments and reviews.
GH_REPOS = ("uausername/haccp", "{owner}/{repo}")
_GH_PATH = re.compile(
    r"/?repos/(?P<repo>[\w.{}-]+/[\w.{}-]+)/(?:pulls/\d+/(?:comments|reviews(?:/\d+/comments)?)"
    r"|pulls/comments/\d+|issues/\d+/comments|issues/comments/\d+)"
    r"(?:\?(?:(?:per_page|page)=\d+)(?:&(?:per_page|page)=\d+)*)?"
)
_GH_HEADER = re.compile(r"(?i)(?:accept:\s*application/(?:vnd\.github[\w.+-]*|json)|x-github-api-version:\s*\d{4}-\d{2}-\d{2})")
_GH_FIELD = re.compile(r"(?:per_page|page)=\d+")
_SHELL_OPS = set(";&|<>()\n")


def gh_api_read(command: str) -> Decision | None:
    """Decision for a `gh api` command, or None when the command is not one.

    Allowed: `gh api <path>` with `--paginate`, `--slurp`, `-i/--include`, `--silent`,
    `--jq/-q <filter>`, `--cache <dur>`, `-X/--method GET`, `-H/--header` (Accept or API
    version only) and `-f/-F/--field/--raw-field` with per_page/page only -- and then
    only with an explicit GET, because `gh api` turns a request with fields into a POST.
    """
    import shlex

    text = command.strip()
    if not re.match(r"gh\s+api(?:\s|$)", text):
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return Decision(False, "gh api: unbalanced quotes")
    if any(w and set(w) <= _SHELL_OPS for w in words):
        return Decision(False, "gh api: shell operator (chain, pipe or redirect)")
    words = words[2:]
    path, method, fields, i = None, None, False, 0

    def value(flag: str) -> str | None:
        nonlocal i
        w = words[i]
        if w.startswith(flag + "=") and flag.startswith("--"):
            return w[len(flag) + 1:]
        if w == flag:
            if i + 1 >= len(words):
                return None
            i += 1
            return words[i]
        if not flag.startswith("--") and w.startswith(flag) and len(w) > len(flag):
            return w[len(flag):]  # -XGET, -q.body
        return None

    while i < len(words):
        w = words[i]
        if w in ("--paginate", "--slurp", "-i", "--include", "--silent"):
            pass
        elif w.startswith(("--jq", "-q")):
            if value("--jq" if w.startswith("--jq") else "-q") is None:
                return Decision(False, f"gh api: bad {w}")
        elif w.startswith("--cache"):
            v = value("--cache")
            if v is None or not re.fullmatch(r"\d+[smh]", v):
                return Decision(False, "gh api: bad --cache")
        elif w.startswith(("-X", "--method")):
            v = value("--method" if w.startswith("--method") else "-X")
            if v is None or v.upper() != "GET":
                return Decision(False, f"gh api: method {v!r} is not GET")
            method = "GET"
        elif w.startswith(("-H", "--header")):
            v = value("--header" if w.startswith("--header") else "-H")
            if v is None or not _GH_HEADER.fullmatch(v.strip()):
                return Decision(False, f"gh api: header {v!r} not allowed")
        elif w.startswith(("--raw-field", "--field", "-f", "-F")):
            flag = next(f for f in ("--raw-field", "--field", "-f", "-F") if w.startswith(f))
            v = value(flag)
            if v is None or not _GH_FIELD.fullmatch(v):
                return Decision(False, f"gh api: field {v!r} is not per_page/page")
            fields = True
        elif w.startswith("-"):
            return Decision(False, f"gh api: flag {w} not allowed")
        else:
            if path is not None:
                return Decision(False, "gh api: more than one endpoint")
            path = w
        i += 1
    if path is None:
        return Decision(False, "gh api: no endpoint")
    m = _GH_PATH.fullmatch(path)
    if not m or m.group("repo") not in GH_REPOS:
        return Decision(False, f"gh api: endpoint {path!r} is not a PR comment/review read")
    if fields and method != "GET":
        return Decision(False, "gh api: fields without an explicit GET make a POST")
    return Decision(True, "gh api: read of PR comments/reviews")


def _words(command: str) -> list[str] | Decision:
    import shlex

    lexer = shlex.shlex(command.strip(), posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return Decision(False, "unbalanced quotes")
    if any(w and set(w) <= _SHELL_OPS for w in words):
        return Decision(False, "shell operator (chain, pipe or redirect)")
    return words


# --- disposable containers (Administrator, 24.11) ------------------------------------
# A container with no network, no privileges and at most a read-only view of the
# worker's own /tmp folder cannot reach the host, other workers or the internet.
_DOCKER_IMAGES = re.compile(r"postgres:\d+(?:\.\d+)?(?:-alpine)?|denoland/deno:[\w.-]+")


def _own_prefix(worktree: str) -> str:
    return re.sub(r"\d+$", "", worktree.removeprefix("codex-"))


def docker_sandbox(command: str, worktree: str) -> Decision | None:
    """Decision for `docker run|rm|exec` of the worker's own throwaway container, or None."""
    text = command.strip()
    m = re.match(r"docker\s+(run|rm|exec)\s", text)
    if not m:
        return None
    words = _words(text)
    if isinstance(words, Decision):
        return Decision(False, "docker: " + words.reason)
    prefix = _own_prefix(worktree)
    own_name = re.compile(r"(?:codex-)?" + re.escape(prefix) + r"\d*[-_][\w.-]+")
    own_tmp = re.compile(r"/tmp/(?:codex-)?" + re.escape(prefix) + r"\d*[\w.-]*(?:/[\w.-]+)*/?")
    sub, args = m.group(1), words[2:]
    if sub == "rm":
        names = [a for a in args if a not in ("-f", "--force", "-v")]
        if names and all(own_name.fullmatch(n) for n in names):
            return Decision(True, "docker: remove own container")
        return Decision(False, "docker rm: not only own containers")
    if sub == "exec":
        while args and args[0] in ("-i", "-t", "-it"):
            args = args[1:]
        if len(args) >= 2 and own_name.fullmatch(args[0]) and args[1] in ("psql", "pg_isready"):
            return Decision(True, "docker: psql in own container")
        return Decision(False, "docker exec: only psql/pg_isready in an own container")
    # run
    network = name = image = None
    i = 0
    while i < len(args):
        a = args[i]
        val = lambda: args[i + 1] if i + 1 < len(args) else ""  # noqa: E731
        if a in ("--rm", "-d", "--detach", "--read-only"):
            i += 1
            continue
        if a.startswith("--network=") or a == "--network":
            network = a.split("=", 1)[1] if "=" in a else val()
            i += 1 if "=" in a else 2
            continue
        if a.startswith("--name=") or a == "--name":
            name = a.split("=", 1)[1] if "=" in a else val()
            i += 1 if "=" in a else 2
            continue
        if a in ("-e", "--env"):
            if not re.fullmatch(r"(?:POSTGRES_(?:HOST_AUTH_METHOD=trust|DB=\w+|PASSWORD=\w+|USER=\w+)"
                                r"|DENO_DIR=/tmp/[\w.-]+|PGDATA=/[\w/.-]+)", val()):
                return Decision(False, f"docker run: env {val()!r} not allowed")
            i += 2
            continue
        if a == "--tmpfs":
            if not re.fullmatch(r"/[\w/.-]+", val()):
                return Decision(False, "docker run: bad --tmpfs")
            i += 2
            continue
        if a in ("-v", "--volume"):
            mm = re.fullmatch(r"([^:]+):(/[\w/.-]+):ro", val())
            if not mm or not own_tmp.fullmatch(mm.group(1)) or ".." in val():
                return Decision(False, f"docker run: volume {val()!r} is not an own /tmp folder read-only")
            i += 2
            continue
        if a in ("-w", "--workdir"):
            i += 2
            continue
        if a.startswith("-"):
            return Decision(False, f"docker run: flag {a} not allowed")
        image = a
        break  # the rest is the container's own command: it runs inside the sandbox
    if network != "none":
        return Decision(False, "docker run: needs --network none")
    if name is not None and not own_name.fullmatch(name):
        return Decision(False, f"docker run: name {name!r} is not the worker's own")
    if not image or not _DOCKER_IMAGES.fullmatch(image):
        return Decision(False, f"docker run: image {image!r} not allowed")
    return Decision(True, "docker: sandboxed own container (no network, own /tmp read-only)")


def tmp_script(command: str) -> Decision | None:
    """Never approved: a script the agent wrote is arbitrary code. Name it for the person."""
    m = re.match(r"(?:bash|sh|python3?)\s+(/tmp/[\w./-]+)", command.strip())
    if m:
        return Decision(False, f"runs a script from /tmp -- read {m.group(1)} before approving")
    return None


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
    r"SELECT(?:current_database\(\),)?version\(\);?",
)

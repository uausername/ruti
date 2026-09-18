"""Running `opencode` on the manager's behalf and reporting back in a few lines.

This is where most of the subscription saving actually comes from, and it needs no
model to do it. `opencode` emits tens of thousands of tokens of tool traffic; if the
manager reads that directly it pays for all of it in context, and context length is
what drives the burn rate. A deterministic wrapper that watches the run and reports
"3 files changed, +84/-12, exit 0" costs nothing and tells the manager everything it
needs to decide what happens next.

It also guards the failure that motivated this whole project: a request for a local
model silently answered by a remote one. LiteLLM's fallback chain does that by design,
and from the outside it is indistinguishable from success -- except that the response
names the model that really answered. So we ask, before sending the work.

That check is about *which backend* took the request, and it says nothing about which
model did the work behind a router. The two are reported separately: `substituted`
for the first, `model_effective` for the second (see `usage.py`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from . import usage as usage_mod
from .config import LOG_DIR, PROXY_BASE, STATE_ROOT, ensure_dirs, read_json, write_json

DEFAULT_TIMEOUT = 900.0

# Written while a delegate is running so the status line can say what is executing
# right now, rather than only what finished last.
RUNNING_FILE = STATE_ROOT / "running.json"

# Directories a delegate fills as a side effect of running code, never as work. Listed
# by name rather than by path: they turn up nested as readily as at the top level.
GENERATED_DIRS = frozenset({
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "node_modules", ".next", "dist", "build", "target", ".gradle",
})


@dataclass
class Probe:
    """What the proxy said about a one-token request to an alias."""

    model: str | None = None
    group: str | None = None  # the model group that served it
    fallbacks: int | None = None  # LiteLLM's own count of fallbacks attempted


@dataclass
class Outcome:
    model_requested: str
    model_answering: str | None = None
    # The model that really did the work, from the proxy's usage log -- never the
    # alias. For a router alias this is where its pick finally becomes visible.
    model_effective: str = usage_mod.UNKNOWN
    router: bool = False
    usage: usage_mod.Usage | None = None
    exit_code: int | None = None
    duration_s: float = 0.0
    files_changed: list[str] = field(default_factory=list)
    diff_stat: str = ""
    log_path: str = ""
    tail: str = ""
    error: str = ""
    substituted: bool = False
    lines_written: int = 0
    broken_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error and not self.broken_files

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model_requested": self.model_requested,
            "model_answering": self.model_answering,
            "model_effective": self.model_effective,
            "router": self.router,
            "substituted": self.substituted,
            "cost_usd": self.usage.cost_usd if self.usage else None,
            **({"usage": self.usage.summary()} if self.usage else {}),
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 1),
            "files_changed": self.files_changed,
            "lines_written": self.lines_written,
            **({"broken_files": self.broken_files} if self.broken_files else {}),
            "diff_stat": self.diff_stat,
            "log": self.log_path,
            **({"error": self.error} if self.error else {}),
            **({"tail": self.tail} if self.tail else {}),
        }


def who_answers(model: str, timeout: float = 30.0) -> str | None:
    """Ask the proxy which model actually replies for `model`."""
    return probe(model, timeout).model


def probe(model: str, timeout: float = 30.0) -> Probe:
    """Send `model` a one-token request and keep what the proxy says about the reply.

    A mismatch means LiteLLM's fallback is standing in for a backend that is down. The
    request still succeeds, so nothing downstream notices -- including the manager,
    which will happily keep routing "local, private" work to a remote provider. The
    proxy also states it outright in `x-litellm-attempted-fallbacks`, which is what
    lets a router naming its own pick be told apart from a fallback.
    """
    import urllib.error
    import urllib.request

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{PROXY_BASE}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            headers = response.headers
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return Probe()
    try:
        fallbacks = int(headers.get("x-litellm-attempted-fallbacks"))
    except (TypeError, ValueError):
        fallbacks = None
    return Probe(
        model=body.get("model") if isinstance(body, dict) else None,
        group=headers.get("x-litellm-model-group") or None,
        fallbacks=fallbacks,
    )


def is_substitution(alias: str, answer: Probe, *, router: bool) -> bool:
    """Did a different backend take the request than the one `alias` names?

    Unchanged in meaning from the original name comparison: it is about LiteLLM's
    fallback standing in for a backend that is down. What it must not do is read a
    router's choice as that. A router answering under the name of the model it picked
    is doing its job -- only the proxy reporting a fallback, or a different group
    serving the request, counts as one.
    """
    if answer.fallbacks:
        return True
    if answer.group and answer.group != alias:
        return True
    if not answer.model or answer.model.split("/")[-1] == alias:
        return False
    return not router


def _router_alias(alias: str) -> bool:
    from . import openrouter, providers

    return any(
        openrouter.is_router_record(record)
        for record in providers.load_registry()["providers"]
        if record.get("alias") == alias
    )


def _running_alias() -> str | None:
    """The alias another delegation is running against right now, if any."""
    marker = read_json(RUNNING_FILE, default=None)
    if not isinstance(marker, dict) or marker.get("expires_at", 0) < time.time():
        return None
    return str(marker.get("model", "")).split("/")[-1] or None


def _opencode_executable() -> str:
    """The real `opencode.exe`, not the npm `.cmd` shim that wraps it.

    Windows `CreateProcess` cannot launch a `.cmd`/`.bat` directly; Python's
    `subprocess` silently retries through `cmd.exe /c` when that happens, and cmd.exe's
    own reparsing of the command line is where a long task brief gets mangled -- an
    embedded newline truncates the argument outright, and a literal `%` triggers
    variable expansion. Found from a delegation that came back exit 0 with the model
    complaining its message was empty, while the exact same text written to the task
    log on disk was complete. The shim itself does nothing but forward every argument
    to this .exe a few directories down (`"%dp0%\\node_modules\\opencode-ai\\bin\\
    opencode.exe" %*`); calling it directly means cmd.exe never sees the argument.
    """
    import shutil

    shim = shutil.which("opencode")
    if not shim:
        return "opencode"  # let proc.resolve() fail with its own ToolNotFound message
    path = Path(shim)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return shim
    candidate = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    return str(candidate) if candidate.is_file() else shim


def _git(args: list[str], cwd: Path) -> str:
    try:
        result = proc.run(["git", *args], cwd=cwd, timeout=30.0)
        # rstrip only -- `git status --porcelain` uses a leading space as a real
        # status column (e.g. " M file.py"), and a plain .strip() eats it, which
        # then shifts every line[3:] slice in files_changed by one character.
        return result.stdout.rstrip() if result.ok else ""
    except (proc.ToolNotFound, proc.ToolTimeout):
        return ""


def run(
    task: str,
    *,
    model: str,
    directory: Path,
    timeout: float = DEFAULT_TIMEOUT,
    check_substitution: bool = True,
) -> Outcome:
    """Run one delegated task and return a summary small enough to read."""
    ensure_dirs()
    alias = model.split("/")[-1]
    outcome = Outcome(model_requested=model, router=_router_alias(alias))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"delegate-{stamp}-{model.replace('/', '_')}.log"
    outcome.log_path = str(log_path)

    if check_substitution:
        answer = probe(alias)
        outcome.model_answering = answer.model
        outcome.substituted = is_substitution(alias, answer, router=outcome.router)

    # Kept alongside the log so a surprising result can be traced back to the exact
    # brief that produced it. It is not passed with `-f`: that flag takes a list of
    # attachments and greedily swallows the message argument as another filename.
    task_file = LOG_DIR / f"task-{stamp}.md"
    task_file.write_text(task, encoding="utf-8")

    # Windows caps a command line at ~32k characters. The prompt is passed as one argv
    # element (no shell, so no quoting hazard), but an oversized brief would be
    # truncated into something that still looks like a valid instruction.
    if len(task) > 24_000:
        outcome.error = (
            f"the brief is {len(task)} characters, too long to pass safely on a Windows "
            "command line -- split it into smaller tasks"
        )
        outcome.exit_code = -1
        return outcome

    head_before = _git(["rev-parse", "HEAD"], directory)
    dirty_before = set(_git(["status", "--porcelain"], directory).splitlines())

    # Usage is matched by alias and time, so a concurrent run against the same alias
    # would be counted in with this one. Rare, but worth saying when it can happen.
    overlapping = _running_alias() == alias
    started = time.monotonic()
    started_wall = time.time()
    _mark_running(model, timeout)
    try:
        # `opencode run` has no timeout flag of its own, so the parent has to impose
        # one or a stuck delegate wedges the session indefinitely.
        result = proc.run(
            [_opencode_executable(), "run", "--dir", str(directory), "--model", model,
             "--auto", task],
            timeout=timeout,
            cwd=directory,
        )
        outcome.exit_code = result.returncode
        log_path.write_text(
            f"$ opencode run --model {model}\n\n=== stdout ===\n{result.stdout}\n"
            f"=== stderr ===\n{result.stderr}\n",
            encoding="utf-8",
        )
        if result.returncode != 0:
            outcome.tail = "\n".join(
                (result.stderr or result.stdout).strip().splitlines()[-20:]
            )
    except proc.ToolTimeout as exc:
        outcome.error = str(exc)
        outcome.exit_code = -1
        # Whatever the child printed before it was killed -- often the only clue to
        # what it was stuck doing (indexing the repo, waiting on a prompt, ...). Write
        # it to the same log a normal run would have produced rather than leaving no
        # trace at all.
        if exc.stdout or exc.stderr:
            log_path.write_text(
                f"$ opencode run --model {model}\n\n=== KILLED: {exc} ===\n\n"
                f"=== stdout (partial) ===\n{exc.stdout}\n"
                f"=== stderr (partial) ===\n{exc.stderr}\n",
                encoding="utf-8",
            )
            outcome.tail = "\n".join(
                (exc.stderr or exc.stdout).strip().splitlines()[-20:]
            )
    except proc.ToolNotFound as exc:
        outcome.error = str(exc)
        outcome.exit_code = -1
    finally:
        outcome.duration_s = time.monotonic() - started
        finished_wall = time.time()
        _clear_running()

    outcome.usage = usage_mod.collect(alias, started_wall, finished_wall)
    outcome.model_effective = outcome.usage.model
    if overlapping:
        outcome.usage.note = "; ".join(filter(None, [
            outcome.usage.note,
            "another delegation to this alias was running at the same time, and its "
            "requests may be counted here",
        ]))

    dirty_after = set(_git(["status", "--porcelain"], directory).splitlines())
    outcome.files_changed = sorted(
        line[3:] for line in (dirty_after - dirty_before) if len(line) > 3
    )
    head_after = _git(["rev-parse", "HEAD"], directory)
    if head_before and head_after and head_before != head_after:
        outcome.diff_stat = _git(["diff", "--stat", f"{head_before}..{head_after}"], directory)
    elif outcome.files_changed:
        # Scoped to the delegate's own paths. An unscoped `git diff --stat` reports
        # every modification in the working tree, so a caller who had uncommitted work
        # of their own would see it attributed to the delegate -- which is worse than
        # showing nothing, because it looks like the delegate edited files it was told
        # not to touch.
        outcome.diff_stat = _git(["diff", "--stat", "--", *outcome.files_changed], directory)

    outcome.broken_files = _unparsable_python(outcome.files_changed, directory)
    outcome.lines_written = _count_written_lines(outcome.files_changed, directory)
    _record(outcome, log_path)
    return outcome


def _mark_running(model: str, timeout: float) -> None:
    """Announce that a delegate is executing. Never raises: this is only reporting.

    Carries an expiry rather than a pid. If this process is killed outright the marker
    is never cleared, and a status line that trusted it would claim a delegate is still
    running for the rest of the session. The run cannot outlive its own timeout, so
    anything past that is stale by definition.
    """
    try:
        ensure_dirs()
        write_json(RUNNING_FILE, {
            "model": model,
            "started_at": time.time(),
            "expires_at": time.time() + timeout,
        })
    except Exception:
        pass


def _clear_running() -> None:
    try:
        RUNNING_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_generated(path: Path, directory: Path) -> bool:
    """Whether any component of `path` is a directory tooling produces on its own."""
    try:
        parts = path.relative_to(directory).parts
    except ValueError:
        parts = path.parts
    return any(part in GENERATED_DIRS for part in parts)


def _iter_written_files(paths: list[str], directory: Path):
    """Every file under the delegate's changed paths, minus what tooling generated.

    A delegate that runs the tests it has just written leaves `__pycache__` behind, and
    git reports the directory as changed like any other. Those files are not work, and
    counting them inverts the measurement rather than merely blurring it: this
    project's own first end-to-end delegation recorded 923 lines written for two files
    containing 155, the difference being compiled bytecode read as though it were
    source.
    """
    for entry in paths:
        target = directory / entry.strip().strip('"')
        if _is_generated(target, directory):
            continue
        try:
            candidates = (
                [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else [target]
            )
        except OSError:
            continue
        for path in candidates:
            if _is_generated(path, directory):
                continue
            yield path


def _unparsable_python(paths: list[str], directory: Path) -> list[str]:
    """Python files the delegate wrote that do not parse.

    A delegate can exit 0 having produced a file that cannot even be imported, and
    then nothing downstream notices: `opencode` reports success, the summary says OK,
    and the manager moves on. Found this way on a real project -- a test file whose
    string literal was left unterminated was reported as a clean run.

    Parsing is the cheapest possible check and needs no model, so it belongs here
    rather than in a verifier subagent. It says nothing about whether the code is
    *correct* -- only that it is not obviously broken, which is a different and much
    weaker claim, and the reason a verifier still has a job.
    """
    broken = []
    for path in _iter_written_files(paths, directory):
        if path.suffix != ".py":
            continue
        try:
            compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(directory)}:{exc.lineno}: {exc.msg}")
        except (OSError, ValueError):
            continue
    return broken


def _count_written_lines(paths: list[str], directory: Path) -> int:
    """Lines the delegate produced -- the work the manager did not have to emit itself.

    This is the honest measure of what delegation saves, and it is not the one the
    design originally assumed. `opencode`'s own output turned out to be terse: five
    files created produced barely half a kilobyte of stdout, so "containing the
    transcript" saves very little. What it genuinely avoids is the generated code
    passing through the manager's context on the way to disk, which for a tool-using
    model is the bulk of what it would have spent.

    Counted from the files themselves rather than from git, so an untracked new file
    counts and the caller's index is never touched.
    """
    total = 0
    for path in _iter_written_files(paths, directory):
        try:
            if path.stat().st_size >= 1_000_000:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        # A NUL byte is the cheapest reliable "this is not source" signal, and it
        # catches artefacts an extension list would not think to name. Decoding with
        # errors="ignore" is what made this necessary: it turns any binary file into a
        # plausible-looking line count instead of failing loudly enough to skip it.
        if b"\x00" in blob:
            continue
        total += len(blob.decode("utf-8", errors="ignore").splitlines())
    return total


def _record(outcome: Outcome, log_path: Path) -> None:
    """File what this run produced and what the manager was spared."""
    from . import ledger

    try:
        log_bytes = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        log_bytes = 0

    alias = outcome.model_requested.split("/")[-1]
    # A local run that a fallback answered was not local, and may have been billed.
    local = alias.startswith("local-") and not outcome.substituted
    spent = outcome.usage
    ledger.record(
        "delegation",
        model=outcome.model_requested,
        tier="local" if alias.startswith("local-") else "remote",
        ok=outcome.ok,
        substituted=outcome.substituted,
        model_effective=outcome.model_effective,
        router=outcome.router,
        # Who sends the bill, for the report's money column. A local run costs nothing
        # by construction; a remote one is costed only if the provider stated a price.
        provider="local" if local else _billing_provider(alias),
        requests=spent.requests if spent else 0,
        cost_usd=0.0 if local else (spent.cost_usd if spent else None),
        cost_complete=True if local else bool(spent and spent.cost_complete),
        models={
            row["model"]: {"requests": row["requests"], "cost_usd": row["cost_usd"]}
            for row in (spent.models if spent else [])
        },
        duration_s=round(outcome.duration_s, 1),
        files_changed=len(outcome.files_changed),
        # The names, not just the count: without them the report can say how much a
        # delegate wrote but never which files, and nothing can tell whether a given
        # file came from a delegate or was typed in the session.
        files=list(outcome.files_changed),
        lines_written=outcome.lines_written,
        log_bytes=log_bytes,
        summary_bytes=len(json.dumps(outcome.summary())),
    )


def _billing_provider(alias: str) -> str | None:
    from . import providers

    for record in providers.load_registry()["providers"]:
        if record.get("alias") == alias:
            return record.get("provider")
    return None

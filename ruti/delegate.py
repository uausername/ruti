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
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from .config import LOG_DIR, PROXY_BASE, STATE_ROOT, ensure_dirs, write_json

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
class Outcome:
    model_requested: str
    model_answering: str | None = None
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
            "substituted": self.substituted,
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
    """Ask the proxy which model actually replies for `model`.

    A mismatch means LiteLLM's fallback is standing in for a backend that is down. The
    request still succeeds, so nothing downstream notices -- including the manager,
    which will happily keep routing "local, private" work to a remote provider.
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
            return json.loads(response.read().decode("utf-8")).get("model")
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None


def _git(args: list[str], cwd: Path) -> str:
    try:
        result = proc.run(["git", *args], cwd=cwd, timeout=30.0)
        return result.stdout.strip() if result.ok else ""
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
    outcome = Outcome(model_requested=model)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"delegate-{stamp}-{model.replace('/', '_')}.log"
    outcome.log_path = str(log_path)

    if check_substitution:
        answering = who_answers(f"{model.split('/')[-1]}")
        outcome.model_answering = answering
        if answering and answering.split("/")[-1] != model.split("/")[-1]:
            outcome.substituted = True

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

    started = time.monotonic()
    _mark_running(model, timeout)
    try:
        # `opencode run` has no timeout flag of its own, so the parent has to impose
        # one or a stuck delegate wedges the session indefinitely.
        result = proc.run(
            ["opencode", "run", "--dir", str(directory), "--model", model, "--auto", task],
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
    except proc.ToolNotFound as exc:
        outcome.error = str(exc)
        outcome.exit_code = -1
    finally:
        outcome.duration_s = time.monotonic() - started
        _clear_running()

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
    ledger.record(
        "delegation",
        model=outcome.model_requested,
        tier="local" if alias.startswith("local-") else "remote",
        ok=outcome.ok,
        substituted=outcome.substituted,
        duration_s=round(outcome.duration_s, 1),
        files_changed=len(outcome.files_changed),
        lines_written=outcome.lines_written,
        log_bytes=log_bytes,
        summary_bytes=len(json.dumps(outcome.summary())),
    )

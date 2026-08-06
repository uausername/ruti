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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from .config import LOG_DIR, PROXY_BASE, ensure_dirs

DEFAULT_TIMEOUT = 900.0


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

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model_requested": self.model_requested,
            "model_answering": self.model_answering,
            "substituted": self.substituted,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 1),
            "files_changed": self.files_changed,
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
    import json
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

    dirty_after = set(_git(["status", "--porcelain"], directory).splitlines())
    outcome.files_changed = sorted(
        line[3:] for line in (dirty_after - dirty_before) if len(line) > 3
    )
    head_after = _git(["rev-parse", "HEAD"], directory)
    if head_before and head_after and head_before != head_after:
        outcome.diff_stat = _git(["diff", "--stat", f"{head_before}..{head_after}"], directory)
    else:
        outcome.diff_stat = _git(["diff", "--stat"], directory)

    return outcome

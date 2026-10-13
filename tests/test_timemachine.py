"""The time machine as a ruti mode: the engine, its hooks, and the vendored copy.

Every test builds a throwaway git project with a letters file, because the engine's
answers come from git history and the working tree, never from ruti's own state.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from click.testing import CliRunner

from ruti import modes
from ruti import timemachine as tm
from ruti.cli import main as cli
from ruti.hooks import session_start, wait_gate

LETTERS = """version: 1
settings:
  lang: en
  tasks: docs/tasks.yaml
  goal_node: launch
expedition:
  last: {base}
  date: 2026-10-01
letters:
  - id: parity
    title: Two implementations drifted
    class: parity
    severity: high
    paths: ["src/reducer.py"]
    warning: The two reducers disagreed twice.
    precaution: Change both sides in one PR.
    evidence:
      - {{pr: 1, date: 2026-09-01}}
    guard: test
  - id: cors-open
    title: CORS is open
    class: bomb
    severity: medium
    while_open: set-origins
    paths: ["src/cors.py"]
    warning: Without the variable every origin is allowed.
    precaution: Do not remove the fallback silently.
    evidence:
      - {{pr: 2, date: 2026-09-02}}
    guard: none
"""

TASKS = """version: 1
nodes:
  - id: launch
    title: Launch
    kind: ops
    owner: founder
    status: open
    priority: high
    blocked_by: [refactor-reducer, set-origins]
  - id: refactor-reducer
    title: Refactor the reducer
    kind: feature
    owner: claude
    status: open
    priority: high
    touches: ["src/reducer.py"]
    acceptance: x
  - id: set-origins
    title: Set ALLOWED_ORIGINS
    kind: ops
    owner: founder
    status: {origins}
    priority: medium
"""


class Repo:
    def __init__(self, root):
        self.root = root

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message: str, files: dict[str, str]) -> str:
        for rel, body in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    r = Repo(root)
    r.git("init", "-q", "-b", "main")
    r.git("config", "user.email", "t@t")
    r.git("config", "user.name", "t")
    base = r.commit("chore: start", {"src/reducer.py": "a", "src/cors.py": "a", "src/other.py": "a"})
    r.commit("feat: letters and graph", {
        "docs/future-letters.yaml": LETTERS.format(base=base),
        "docs/tasks.yaml": TASKS.format(origins="open"),
    })
    # warn's once-per-session memory lives in the temp dir; keep it inside this test
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TIMEMACHINE_ROOT", raising=False)
    return r


def pre_tool(root, file: str, tool: str = "Edit", session: str = "s1") -> dict:
    return {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": tool,
            "cwd": str(root), "tool_input": {"file_path": str(root / file)}}


# ---------------------------------------------------------------- engine


@pytest.mark.parametrize("glob,path,hit", [
    ("src/**", "src/a/b.py", True),
    ("src/*.py", "src/a/b.py", False),
    ("docs/", "docs/x/y.md", True),
    ("mvp outline", "mvp outline", True),
])
def test_globs(glob, path, hit):
    assert bool(tm.glob_re(glob).match(path)) is hit


def test_a_broad_node_does_not_pick_up_letters_about_single_files():
    assert tm.touches_overlap("src/", "src/**")
    assert not tm.touches_overlap(".github/workflows/", ".github/workflows/prod-watch.yml")


def test_check_passes_and_refuses_a_letter_without_evidence(repo):
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 0
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text().replace("      - {pr: 1, date: 2026-09-01}\n", ""))
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_check_refuses_a_duplicate_key(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text().replace("    guard: test\n", "    guard: test\n    guard: ci\n"))
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_expedition_sorts_fixes_since_the_marker(repo):
    repo.commit("fix(reducer): drifted again (#10)", {"src/reducer.py": "b"})
    repo.commit("fix(other): new trouble (#11)", {"src/other.py": "b", "tests/test_other.py": "t"})
    repo.commit("fix(other): again (#12)", {"src/other.py": "c"})
    repo.commit("feat: not a fix (#13)", {"src/other.py": "d"})
    tm.project(repo.root)
    rep = tm.expedition(tm.load_letters())
    assert rep["fixes"] == 3
    assert [(i["pr"], i["letters"]) for i in rep["evidence"]] == [(10, ["parity"])]
    assert sorted(i["pr"] for i in rep["new"]) == [11, 12]
    assert rep["hot"] == [{"file": "src/other.py", "fixes": 2}]

    assert tm.main(["tm", "--root", str(repo.root), "expedition", "--mark"]) == 0
    tm.project(repo.root)
    assert tm.expedition(tm.load_letters())["fixes"] == 0
    assert "  - id: parity" in (repo.root / "docs/future-letters.yaml").read_text()


# ---------------------------------------------------------------- hooks


def test_session_start_brings_the_brief(repo):
    result = session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)})
    assert result is not None
    user, model = result
    assert "since the last session" in user
    assert "parity" in model                      # a letter for the node in the queue
    assert "cors-open ⇐ set-origins" in model      # a ticking bomb


def test_a_project_without_letters_gets_nothing(tmp_path):
    (tmp_path / "plain").mkdir()
    assert session_start.time_machine({"session_id": "s1", "cwd": str(tmp_path / "plain")}) is None


def test_warn_names_the_letter_once_per_session(repo):
    first = wait_gate.handle(pre_tool(repo.root, "src/reducer.py"))
    assert "parity" in first["hookSpecificOutput"]["additionalContext"]
    assert "permissionDecision" not in first["hookSpecificOutput"]   # never blocks
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py")) is None
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py", session="s2")) is not None


def test_warn_ignores_reads_and_unrelated_files(repo):
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py", tool="Read")) is None
    assert wait_gate.handle(pre_tool(repo.root, "src/other.py")) is None


def test_a_defused_bomb_stays_silent(repo):
    (repo.root / "docs/tasks.yaml").write_text(TASKS.format(origins="done"))
    assert wait_gate.handle(pre_tool(repo.root, "src/cors.py")) is None
    (repo.root / "docs/tasks.yaml").write_text(TASKS.format(origins="open"))
    assert wait_gate.handle(pre_tool(repo.root, "src/cors.py", session="s3")) is not None


def test_mode_off_silences_both_hooks(repo):
    modes.set_timemachine("s1", False)
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py")) is None
    assert session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)}) is None


def test_a_self_wired_project_keeps_its_own_hook(repo):
    settings = repo.root / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "bash .claude/hooks/time-machine.sh warn"}]}]}}))
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py")) is None
    assert session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)}) is None


# ---------------------------------------------------------------- vendored copy


def test_vendor_copies_byte_for_byte_and_status_sees_drift(repo):
    runner = CliRunner()
    result = runner.invoke(cli, ["timemachine", "--root", str(repo.root), "vendor"])
    assert result.exit_code == 0, result.output
    copy = repo.root / tm.VENDORED_REL
    assert copy.read_bytes() == open(tm.__file__, "rb").read()
    tm.project(repo.root)
    assert tm.vendored_state() == "same"

    copy.write_text(copy.read_text() + "\n# edited here instead of in ruti\n")
    assert tm.vendored_state() == "differs"
    assert any("vendored" in line for line in tm.brief_extras(tm.load_letters()))
    result = runner.invoke(cli, ["timemachine", "--root", str(repo.root), "status"])
    assert "DIFFERS" in result.output


def test_the_vendored_copy_runs_on_its_own(repo, tmp_path):
    """What a cloud session without ruti does: run the copy with plain python."""
    import os
    import sys

    CliRunner().invoke(cli, ["timemachine", "--root", str(repo.root), "vendor"])
    copy = repo.root / tm.VENDORED_REL
    # its own temp dir: the copy remembers shown letters there, and a real /tmp would
    # carry that memory from one run of the suite into the next
    env = {**os.environ, "TMPDIR": str(tmp_path / "tmp"), "TEMP": str(tmp_path / "tmp")}
    event = json.dumps(pre_tool(repo.root, "src/reducer.py", session="cloud"))
    out = subprocess.run([sys.executable, str(copy), "warn"], input=event.encode(), env=env,
                         capture_output=True, cwd=str(repo.root)).stdout.decode()
    assert "parity" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    brief = subprocess.run([sys.executable, str(copy), "brief"], input=b"{}", env=env,
                           capture_output=True, cwd=str(repo.root)).stdout.decode()
    assert "python3 scripts/time-machine/tm.py travel" in json.loads(brief)[
        "hookSpecificOutput"]["additionalContext"]


def test_mode_shows_up_with_a_default_of_on():
    assert modes.DEFAULTS["timemachine"] is True
    assert modes.current("fresh")["timemachine"] is True

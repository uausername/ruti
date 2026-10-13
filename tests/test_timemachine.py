"""The time machine as a ruti mode: the engine, its hooks, and the vendored copy.

Every test builds a throwaway git project with a letters file, because the engine's
answers come from git history and the working tree, never from ruti's own state.
"""

from __future__ import annotations

import json
import re
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
    letters.write_text(letters.read_text(encoding="utf-8").replace("      - {pr: 1, date: 2026-09-01}\n", ""),
                       encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_check_refuses_a_duplicate_key(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8").replace("    guard: test\n", "    guard: test\n    guard: ci\n"),
                       encoding="utf-8")
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
    assert "  - id: parity" in (repo.root / "docs/future-letters.yaml").read_text(encoding="utf-8")


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
    (repo.root / "docs/tasks.yaml").write_text(TASKS.format(origins="done"), encoding="utf-8")
    assert wait_gate.handle(pre_tool(repo.root, "src/cors.py")) is None
    (repo.root / "docs/tasks.yaml").write_text(TASKS.format(origins="open"), encoding="utf-8")
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

    copy.write_bytes(copy.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert tm.vendored_state() == "same"          # CRLF from a Windows checkout is not drift
    copy.write_text(copy.read_text(encoding="utf-8") + "\n# edited here instead of in ruti\n",
                    encoding="utf-8")
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


# ---------------------------------------------------------------- review of haccp#449


@pytest.mark.parametrize("touch,glob,hit", [
    ("supabase/migrations/", "supabase/migrations/*.sql", True),     # the dir's own files
    (".github/workflows/", ".github/workflows/*-cron.yml", True),
    (".github/workflows/", ".github/workflows/prod-watch.yml", False),  # one literal file
    ("supabase/functions/", "supabase/functions/org-*/**", False),   # `*` mid-glob: narrower
    ("app/lib/data/capture_x/", "app/lib/data/capture_*.dart", False),  # `*` never crosses `/`
    ("app/lib/ui/chat/", "app/lib/**", True),
    ("app/", "app/lib/**", False),
    ("src/a.py", "src/*.py", True),
])
def test_touches_overlap_is_segment_wise(touch, glob, hit):
    assert tm.touches_overlap(touch, glob) is hit


def test_a_claim_is_stale_after_24_hours_not_after_midnight():
    import datetime as dt
    node = {"id": "n", "status": "in_progress", "notes": "2026-10-12: claim"}
    morning_after = dt.datetime(2026, 10, 13, 9, 0)
    assert tm.stale_claims([node], morning_after.date(), morning_after) == []
    two_days_on = dt.datetime(2026, 10, 14, 9, 0)
    assert tm.stale_claims([node], two_days_on.date(), two_days_on)


def test_a_bash_append_to_a_letters_file_gets_its_letter(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8").replace(
        '    paths: ["src/reducer.py"]\n', '    paths: ["src/reducer.py"]\n    tools: [Write, Edit, Bash]\n'),
        encoding="utf-8")
    bash = lambda cmd, s: {"session_id": s, "hook_event_name": "PreToolUse", "tool_name": "Bash",  # noqa: E731
                           "cwd": str(repo.root), "tool_input": {"command": cmd}}
    hit = wait_gate.handle(bash("cat >> src/reducer.py <<'EOF'\nx\nEOF", "b1"))
    assert "parity" in hit["hookSpecificOutput"]["additionalContext"]
    assert wait_gate.handle(bash("ls src/ | wc -l", "b2")) is None          # no redirect
    assert wait_gate.handle(bash("echo x > /dev/null", "b3")) is None       # outside the project
    assert wait_gate.handle(bash("tee -a src/cors.py < x", "b4")) is None   # its letter has no Bash


def test_the_time_line_only_on_startup(repo):
    tm.project(repo.root)
    startup = tm.brief_text(repo.root, source="startup")
    assert "since the last session" in startup
    resumed = tm.brief_text(repo.root, source="resume")
    assert "since the last session" not in resumed and "resumed" in resumed
    compacted = tm.brief_text(repo.root, source="compact")
    assert "⏳" not in compacted and "parity" in compacted          # letters come back


def test_a_rename_fails_the_change_that_made_the_letter_stale(repo):
    before = repo.git("rev-parse", "HEAD")
    repo.git("mv", "src/cors.py", "src/cors_policy.py")
    repo.commit("refactor: rename cors (#20)", {})
    root = str(repo.root)
    assert tm.main(["tm", "--root", root, "check", "--stale-only", "--base", before]) == 1
    # the next, unrelated change sees the staleness as somebody else's: a warning
    after = repo.git("rev-parse", "HEAD")
    repo.commit("feat: unrelated (#21)", {"src/other.py": "z"})
    assert tm.main(["tm", "--root", root, "check", "--stale-only", "--base", after]) == 0
    assert tm.main(["tm", "--root", root, "check", "--base", after]) == 0
    # with no base at all (a local run) it is an error again
    assert tm.main(["tm", "--root", root, "check"]) == 1


def test_an_unhashable_key_is_a_yaml_error_not_a_crash(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8") + "? [a, b]\n: 1\n", encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_without_pyyaml_the_hooks_exit_3_so_the_wrapper_tries_another_python(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert tm.cmd_warn() == 3
    assert tm.cmd_brief() == 3


def test_warn_needs_no_git(repo, monkeypatch):
    """`warn` runs on every edit: it must not shell out to git at all."""
    calls = []
    real = tm._git_in
    monkeypatch.setattr(tm, "_git_in", lambda *a: calls.append(a) or real(*a))
    tm.warn_text(pre_tool(repo.root, "src/reducer.py", session="nogit"), repo.root)
    assert calls == []


# ---------------------------------------------------------------- review of ruti#52/#53


def test_the_hooks_find_the_letters_from_a_subdirectory(repo):
    (repo.root / "src/deep").mkdir()
    sub = {"session_id": "s1", "cwd": str(repo.root / "src/deep")}
    assert session_start.time_machine(sub) is not None             # `cd src/deep` earlier
    edit = {**pre_tool(repo.root, "src/reducer.py"), "cwd": str(repo.root / "src/deep")}
    assert "parity" in wait_gate.handle(edit)["hookSpecificOutput"]["additionalContext"]


def test_an_edit_in_a_checkout_under_the_session_gets_that_checkouts_letters(repo, tmp_path):
    edit = {**pre_tool(repo.root, "src/reducer.py"), "cwd": str(tmp_path)}  # repos side by side
    assert "parity" in wait_gate.handle(edit)["hookSpecificOutput"]["additionalContext"]


def test_a_checkout_outside_the_session_has_no_say(repo, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    edit = {**pre_tool(repo.root, "src/reducer.py", session="o1"), "cwd": str(other)}
    assert wait_gate.handle(edit) is None


def test_a_ref_from_the_letters_file_is_never_a_git_option(repo, tmp_path):
    out = tmp_path / "written"
    letters = repo.root / "docs/future-letters.yaml"
    text = re.sub(r"  last: \w+\n", lambda _m: f"  last: '--output={out}3'\n",  # Windows paths: no escapes
                  letters.read_text(encoding="utf-8"))
    letters.write_text(text.replace("  goal_node: launch\n", f"  goal_node: launch\n  main_ref: '--output={out}1'\n"
                                    f"  activity_refs: ['--output={out}2']\n"), encoding="utf-8")
    assert session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)}) is not None
    tm.project(repo.root)
    tm.expedition(tm.load_letters())
    assert tm.commit_files(f"--output={out}4") == []
    assert not any(tmp_path.glob("written*"))


def test_ruti_never_runs_a_checkouts_graph_module(repo):
    marker = repo.root / "ran"
    (repo.root / "graph.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n"
        "def load_nodes(): return {}\n"
        "def ready_front(by_id): return [], [], []\n", encoding="utf-8")
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8").replace(
        "  goal_node: launch\n", "  goal_node: launch\n  graph_module: graph.py\n"), encoding="utf-8")
    result = session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)})
    assert not marker.exists()
    assert "cors-open ⇐ set-origins" in result[1]        # the graph came from tasks.yaml


def test_an_allow_rule_is_not_a_hook(repo):
    local = repo.root / ".claude/settings.local.json"
    local.parent.mkdir()
    local.write_text(json.dumps({"permissions": {"allow": [
        "Bash(python3 scripts/time-machine/tm.py:*)", "Bash(bash .claude/hooks/time-machine.sh:*)"]}}))
    assert not tm.Project(repo.root).self_wired()
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py")) is not None
    local.write_text("{not json")
    assert not tm.Project(repo.root).self_wired()


@pytest.mark.parametrize("touch,glob,hit", [
    ("app/lib/", "**/pubspec.yaml", False),       # one file name anywhere: narrower
    ("app/lib/", "app/**/main.dart", False),
    ("app/lib/", "app/**/*.dart", True),          # every .dart under it
    ("app/lib/", "app/**", True),
    ("app/lib/", "**", True),
])
def test_a_file_name_after_double_star_is_narrower_than_a_directory(touch, glob, hit):
    assert tm.touches_overlap(touch, glob) is hit


def test_one_malformed_letter_does_not_silence_the_others(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8") + """  - id: broken
    title: Broken
    class: ci
    severity: critical
    paths: ["src/reducer.py"]
    warning: x
    precaution: y
    evidence: [{pr: 3, date: 2026-09-03}]
    guard: ci
  - title: No id at all
    severity: high
    paths: ["src/reducer.py"]
""", encoding="utf-8")
    assert "parity" in wait_gate.handle(pre_tool(repo.root, "src/reducer.py"))[
        "hookSpecificOutput"]["additionalContext"]
    _, model = session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)})
    assert "parity" in model and "malformed: 2" in model
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_an_unparsable_letters_file_says_so_in_the_brief(repo):
    (repo.root / "docs/future-letters.yaml").write_text("letters: [\n", encoding="utf-8")
    _, model = session_start.time_machine({"session_id": "s1", "cwd": str(repo.root)})
    assert "does not parse" in model


def test_mark_finds_a_place_without_a_plain_version_line(repo):
    letters = repo.root / "docs/future-letters.yaml"
    text = letters.read_text(encoding="utf-8")
    start, end = text.index("expedition:"), text.index("letters:")
    letters.write_text(text[:start].replace("version: 1\n", 'version: "1"  # schema\n') + text[end:],
                       encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "expedition", "--mark"]) == 0
    tm.project(repo.root)
    assert tm.expedition_marker()["last"] == repo.git("rev-parse", "HEAD")


def test_a_mark_that_cannot_be_written_fails_and_leaves_the_file(repo, capsys):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text("{version: 1, letters: []}\n", encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "expedition", "--mark"]) == 1
    assert letters.read_text(encoding="utf-8") == "{version: 1, letters: []}\n"
    assert "nothing written" in capsys.readouterr().err


def test_the_hook_parses_the_letters_once(repo, monkeypatch):
    built = []
    real = tm.Project.__init__
    monkeypatch.setattr(tm.Project, "__init__", lambda self, root: built.append(root) or real(self, root))
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py")) is not None
    assert len(built) == 1


@pytest.mark.parametrize("command,targets", [
    ("make 2>&1 | tee --append docs/tasks.yaml", ["docs/tasks.yaml"]),
    ("make | tee -ai out.log", ["out.log"]),
    ('echo x > "my file.txt"', ["my file.txt"]),
    ("echo x >> 'a b'", ["a b"]),
    ("cat <<'EOF' > src/x.py", ["src/x.py"]),
    ("echo committee guarantee", []),
    ("ls 2>&1", []),
    ("echo x | tee --output-error=warn docs/future-letters.yaml", ["docs/future-letters.yaml"]),
    ("bash scripts/tee.sh", []),
])
def test_redirect_targets(command, targets):
    assert tm.redirect_targets(command) == targets


def test_a_bash_command_mentioning_committee_loads_no_letters(repo, monkeypatch):
    monkeypatch.setattr(tm, "Project", lambda *a: pytest.fail("letters loaded"))
    bash = {"session_id": "c1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "cwd": str(repo.root), "tool_input": {"command": "echo committee"}}
    assert wait_gate.handle(bash) is None


def test_the_expedition_cap_is_a_cap(repo):
    for i in range(5):
        repo.commit(f"fix: thing {i} (#{30 + i})", {f"src/f{i}.py": "x"})
    tm.project(repo.root)
    rep = tm.expedition(tm.load_letters(), cap=3)
    assert rep["fixes"] == 3 and rep["capped"]
    assert tm.expedition(tm.load_letters())["capped"] is False


def test_the_expedition_reads_a_merge_commits_files(repo):
    repo.git("checkout", "-q", "-b", "topic")
    repo.commit("work", {"src/reducer.py": "m"})
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "topic", "-m", "fix(reducer): drifted in a merge (#40)")
    tm.project(repo.root)
    rep = tm.expedition(tm.load_letters())
    assert [(i["pr"], i["letters"]) for i in rep["evidence"]] == [(40, ["parity"])]


def test_check_reports_a_list_id_instead_of_crashing(repo):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8").replace("  - id: parity\n", "  - id: [parity]\n"),
                       encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1


def test_a_slow_time_machine_does_not_cost_the_session_start(monkeypatch, capsys):
    import io
    import time as _time

    from ruti import flow

    monkeypatch.setattr(session_start, "TIME_MACHINE_DEADLINE", 0.2)
    monkeypatch.setattr(session_start, "_mark_start", lambda: None)
    monkeypatch.setattr(session_start, "build_report", lambda: None)
    monkeypatch.setattr(flow, "session_start", lambda payload: ("continued", "the handoff"))
    monkeypatch.setattr(session_start, "time_machine", lambda payload: _time.sleep(2) or ("late", "late"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    started = _time.monotonic()
    assert session_start.main() == 0
    assert _time.monotonic() - started < 1.5
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] == "the handoff"


# ---------------------------------------------------------------- review of ruti#54


def test_mark_replaces_a_marker_block_with_a_blank_line_inside(repo):
    letters = repo.root / "docs/future-letters.yaml"
    text = letters.read_text(encoding="utf-8").replace("  date: 2026-10-01\n", "\n  date: 2026-10-01\n")
    letters.write_text(text, encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "expedition", "--mark"]) == 0
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 0      # no duplicate `date`
    tm.project(repo.root)
    assert str(tm.expedition_marker()["date"]) != "2026-10-01"


def test_mark_handles_a_marker_at_the_end_without_a_newline(repo):
    letters = repo.root / "docs/future-letters.yaml"
    text = letters.read_text(encoding="utf-8")
    start, end = text.index("expedition:"), text.index("letters:")
    letters.write_text(text[:start] + text[end:].rstrip("\n") + "\n" + text[start:end].rstrip("\n"),
                       encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "expedition", "--mark"]) == 0
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 0


def test_check_names_a_letter_the_hooks_would_skip(repo, capsys):
    letters = repo.root / "docs/future-letters.yaml"
    base = letters.read_text(encoding="utf-8")
    letters.write_text(base.replace("    warning: The two reducers disagreed twice.\n",
                                    "    warning:\n      - The two reducers\n      - disagreed twice.\n"),
                       encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1
    assert "wrong type" in capsys.readouterr().out
    letters.write_text(base.replace('    paths: ["src/reducer.py"]\n', "    paths: [1]\n"), encoding="utf-8")
    assert tm.main(["tm", "--root", str(repo.root), "check"]) == 1      # named, not a crash


def test_letters_of_a_monorepo_package(repo, monkeypatch):
    pkg = repo.root / "packages/app"
    (pkg / "docs").mkdir(parents=True)
    (pkg / "docs/future-letters.yaml").write_text(
        (repo.root / "docs/future-letters.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (pkg / "src").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(pkg))
    assert tm.session_root({"cwd": str(pkg)}) == pkg.resolve()
    edit = pre_tool(pkg, "src/reducer.py", session="mono")
    assert "parity" in wait_gate.handle(edit)["hookSpecificOutput"]["additionalContext"]


def test_a_nested_checkouts_own_hook_does_not_run_so_ruti_delivers(repo, tmp_path):
    settings = repo.root / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "bash .claude/hooks/time-machine.sh warn"}]}]}}))
    edit = {**pre_tool(repo.root, "src/reducer.py", session="n1"), "cwd": str(tmp_path)}
    assert "parity" in wait_gate.handle(edit)["hookSpecificOutput"]["additionalContext"]
    assert wait_gate.handle(pre_tool(repo.root, "src/reducer.py", session="n2")) is None


def test_a_cd_into_a_worktree_keeps_the_session_project(repo, monkeypatch):
    worktree = repo.root / ".claude/worktrees/x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.root))
    edit = {**pre_tool(repo.root, "src/reducer.py", session="w1"), "cwd": str(worktree)}
    assert "parity" in wait_gate.handle(edit)["hookSpecificOutput"]["additionalContext"]


def test_a_bash_redirect_into_a_nested_checkout_gets_its_letters(repo, tmp_path):
    letters = repo.root / "docs/future-letters.yaml"
    letters.write_text(letters.read_text(encoding="utf-8").replace(
        '    paths: ["src/reducer.py"]\n', '    paths: ["src/reducer.py"]\n    tools: [Write, Edit, Bash]\n'),
        encoding="utf-8")
    bash = {"session_id": "nb", "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(tmp_path),
            "tool_input": {"command": f"cat > {repo.root / 'src/reducer.py'} <<'EOF'\nx\nEOF"}}
    assert "parity" in wait_gate.handle(bash)["hookSpecificOutput"]["additionalContext"]


def test_a_git_timeout_is_not_nothing_new(repo, monkeypatch, capsys):
    real = tm._git_in

    def slow(root, *args, **kw):
        if kw.get("strict"):
            raise subprocess.TimeoutExpired("git", kw.get("timeout", 10))
        return real(root, *args, **kw)

    monkeypatch.setattr(tm, "_git_in", slow)
    assert tm.main(["tm", "--root", str(repo.root), "expedition"]) == 1
    assert "timed out" in capsys.readouterr().err
    tm.project(repo.root)
    assert any("did not finish" in line for line in tm.brief_extras(tm.load_letters()))

"""Exercise the wrapper with real Git and overlapping fake OpenCode processes."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ruti import delegate, proc, runs, usage


def test_overlapping_same_alias_runs_have_independent_files_markers_and_usage(tmp_path, monkeypatch):
    real_run = proc.run
    first_finished = threading.Event()
    both_started = threading.Barrier(2, timeout=15)
    write_lock = threading.Lock()

    def fake_opencode(argv, **kwargs):
        if argv[0] != "fake-opencode":
            return real_run(argv, **kwargs)
        config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        run_id = config["provider"]["ruti-router"]["options"]["headers"]["x-ruti-run-id"]
        directory = kwargs["cwd"]
        with write_lock:
            with usage.USAGE_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"run_id": run_id, "requested": "free",
                                        "at": time.time(), "model": directory.name,
                                        "cost_usd": 0.1}) + "\n")
        both_started.wait()
        assert len(runs.active(delegate.RUNNING_FILE)) == 2
        both_started.wait()
        if directory.name == "first":
            return proc.Result(argv, 0, "first completed", "", 0)
        assert first_finished.wait(15)
        assert {r["run_id"] for r in runs.active(delegate.RUNNING_FILE)} == {run_id}
        return proc.Result(argv, 0, "second completed", "", 0)

    monkeypatch.setattr(delegate, "_opencode_executable", lambda: "fake-opencode")
    monkeypatch.setattr(proc, "run", fake_opencode)
    monkeypatch.setattr(usage, "_settle", lambda *a: None)
    directories = [tmp_path / "first", tmp_path / "second"]
    for directory in directories:
        directory.mkdir()
        real_run(["git", "init", "--quiet", str(directory)]).check()

    def work(directory):
        try:
            return delegate.run("brief", model="ruti-router/free", directory=directory,
                                check_substitution=False, timeout=30)
        finally:
            if directory.name == "first":
                first_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(work, directories))
    assert first.ok and second.ok
    assert first.run_id != second.run_id
    assert first.log_path != second.log_path
    assert first.model_effective == "first" and second.model_effective == "second"
    assert first.usage.requests == second.usage.requests == 1
    assert first.usage.cost_usd == second.usage.cost_usd == 0.1
    assert len(list(delegate.LOG_DIR.glob("task-*.md"))) == 2
    assert runs.active(delegate.RUNNING_FILE) == []


def test_edit_to_already_dirty_file_is_reported(tmp_path, monkeypatch):
    real_run = proc.run
    tmp_path = tmp_path / "checkout"
    tmp_path.mkdir()
    real_run(["git", "init", "--quiet", str(tmp_path)]).check()
    path = tmp_path / "code.txt"
    path.write_text("original\n", encoding="utf-8")
    real_run(["git", "add", "code.txt"], cwd=tmp_path).check()
    real_run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
              "commit", "--quiet", "-m", "baseline"], cwd=tmp_path).check()
    path.write_text("user edit\n", encoding="utf-8")
    before = real_run(["git", "status", "--porcelain"], cwd=tmp_path).stdout

    def fake_opencode(argv, **kwargs):
        if argv[0] != "fake-opencode":
            return real_run(argv, **kwargs)
        path.write_text("delegate edit after user edit\n", encoding="utf-8")
        return proc.Result(argv, 0, "done", "", 0)

    monkeypatch.setattr(delegate, "_opencode_executable", lambda: "fake-opencode")
    monkeypatch.setattr(proc, "run", fake_opencode)
    monkeypatch.setattr(usage, "_settle", lambda *a: None)
    result = delegate.run("edit file", model="ruti-router/free", directory=tmp_path,
                          check_substitution=False)
    after = real_run(["git", "status", "--porcelain"], cwd=tmp_path).stdout
    assert before == after
    assert result.ok and result.files_changed == ["code.txt"]

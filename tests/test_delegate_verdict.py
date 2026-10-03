"""A delegate is given enough time to finish, and a run cut short is neither a success
nor a failure until someone has read the diff.

Two failures met here. `route` hands out a command without a timeout although it knows
the task's size, so the flat 900s default killed 150-300 line changes that were on their
last step. And such a run was recorded `ok: false` and counted by `track` as a failure,
so an alias lost ranking for work the manager went on to use.

The fix is a timeout sized to the task, and a verdict: the manager's review of what
became of the run, which is the one thing the ledger cannot work out for itself.
"""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from ruti import (cli, delegate, ledger, litellm_cfg, lmstudio, modes, proc, quota,
                  router, sessions, track, usage)

ALIAS = "ruti-router/inkling-small"
REGISTRY = [{"alias": "inkling-small", "model": "openrouter/thinkingmachines/inkling-small:free",
             "router": False, "free": True}]


# --------------------------------------------------------------- the sized timeout


@pytest.mark.parametrize("loc, files, expected", [
    (20, 1, 600),      # under the floor: 300 + 60 + 60 = 420
    (200, 5, 1200),    # 300 + 600 + 300
    (2000, 30, 2700),  # 8100, over the ceiling
])
def test_the_timeout_is_sized_to_the_task(loc, files, expected):
    assert router.suggested_timeout(router.Task(loc=loc, files=files)) == expected


def test_the_timeout_is_always_a_whole_number_of_minutes():
    # 903 seconds is an estimate nobody made; a budget has to be something a person can
    # read back and compare against the clock.
    for loc in range(0, 600, 37):
        seconds = router.suggested_timeout(router.Task(loc=loc, files=4))
        assert seconds % 60 == 0


def test_a_negative_size_is_not_shorter_than_the_floor():
    assert router.suggested_timeout(router.Task(loc=-100, files=-5)) == 600


@pytest.fixture
def ranking(registry, monkeypatch):
    """`router.rank` with local models gone and the four registered aliases served."""
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models",
                        lambda: [r["alias"] for r in registry])
    monkeypatch.setattr(modes, "current", lambda _sid: {"coding": False, "free": "off"})
    snapshot = quota.Quota(five_hour=quota.Window(10.0, time.time() + 3 * 3600),
                           seven_day=None, captured_at=time.time())
    assert snapshot.band == quota.GREEN

    def rank(task: router.Task | None = None, *, track_records=None):
        if track_records is not None:
            monkeypatch.setattr(track, "load", lambda: track_records)
        return router.rank(task or router.Task(kind="implement", files=5, loc=200),
                           snapshot, record=False)

    return rank


def test_routes_command_carries_the_sized_timeout(ranking):
    # The complaint the whole change answers: `route` knew this was 200 lines across
    # five files and said nothing about how long the delegate was allowed to take.
    result = ranking()
    for entry in result["ranked"] + result["rejected"]:
        if entry["command"].startswith("ruti delegate"):
            assert "--timeout 1200" in entry["command"], entry["command"]
            assert entry["command"].endswith("--task-file <file>")
    assert result["task"]["timeout_s"] == 1200


def test_a_bigger_task_gets_a_bigger_timeout(ranking):
    small = ranking(router.Task(kind="implement", files=1, loc=20))
    large = ranking(router.Task(kind="implement", files=30, loc=2000))
    assert small["task"]["timeout_s"] == 600 and large["task"]["timeout_s"] == 2700
    assert any("--timeout 600" in e["command"] for e in small["ranked"] + small["rejected"])
    assert any("--timeout 2700" in e["command"] for e in large["ranked"] + large["rejected"])


# ------------------------------------------------------------------------ the run


def test_a_summary_names_the_run_and_whether_it_was_cut_short():
    outcome = delegate.Outcome(model_requested="ruti-router/inkling-small",
                               exit_code=0, run_id="ab12cd34")
    summary = outcome.summary()
    assert summary["run_id"] == "ab12cd34"
    assert summary["timed_out"] is False
    # No next step for a run that finished: there is nothing to review but the diff.
    assert "next" not in summary


def test_a_timed_out_run_says_what_to_do_next():
    outcome = delegate.Outcome(model_requested="ruti-router/inkling-small",
                               exit_code=-1, error="timed out after 1200s",
                               run_id="ab12cd34", timed_out=True)
    summary = outcome.summary()
    assert summary["timed_out"] is True
    assert summary["next"] == ("timed out -- review the diff, then record the result: "
                               "ruti verdict ok|bad --run ab12cd34")
    # Still not ok: a timed-out run has to be looked at, and `ok` is what makes a
    # summary read as finished.
    assert summary["ok"] is False


def test_a_real_timeout_marks_the_run_and_names_it(monkeypatch, tmp_path):
    # The whole chain for the case this exists for: `opencode` is killed by its own
    # budget, so the run is marked, gets an id, and lands in the ledger for `track` to
    # find -- and the summary says what to do about it.
    def timeout(argv, **kwargs):
        if argv[0] == "git":
            return proc.Result(argv, 1, "", "not a repository", 0.0)
        raise proc.ToolTimeout("opencode exceeded its 1200s timeout", stdout="writing...")

    monkeypatch.setattr(delegate.proc, "run", timeout)
    monkeypatch.setattr(usage, "SETTLE_SECONDS", 0)
    outcome = delegate.run("write it", model="ruti-router/inkling-small",
                           directory=tmp_path, timeout=1200, check_substitution=False)
    assert outcome.timed_out is True and outcome.ok is False
    assert outcome.run_id and len(outcome.run_id) == 8
    assert outcome.summary()["next"].endswith(f"--run {outcome.run_id}")

    event = [e for e in ledger.read_events() if e["event"] == "delegation"][-1]
    assert (event["run_id"], event["timed_out"], event["ok"]) == (
        outcome.run_id, True, False)


# ------------------------------------------------------------ the track record


def _record(events):
    return track.records(events, REGISTRY)[ALIAS]


def test_a_timed_out_run_counts_for_nothing_until_it_is_reviewed():
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3,
                  run_id="r1", timed_out=True, duration_s=900)
    record = _record(ledger.read_events())
    # Not a failure: the manager may go on to use what the delegate wrote.
    assert (record.runs, record.succeeded, record.excluded) == (0, 0, 0)
    # Not a success either, and not invisible: the run is held, visibly.
    assert record.timed_out == 1
    assert record.factor == 1.0 and record.speed is None


def test_verdict_ok_is_what_the_manager_used():
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3,
                  run_id="r1", timed_out=True, duration_s=900)
    ledger.record("verdict", run_id="r1", verdict="ok", model=ALIAS)
    record = _record(ledger.read_events())
    assert (record.runs, record.succeeded) == (1, 1)
    assert record.timed_out == 0
    assert record.factor > 1.0


def test_the_last_verdict_stands():
    # A diff read twice is normal: the first look said it was fine, the second found
    # the part that had to be rewritten. The last word is the one that counts.
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3,
                  run_id="r1", timed_out=True, duration_s=900)
    ledger.record("verdict", run_id="r1", verdict="ok", model=ALIAS)
    assert _record(ledger.read_events()).succeeded == 1
    ledger.record("verdict", run_id="r1", verdict="bad", model=ALIAS)
    record = _record(ledger.read_events())
    assert (record.runs, record.succeeded) == (1, 0)
    assert record.factor < 1.0


def test_a_run_with_no_id_is_counted_exactly_as_before():
    # Ledger lines written before `run_id` existed have to behave as they always did,
    # or every ranking on this machine would change retroactively.
    ledger.record("delegation", model=ALIAS, ok=True, files_changed=2, duration_s=45.0)
    record = _record(ledger.read_events())
    assert (record.runs, record.succeeded, record.timed_out) == (1, 1, 0)

    ledger.record("delegation", model=ALIAS, ok=False, files_changed=0, duration_s=45.0)
    record = _record(ledger.read_events())
    assert (record.runs, record.succeeded, record.timed_out) == (2, 1, 0)


def test_a_verdict_on_another_runs_id_touches_nothing():
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3,
                  run_id="r1", timed_out=True, duration_s=900)
    ledger.record("verdict", run_id="r-other", verdict="ok", model=ALIAS)
    record = _record(ledger.read_events())
    assert (record.runs, record.timed_out) == (0, 1)


def test_route_shows_the_held_runs_rather_than_saying_nothing(ranking):
    # Otherwise an alias whose runs all timed out reads as never asked, and the manager
    # is told nothing about the three hours that went into finding that out.
    result = ranking(track_records={ALIAS: track.Record(timed_out=3)})
    entry = next(e for e in result["ranked"] + result["rejected"] if e["executor"] == ALIAS)
    assert entry["track"]["timed_out"] == 3 and entry["track"]["runs"] == 0
    assert any("3 past run(s) hit their timeout" in reason for reason in entry["reasons"])


# ------------------------------------------------------------------------- the CLI


def _invoke(args):
    result = CliRunner().invoke(cli.main, args)
    return result, result.output + (result.stderr if hasattr(result, "stderr") else "")


@pytest.fixture
def session(monkeypatch):
    """A session id the ledger also records, so `verdict` can find this session's runs."""
    monkeypatch.setattr(sessions, "current_session_id", lambda: "s1")
    return "s1"


def test_with_nothing_to_review_it_says_so(session):
    result, text = _invoke(["verdict", "ok"])
    assert result.exit_code != 0
    assert "no delegation in this session" in text and "--run" in text


def test_the_latest_run_of_the_session_is_the_one_reviewed(session):
    ledger.record("delegation", model="ruti-router/free", ok=True, files_changed=1,
                  run_id="older")
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=2,
                  run_id="r1", timed_out=True, duration_s=1200)
    result, text = _invoke(["verdict", "ok", "--note", "finished it by hand"])
    assert result.exit_code == 0, text
    assert "verdict ok for ruti-router/inkling-small run r1" in " ".join(text.split())

    event = [e for e in ledger.read_events() if e["event"] == "verdict"][-1]
    assert (event["run_id"], event["verdict"], event["model"]) == (
        "r1", "ok", "ruti-router/inkling-small")
    assert event["note"] == "finished it by hand"


def test_a_verdict_json_is_the_recorded_fields(session):
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=2, run_id="r1",
                  timed_out=True, duration_s=1200)
    result, _ = _invoke(["verdict", "bad", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"run_id": "r1", "verdict": "bad", "model": ALIAS}


def test_another_sessions_run_is_not_this_ones(session):
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=2, run_id="r1",
                  timed_out=True, duration_s=1200, session="someone-else")
    result, text = _invoke(["verdict", "ok"])
    assert result.exit_code != 0 and "no delegation in this session" in text


def test_run_finds_a_run_from_another_session_or_another_hour(session):
    # `--run` is what a verdict looks like when it is given after the fact, which is
    # when a review of a big diff actually happens.
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=2, run_id="r1",
                  timed_out=True, duration_s=1200, session="a-session-that-ended")
    result, text = _invoke(["verdict", "bad", "--run", "r1"])
    assert result.exit_code == 0, text
    assert [e["run_id"] for e in ledger.read_events() if e["event"] == "verdict"] == ["r1"]


def test_an_unknown_run_id_is_refused(session):
    result, text = _invoke(["verdict", "ok", "--run", "nope"])
    assert result.exit_code != 0
    assert "nope" in text
    assert not [e for e in ledger.read_events() if e["event"] == "verdict"]


def test_the_verdict_decides_what_the_track_record_says(session):
    # The wiring end to end: a real `delegate` event, a real `verdict` command, and the
    # ranking reading back what the manager decided.
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3, run_id="r1",
                  timed_out=True, duration_s=1200)
    assert track.load()[ALIAS].runs == 0 and track.load()[ALIAS].timed_out == 1
    assert _invoke(["verdict", "ok"])[0].exit_code == 0
    record = track.load()[ALIAS]
    assert (record.runs, record.succeeded, record.timed_out) == (1, 1, 0)


def test_at_names_a_run_logged_before_run_id_existed(session):
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3,
                  duration_s=900, session="old")
    at = next(e["at"] for e in ledger.read_events() if e["event"] == "delegation")
    assert track.load()[ALIAS].succeeded == 0
    result, text = _invoke(["verdict", "ok", "--at", str(round(at, 1))])
    assert result.exit_code == 0, text
    event = [e for e in ledger.read_events() if e["event"] == "verdict"][-1]
    assert event["at_ref"] == at and "run_id" not in event
    record = track.load()[ALIAS]
    assert (record.runs, record.succeeded) == (1, 1)


def test_at_with_nothing_near_it_is_refused(session):
    ledger.record("delegation", model=ALIAS, ok=False, files_changed=3, duration_s=900)
    result, text = _invoke(["verdict", "ok", "--at", "12345"])
    assert result.exit_code != 0 and "12345" in text
    assert not [e for e in ledger.read_events() if e["event"] == "verdict"]


def test_at_and_run_together_are_refused(session):
    result, text = _invoke(["verdict", "ok", "--at", "1", "--run", "r1"])
    assert result.exit_code != 0 and "not both" in text

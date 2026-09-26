"""An executor should be ranked on what it has actually done.

The constants every remote model is given are equal, so without history they all score
0.9 and the order says nothing. These tests are about the history the ledger already
holds becoming that score -- and, just as much, about a run another model answered never
being counted against the alias that was asked for.
"""

from __future__ import annotations

import time

import pytest

from ruti import ledger, litellm_cfg, lmstudio, modes, quota, router, track

NOW = 1_790_400_000.0


def delegation(alias: str, *, at: float = NOW, ok: bool = True, files: int = 1,
               duration: float = 120.0, substituted: bool = False,
               effective: str | None = None, is_router: bool = False) -> dict:
    """One ledger delegation event, as `delegate._record` writes it."""
    return {
        "event": "delegation", "at": at, "model": alias, "ok": ok,
        "substituted": substituted, "model_effective": effective,
        "router": is_router, "duration_s": duration, "files_changed": files,
    }


# A registry as `providers.load_registry()` hands it over: aliases, not executor names.
LAGUNA = {"alias": "laguna-s-2.1", "model": "openrouter/poolside/laguna-s-2.1:free",
          "router": False, "free": True}
PARETO = {"alias": "pareto-code", "model": "openrouter/openrouter/pareto-code",
          "router": True, "free": False}


# ------------------------------------------------------------------------ the math


def test_no_runs_leaves_a_model_exactly_where_it_started():
    # An alias nobody has asked is not a slow model, it is an unknown one: the factor
    # must be 1.0 and there is no speed to substitute for the default 0.75.
    record = track.Record()
    assert record.runs == 0 and record.excluded == 0
    assert record.factor == 1.0
    assert record.speed is None
    assert record.reliability == 0.5


def test_one_good_run_is_a_bonus_not_a_verdict():
    record = track.Record(runs=1, succeeded=1, median_s=300.0)
    assert round(record.factor, 3) == 1.133
    # 300s of a 900s budget: 0.667, the speed scale the fixed 0.75 also uses.
    assert record.speed == pytest.approx(1 - 300 / 900, abs=1e-9)


def test_two_failures_demote():
    record = track.Record(runs=2, succeeded=0)
    assert record.factor == 0.8
    # Nothing succeeded, so there is no measured speed -- and that is not speed 0.75.
    assert record.speed is None


def test_the_factor_is_bounded_at_both_ends():
    # A flawless record is worth a fifth more than nothing, no more: with this many runs
    # the raw reliability would otherwise multiply the score by 1.4.
    assert track.Record(runs=40, succeeded=40, median_s=1.0).factor == 1.2
    # And a model that has failed everything is demoted rather than ruled out, so one bad
    # week cannot delete a model from the ranking. The floor is never actually reached --
    # the smoothing always keeps one failure in reserve -- but the demotion is real: the
    # worst record the ledger can hold scores a third below a model nobody has asked.
    worst = track.Record(runs=track.MAX_RUNS, succeeded=0)
    assert 0.6 <= worst.factor < 1.0
    assert worst.factor == pytest.approx(0.6 + 0.8 / (track.MAX_RUNS + 2))



def test_a_fast_median_is_capped_at_the_top_of_the_scale():
    # 42s is already faster than anything the constants claim; without the cap one
    # quick run would outrank claude:self on a measure of seconds.
    assert track.Record(runs=1, succeeded=1, median_s=42.0).speed == 0.95
    # A run that used the whole timeout gets the floor rather than zero.
    assert track.Record(runs=1, succeeded=1, median_s=track.TIMEOUT_S).speed == 0.3


# ------------------------------------------------------------- what counts as a run


def test_a_run_another_model_answered_is_not_evidence_about_the_alias():
    # The proxy answered as a different group, so `laguna-s-2.1` did no work at all.
    substituted = delegation("ruti-router/laguna-s-2.1", substituted=True)
    out = track.records([substituted], [LAGUNA])
    record = out["ruti-router/laguna-s-2.1"]
    assert (record.runs, record.succeeded, record.excluded) == (0, 0, 1)
    assert record.factor == 1.0 and record.speed is None


def test_a_quietly_answered_run_is_caught_by_the_model_name():
    # `substituted` false, but the response named Gemini: the ledger's honest report of
    # a request `laguna-s-2.1` never got to answer.
    served_by_another = delegation(
        "ruti-router/laguna-s-2.1", effective="gemini/gemini-2.5-flash"
    )
    record = track.records([served_by_another], [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert record.runs == 0 and record.excluded == 1


def test_a_router_is_never_excluded_for_picking_another_model():
    # That is what a router is for, so its picks are not a model record either way --
    # and must not count against it as though it had failed.
    run = delegation("ruti-router/pareto-code", ok=True,
                     effective="anthropic/claude-opus-4.6", is_router=True)
    record = track.records([run], [PARETO])["ruti-router/pareto-code"]
    assert record.runs == 1 and record.succeeded == 1 and record.excluded == 0


def test_an_unknown_effective_model_is_not_treated_as_a_mismatch():
    # `usage.py` refuses to fill a model name with the alias, so "unknown" means ruti
    # could not find out -- which is not evidence that some other model did the work.
    run = delegation("ruti-router/laguna-s-2.1", effective="unknown")
    record = track.records([run], [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert record.runs == 1 and record.excluded == 0


def test_the_same_model_under_another_prefix_is_its_own_alias():
    # The ledger holds whatever name the provider's response carried.
    run = delegation("ruti-router/laguna-s-2.1", effective="poolside/laguna-s-2.1:free")
    record = track.records([run], [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert record.runs == 1 and record.excluded == 0


def test_an_alias_with_no_registry_record_is_trusted():
    # config.yaml entries are routable and appear in no registry; nothing here knows
    # what they were meant to run, so nothing here may judge them.
    run = delegation("ruti-router/some-config-only", effective="gemini/gemini-2.5-flash")
    assert track.records([run], [])["ruti-router/some-config-only"].runs == 1


def test_ok_with_nothing_written_is_a_failure():
    # Exit 0 and no changed file is the run that cost the manager the work twice: the
    # delegate's own words in the log, and the file still unwritten.
    run = delegation("ruti-router/laguna-s-2.1", ok=True, files=0, duration=90.0)
    record = track.records([run], [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert (record.runs, record.succeeded, record.excluded) == (1, 0, 0)
    assert record.factor < 1.0 and record.speed is None


def test_the_median_is_over_successful_runs_only():
    events = [
        delegation("ruti-router/laguna-s-2.1", at=NOW, duration=100.0, files=0),  # failed
        delegation("ruti-router/laguna-s-2.1", at=NOW + 1, duration=200.0),
        delegation("ruti-router/laguna-s-2.1", at=NOW + 2, duration=300.0),
    ]
    record = track.records(events, [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert record.runs == 3 and record.succeeded == 2
    # The 100s run failed and changed nothing, so it is not evidence of speed: the
    # median is over the two runs that did the work.
    assert record.median_s == 250.0


def test_only_the_most_recent_runs_are_kept():
    # A model that was broken last month is not evidence about today, so the window
    # slides: the older failures must fall out of the record, not sit in it forever.
    events = [delegation("ruti-router/laguna-s-2.1", at=NOW, ok=False, files=0)]
    events += [
        delegation("ruti-router/laguna-s-2.1", at=NOW + 100 + i, duration=60.0)
        for i in range(track.MAX_RUNS + 5)
    ]
    record = track.records(events, [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert record.runs == track.MAX_RUNS
    assert record.succeeded == track.MAX_RUNS
    assert record.factor == 1.2


def test_malformed_events_are_skipped_rather_than_counted():
    events = [
        "not a dict",
        {"event": "route", "at": NOW, "model": "ruti-router/laguna-s-2.1"},  # other event
        {"event": "delegation", "at": NOW, "ok": True, "files_changed": 1},  # no model
        {"event": "delegation", "model": "ruti-router/laguna-s-2.1", "ok": True,
         "files_changed": 1},  # no timestamp
        {"event": "delegation", "at": "later", "model": "ruti-router/laguna-s-2.1",
         "ok": True, "files_changed": 1},  # timestamp is not a number
        {"event": "delegation", "at": NOW, "model": "ruti-router/laguna-s-2.1",
         "ok": "yes", "files_changed": 1},  # ok is not a bool
        delegation("ruti-router/laguna-s-2.1"),
    ]
    record = track.records(events, [LAGUNA])["ruti-router/laguna-s-2.1"]
    assert (record.runs, record.succeeded) == (1, 1)


def test_load_returns_no_history_rather_than_failing(monkeypatch):
    # A corrupt ledger, an unreadable registry, a proxy mid-write: the ranking still has
    # to happen, and "no history" is the honest state of it.
    def broken(**_kwargs):
        raise OSError("the ledger is not readable")
    monkeypatch.setattr(ledger, "read_events", broken)
    assert track.load() == {}

    monkeypatch.setattr(ledger, "read_events", lambda **_k: [delegation("ruti-router/x")])
    monkeypatch.setattr("ruti.providers.load_registry", lambda: {"version": 1})
    assert track.load() == {}


# ------------------------------------------------------------------- the ranking


@pytest.fixture
def ranking(registry, monkeypatch):
    """`router.rank` with local models gone and the four registered aliases served."""
    monkeypatch.setattr(lmstudio, "available", lambda: False)
    monkeypatch.setattr(litellm_cfg, "served_models",
                        lambda: [r["alias"] for r in registry])
    snapshot = quota.Quota(
        five_hour=quota.Window(10.0, time.time() + 3 * 3600), seven_day=None,
        captured_at=time.time(),
    )
    assert snapshot.band == quota.GREEN

    def rank(*, track_records=None):
        monkeypatch.setattr(modes, "current", lambda _sid: {"coding": False, "free": "off"})
        if track_records is not None:
            monkeypatch.setattr(track, "load", lambda: track_records)
        result = router.rank(router.Task(kind="analyze", files=3, loc=300), snapshot,
                            record=False)
        return result, {e["executor"]: e for e in result["ranked"]}

    return rank


def test_a_proven_alias_outranks_an_unproven_one(ranking):
    proven = track.Record(runs=8, succeeded=8, median_s=30.0)
    flaky = track.Record(runs=4, succeeded=0, median_s=None)
    result, by_name = ranking(track_records={
        "ruti-router/inkling-small": proven,
        "ruti-router/gemini-flash-lite": flaky,
    })

    good = by_name["ruti-router/inkling-small"]
    assert result["ranked"][0]["executor"] == "ruti-router/inkling-small"
    # 0.9 is the score every remote model gets from the constants alone. The measured
    # speed and the factor are what move it off that, in that direction.
    assert good["score"] > 0.9
    assert good["score"] == pytest.approx(
        round((0.6 + 0.4 * proven.speed) * proven.factor, 3)
    )
    assert good["track"] == {"runs": 8, "succeeded": 8, "median_s": 30.0,
                             "excluded": 0, "factor": 1.2}
    assert any("track record" in reason for reason in good["reasons"])
    assert any("median 30s" in reason and "x1.20" in reason
               for reason in good["reasons"])

    bad = by_name["ruti-router/gemini-flash-lite"]
    assert bad["score"] < 0.9
    assert bad["track"]["runs"] == 4 and bad["track"]["succeeded"] == 0
    assert any("track record" in reason and "0 of 4" in reason
               for reason in bad["reasons"])


def test_an_alias_whose_runs_were_another_models_says_so(ranking):
    # No runs of its own is not the same as no history: this alias has been asked eight
    # times and has answered none of them, and the score must not pretend otherwise.
    _, by_name = ranking(track_records={
        "ruti-router/inkling-small": track.Record(runs=0, succeeded=0, excluded=8),
    })
    entry = by_name["ruti-router/inkling-small"]
    assert entry["score"] == 0.9  # unchanged: nothing of its own to go on
    assert entry["track"]["excluded"] == 8 and entry["track"]["runs"] == 0
    assert any("no track record of its own yet: 8" in reason for reason in entry["reasons"])
    assert not any("track record:" in reason for reason in entry["reasons"])


def test_an_alias_with_no_history_is_left_exactly_as_it_was(ranking):
    result, by_name = ranking(track_records={})
    untouched = by_name["ruti-router/pareto-code"]
    assert untouched["score"] == 0.9
    assert untouched["track"] is None
    assert not any("track record" in reason for reason in untouched["reasons"])
    # Nothing in the report changes shape for an executor ruti has never run.
    assert all("track" in entry for entry in result["ranked"] + result["rejected"])


def test_the_real_ledger_is_read_when_nothing_is_monkeypatched(registry):
    # The wiring, not a stub: a delegation written to the ledger has to reach `rank()`
    # as a record under the executor's own name.
    ledger.record("delegation", model="ruti-router/inkling-small", ok=True,
                  files_changed=2, duration_s=45.0)
    assert track.load()["ruti-router/inkling-small"].succeeded == 1


# ------------------------------------------------- the proxy's own per-request log


def request(alias: str, group: str, at: float) -> dict:
    return {"at": at, "requested": alias, "group": group, "model": "x"}


def test_a_router_run_with_a_fallback_request_in_the_log_is_excluded():
    event = delegation("ruti-router/pareto-code", effective="anthropic/claude-fable-5-1",
                       is_router=True, duration=100.0)
    log = [request("pareto-code", "pareto-code", NOW - 50),
           request("pareto-code", "gemini-flash", NOW - 20)]
    record = track.records([event], [PARETO], log)["ruti-router/pareto-code"]
    assert record.runs == 0 and record.excluded == 1


def test_without_the_log_the_same_router_run_counts():
    event = delegation("ruti-router/pareto-code", effective="anthropic/claude-fable-5-1",
                       is_router=True)
    assert track.records([event], [PARETO])["ruti-router/pareto-code"].runs == 1


def test_log_entries_outside_the_runs_window_do_not_count():
    event = delegation("ruti-router/laguna-s-2.1", duration=60.0)
    log = [request("laguna-s-2.1", "gemini-flash", NOW - 600),   # an earlier run's
           request("laguna-s-2.1", "laguna-s-2.1", NOW - 30)]
    assert track.records([event], [LAGUNA], log)["ruti-router/laguna-s-2.1"].runs == 1

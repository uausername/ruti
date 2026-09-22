"""The classifier must never be able to make routing worse.

Two properties carry the whole design and are worth pinning down: every failure is
silent and harmless, and a guess may tighten routing on ordinary confidence but only
relax it on high confidence. The network is stubbed throughout -- these tests must run
with no key and no connection.
"""

from __future__ import annotations

import json

import pytest

from ruti import jev, router


def answer(kind="implement", kind_confidence=0.99, score=2.0, score_confidence=0.9,
           noul=0.1):
    """A response body shaped exactly like the endpoint's, so parsing is tested too."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "kind": {
                "type": "choice", "choice": kind, "confidence": kind_confidence,
                "probabilities": {kind: kind_confidence},
            },
            "difficulty": {
                "type": "score", "score": score, "confidence": score_confidence,
                "legend": {"0": "trivial", "1": "routine", "2": "involved",
                           "3": "demanding", "4": "expert"},
            },
            "trivial": {"type": "noul", "noul": noul},
        },
        "usage": {"input_tokens": 600, "output_tokens": 100, "cost": 2.5e-05},
    }


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(jev, "api_key", lambda transport=jev.DEFAULT_TRANSPORT: "test-key")


def stub_post(monkeypatch, payload):
    monkeypatch.setattr(jev, "_post", lambda body, url, key, timeout: payload)


# --------------------------------------------------------------------- failing open


def test_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(jev, "api_key", lambda transport=jev.DEFAULT_TRANSPORT: None)
    assert jev.classify("Rewrite the export pipeline end to end.") is None


def test_transport_failure_returns_none(keyed, monkeypatch):
    stub_post(monkeypatch, None)
    assert jev.classify("Rewrite the export pipeline end to end.") is None


def test_unparseable_body_returns_none(keyed, monkeypatch):
    stub_post(monkeypatch, {"answers": {"kind": {"choice": "not-a-kind"}}})
    assert jev.classify("Rewrite the export pipeline end to end.") is None


def test_empty_description_never_calls_out(keyed, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("an empty description must not reach the network")

    monkeypatch.setattr(jev, "_post", explode)
    assert jev.classify("   ") is None


def test_score_is_normalised_onto_the_task_scale(keyed, monkeypatch):
    stub_post(monkeypatch, answer(score=2.12))
    guess = jev.classify("Add a flag to route.")
    # Five legend levels span 0..4, so 2.12 is a little past halfway.
    assert guess is not None
    assert guess.difficulty == pytest.approx(0.53, abs=0.01)
    assert guess.cost_usd == pytest.approx(2.5e-05)


# ------------------------------------------------------------------- tighten only


def test_a_harder_kind_is_taken_on_ordinary_confidence(keyed, monkeypatch):
    stub_post(monkeypatch, answer(kind="security", kind_confidence=0.62))
    guess = jev.classify("Change who may read the audit table.")
    task, notes = router.apply_classification(router.Task(kind="implement"), guess)
    assert task.kind == "security"
    assert any("more cautious" in n for n in notes)


def test_security_then_keeps_the_work_in_house(keyed, monkeypatch):
    stub_post(monkeypatch, answer(kind="security", kind_confidence=0.62))
    guess = jev.classify("Change who may read the audit table.")
    task, _ = router.apply_classification(router.Task(kind="implement"), guess)
    assert task.kind in router.KEEP_IN_HOUSE


def test_a_softer_kind_needs_high_confidence(keyed, monkeypatch):
    stub_post(monkeypatch, answer(kind="boilerplate", kind_confidence=0.7))
    guess = jev.classify("Wrap the RLS helpers so they run once per query.")
    task, notes = router.apply_classification(router.Task(kind="security"), guess)
    assert task.kind == "security", "a softer kind must not slip through at 0.70"
    assert any("without the confidence" in n for n in notes)


def test_a_softer_kind_is_taken_once_confident(keyed, monkeypatch):
    stub_post(monkeypatch, answer(kind="boilerplate", kind_confidence=0.93))
    guess = jev.classify("Generate the twenty repetitive DTO classes.")
    task, _ = router.apply_classification(router.Task(kind="implement"), guess)
    assert task.kind == "boilerplate"


def test_difficulty_floor_never_lowers(keyed, monkeypatch):
    stub_post(monkeypatch, answer(kind="debug", score=0.0, score_confidence=0.95))
    guess = jev.classify("Add one more column to the report.")
    # `debug` alone is 0.8; a confident guess of "trivial" must not pull that down.
    # The kind is held steady so the floor is the only thing under test.
    task, _ = router.apply_classification(router.Task(kind="debug"), guess)
    assert task.difficulty == pytest.approx(0.8)


def test_low_confidence_difficulty_is_ignored(keyed, monkeypatch):
    stub_post(monkeypatch, answer(score=4.0, score_confidence=0.3))
    guess = jev.classify("Something vague.")
    task, notes = router.apply_classification(router.Task(kind="implement"), guess)
    assert task.difficulty_floor is None
    assert any("ignored" in n for n in notes)


# ------------------------------------------------------------------------ triviality


def test_not_trivial_forces_a_routing_decision(keyed, monkeypatch):
    stub_post(monkeypatch, answer(noul=0.17))
    guess = jev.classify("Rewrite the export pipeline across three renderers.")
    # Small counts would normally read as trivial and skip the ranking entirely.
    task, _ = router.apply_classification(router.Task(loc=10, files=1), guess)
    assert task.trivial is False


def test_trivial_needs_high_confidence(keyed, monkeypatch):
    stub_post(monkeypatch, answer(noul=0.66))
    guess = jev.classify("Tidy the helper.")
    task, notes = router.apply_classification(router.Task(loc=800, files=12), guess)
    assert task.trivial is False, "0.66 is not enough to skip routing"
    assert any("undecided" in n for n in notes)


def test_confident_trivial_skips_routing(keyed, monkeypatch):
    stub_post(monkeypatch, answer(noul=0.94))
    guess = jev.classify("Fix the typo in the error string.")
    task, _ = router.apply_classification(router.Task(loc=800, files=12), guess)
    assert task.trivial is True


# ----------------------------------------------------------------------- the probe


def test_probe_without_a_key_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(jev, "api_key", lambda transport=jev.DEFAULT_TRANSPORT: None)
    assert jev.probe() is None


def test_probe_reuses_a_recent_success(keyed, monkeypatch, tmp_path):
    monkeypatch.setattr(jev, "PROBE_CACHE", tmp_path / "probe.json")
    calls = []

    def counted(body, url, key, timeout):
        calls.append(1)
        return answer()

    monkeypatch.setattr(jev, "_post", counted)
    first = jev.probe()
    second = jev.probe()
    assert first["ok"] and second["ok"]
    assert second["cached"] is True
    assert len(calls) == 1, "a fresh cache must not hit the network again"


def test_probe_retries_a_cached_failure_sooner(keyed, monkeypatch, tmp_path):
    cache = tmp_path / "probe.json"
    monkeypatch.setattr(jev, "PROBE_CACHE", cache)
    stub_post(monkeypatch, None)
    failed = jev.probe()
    assert failed["ok"] is False

    # Age the failure past its short ceiling but well inside the success ceiling.
    record = json.loads(cache.read_text(encoding="utf-8"))
    record["at"] = record["at"] - (jev.PROBE_FAILURE_MAX_AGE + 60)
    cache.write_text(json.dumps(record), encoding="utf-8")

    stub_post(monkeypatch, answer())
    assert jev.probe()["ok"] is True

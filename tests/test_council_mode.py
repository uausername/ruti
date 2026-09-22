"""The council is the one feature here that deliberately spends more, so its gate matters.

`auto` decides whether N paid calls are justified, and the judge ranks the answers that
come back. Both are stubbed here: these run offline, with no key.
"""

from __future__ import annotations

import pytest

from ruti import council, jev, modes


def nouls(**values):
    """A response body carrying only Nouls, shaped like the endpoint's."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {name: {"type": "noul", "noul": value} for name, value in values.items()},
        "usage": {"cost": 2.5e-05},
        "latency_ms": 900.0,
    }


def stub_ask(monkeypatch, payload):
    monkeypatch.setattr(jev, "ask", lambda state, questions, **kw: payload)


# ------------------------------------------------------------------- the auto gate


def test_mechanical_work_is_declined(monkeypatch):
    stub_ask(monkeypatch, nouls(ambiguous=0.2, consequential=0.3, mechanical=0.9))
    worth = council.worth_convening("Rename tmp to buffer in parser.py")
    assert worth.convene is False
    assert "mechanical" in worth.reason()


def test_a_cheap_but_ambiguous_question_is_declined(monkeypatch):
    """The averaging bug this gate was rebuilt to fix.

    Naming a flag is genuinely ambiguous and not at all mechanical, and a mean of the
    three readings let it through at 0.60. Ambiguity is what makes a question hard;
    consequence is what makes it worth paying several models to answer.
    """
    stub_ask(monkeypatch, nouls(ambiguous=0.94, consequential=0.22, mechanical=0.1))
    worth = council.worth_convening("What should we name the new config flag?")
    assert worth.convene is False
    assert "cheap to get wrong" in worth.reason()


def test_an_unambiguous_but_costly_question_is_declined(monkeypatch):
    stub_ask(monkeypatch, nouls(ambiguous=0.15, consequential=0.95, mechanical=0.2))
    worth = council.worth_convening("Should we rotate the leaked key?")
    assert worth.convene is False
    assert "one defensible answer" in worth.reason()


def test_ambiguous_and_consequential_convenes(monkeypatch):
    stub_ask(monkeypatch, nouls(ambiguous=0.92, consequential=0.87, mechanical=0.05))
    worth = council.worth_convening("Trigger, client reducer, or both with a parity test?")
    assert worth.convene is True


def test_an_unreachable_classifier_does_not_convene(monkeypatch):
    stub_ask(monkeypatch, None)
    assert council.worth_convening("anything") is None, (
        "a failed check must hand the decision back, not spend money by default"
    )


# ---------------------------------------------------------------------- the judge


def opinions(*texts, failed=0):
    out = [council.Opinion(model=f"model-{i}", ok=True, text=t, duration_s=1.0)
           for i, t in enumerate(texts)]
    out += [council.Opinion(model=f"dead-{i}", ok=False, error="timeout")
            for i in range(failed)]
    return council.CouncilResult(question="a hard question", opinions=out)


def test_one_answer_is_not_a_council(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("nothing to judge; the judge must not be called")

    monkeypatch.setattr(jev, "ask", explode)
    assert council.judge(opinions("the only answer", failed=2)) is None


def test_the_judge_picks_by_label_and_reports_agreement(monkeypatch):
    monkeypatch.setattr(jev, "ask", lambda state, questions, **kw: {
        "answers": {
            "best": {"type": "choice", "choice": "B", "confidence": 0.91},
            "agreement": {"type": "noul", "noul": 0.04},
        },
        "usage": {"cost": 2.0e-05},
        "latency_ms": 1500.0,
    })
    verdict = council.judge(opinions("first answer", "second answer"))
    assert verdict.best == "model-1", "label B is the second surviving opinion"
    assert verdict.usable is True
    assert verdict.agreed is False


def test_failed_opinions_are_not_labelled(monkeypatch):
    seen = {}

    def capture(state, questions, **kw):
        seen["labels"] = sorted(questions["best"]["criteria"])
        return {"answers": {"best": {"choice": "A", "confidence": 0.9},
                            "agreement": {"noul": 0.8}}, "usage": {}}

    monkeypatch.setattr(jev, "ask", capture)
    council.judge(opinions("a", "b", failed=3))
    assert seen["labels"] == ["A", "B"], "a model that did not answer has no opinion to rank"


def test_a_low_confidence_pick_is_not_a_winner(monkeypatch):
    monkeypatch.setattr(jev, "ask", lambda state, questions, **kw: {
        "answers": {"best": {"choice": "A", "confidence": 0.41},
                    "agreement": {"noul": 0.5}},
        "usage": {},
    })
    verdict = council.judge(opinions("a", "b"))
    assert verdict.usable is False


def test_long_answers_are_cut_before_reaching_the_judge(monkeypatch):
    seen = {}
    monkeypatch.setattr(jev, "ask", lambda state, questions, **kw: seen.update(state=state) or {
        "answers": {"best": {"choice": "A", "confidence": 0.9},
                    "agreement": {"noul": 0.9}}, "usage": {}})
    council.judge(opinions("x" * 50_000, "short"))
    assert len(seen["state"]) < 10_000, "one rambling model must not crowd out the rest"


def test_an_unreachable_judge_leaves_the_opinions_alone(monkeypatch):
    stub_ask(monkeypatch, None)
    assert council.judge(opinions("a", "b")) is None


# ----------------------------------------------------------------------- the mode


def test_council_defaults_to_off():
    assert modes.DEFAULTS["council"] == "off"
    assert modes.current(None)["council"] == "off"


def test_an_unknown_level_reads_as_off():
    assert modes._normalise_council("sideways") == "off"
    assert modes._normalise_council(True) == "on"


def test_the_level_is_rejected_at_the_setter():
    with pytest.raises(ValueError):
        modes.set_council("some-session", "maybe")


def test_the_summary_names_the_level():
    assert "council:auto" in modes.active_summary(
        {"coding": False, "free": "off", "council": "auto"})
    assert modes.active_summary({"coding": False, "free": "off", "council": "off"}) == ""

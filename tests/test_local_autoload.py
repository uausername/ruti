"""`route` promises a non-resident local model "would load"; `delegate` must load it.

And since it does, a stopped server or an empty GPU is no longer a doctor failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ruti import delegate, doctor, lmstudio, planner

QWEN = SimpleNamespace(key="qwen/qwen3-4b")


@pytest.fixture
def catalog(monkeypatch):
    calls: dict = {"started": 0, "executed": []}
    monkeypatch.setattr(lmstudio, "list_models", lambda: [QWEN])
    monkeypatch.setattr(lmstudio, "start_server", lambda: calls.__setitem__(
        "started", calls["started"] + 1))
    monkeypatch.setattr(planner, "plan_load", lambda model, **kw: SimpleNamespace(
        ok=True, reasons=[], model=model, kw=kw))
    monkeypatch.setattr(planner, "execute", lambda plan, **kw: calls["executed"].append(
        (plan, kw)) or {"identifier": "local-qwen3-4b"})
    return calls


def test_a_stopped_server_is_started_and_the_model_loaded(catalog, monkeypatch):
    monkeypatch.setattr(lmstudio, "server_running", lambda: False)
    planner.ensure_resident("local-qwen3-4b", min_context=12095, ttl_seconds=900)
    assert catalog["started"] == 1
    [(plan, kw)] = catalog["executed"]
    assert plan.model is QWEN and plan.kw["min_context"] == 12095
    assert kw == {"ttl_seconds": 900}


def test_an_unknown_alias_is_refused(catalog, monkeypatch):
    monkeypatch.setattr(lmstudio, "server_running", lambda: True)
    with pytest.raises(RuntimeError, match="no downloaded model"):
        planner.ensure_resident("local-nope", min_context=1)


def test_a_plan_that_cannot_fit_is_refused(catalog, monkeypatch):
    monkeypatch.setattr(lmstudio, "server_running", lambda: True)
    monkeypatch.setattr(planner, "plan_load", lambda model, **kw: SimpleNamespace(
        ok=False, reasons=["does not fit"]))
    with pytest.raises(RuntimeError, match="does not fit"):
        planner.ensure_resident("local-qwen3-4b", min_context=1)


def test_delegate_loads_a_local_alias_before_running(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(planner, "ensure_resident",
                        lambda alias, **kw: seen.append((alias, kw["min_context"])))

    class Stop(Exception):
        pass

    def stop_here(*a, **kw):
        raise Stop

    monkeypatch.setattr(delegate, "probe", stop_here)
    with pytest.raises(Stop):
        delegate.run("x", model="ruti-router/local-qwen3-4b", directory=tmp_path)
    assert seen and seen[0][0] == "local-qwen3-4b" and seen[0][1] > 8095

    seen.clear()
    with pytest.raises(Stop):
        delegate.run("x", model="ruti-router/kimi", directory=tmp_path)
    assert not seen


def test_nothing_resident_is_not_a_doctor_warning(monkeypatch):
    monkeypatch.setattr(doctor.litellm_cfg, "liveliness", lambda: True)
    monkeypatch.setattr(doctor.litellm_cfg, "served_models", lambda: [])
    monkeypatch.setattr(doctor.lmstudio, "server_running", lambda: True)
    monkeypatch.setattr(doctor.lmstudio, "loaded_models", lambda: [])
    assert doctor._check_local_route().status == doctor.OK


def test_a_stopped_server_is_not_a_doctor_failure(monkeypatch):
    monkeypatch.setattr(doctor.lmstudio, "available", lambda: True)
    monkeypatch.setattr(doctor.lmstudio, "server_running", lambda: False)
    assert doctor._check_lmstudio().status == doctor.OK

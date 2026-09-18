"""Keep every test away from the real state directory.

ruti's modules resolve their file paths at import time from %LOCALAPPDATA%\\ruti, which
on a working machine holds the live ledger, provider registry and usage log. A test
that appended a route event there would be counted in `ruti report` as real advice.
"""

from __future__ import annotations

import pytest

from ruti import delegate, ledger, usage


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(usage, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(delegate, "RUNNING_FILE", tmp_path / "running.json")
    monkeypatch.setattr(delegate, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    return tmp_path


PARETO = {
    "provider": "openrouter", "alias": "pareto-code",
    "model": "openrouter/openrouter/pareto-code", "env_var": "RUTI_OPENROUTER_KEY_1",
    "supports_tools": True, "free": False, "coding": True, "context_window": 2_000_000,
    "enabled": True,
}
FREE_ROUTER = {
    "provider": "openrouter", "alias": "free", "model": "openrouter/openrouter/free",
    "env_var": "RUTI_OPENROUTER_KEY_1", "supports_tools": True, "free": True,
    "coding": False, "context_window": 200_000, "enabled": True,
}
INKLING = {
    "provider": "openrouter", "alias": "inkling-small",
    "model": "openrouter/thinkingmachines/inkling-small:free",
    "env_var": "RUTI_OPENROUTER_KEY_1", "supports_tools": True, "free": True,
    "coding": False, "context_window": 1_048_576, "enabled": True,
}
GEMINI = {
    "provider": "gemini", "alias": "gemini-flash-lite",
    "model": "gemini/gemini-2.5-flash-lite", "env_var": "RUTI_GEMINI_KEY_1",
    "supports_tools": True, "enabled": True,  # no price on record
}


@pytest.fixture
def registry(monkeypatch):
    from ruti import providers

    records = [dict(PARETO), dict(FREE_ROUTER), dict(INKLING), dict(GEMINI)]
    monkeypatch.setattr(providers, "load_registry",
                        lambda: {"version": 1, "providers": records})
    return records

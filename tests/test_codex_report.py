import json

from click.testing import CliRunner

from ruti import cli, ledger


def test_codex_report_counts_reviewed_work_and_excludes_claude(monkeypatch):
    events = [
        {"event": "delegation", "run_id": "a", "host": "codex", "ok": True,
         "lines_written": 10, "duration_s": 20, "cost_usd": None},
        {"event": "delegation", "run_id": "b", "host": "codex", "ok": True,
         "lines_written": 40, "duration_s": 50, "cost_usd": None},
        {"event": "delegation", "run_id": "c", "host": "codex", "ok": True},
        {"event": "delegation", "run_id": "d", "host": "claude", "lines_written": 999},
        {"event": "verdict", "run_id": "a", "host": "claude", "verdict": "ok"},
        {"event": "verdict", "run_id": "b", "host": "codex", "verdict": "bad"},
    ]
    monkeypatch.setenv("RUTI_HOST", "codex")
    monkeypatch.setattr(ledger, "read_events", lambda **k: events)
    result = CliRunner().invoke(cli.main, ["report", "--json"])
    assert result.exit_code == 0, result.output
    stats = json.loads(result.stdout)
    assert stats["delegations"]["total"] == 3
    assert stats["work_offloaded"]["lines_written"] == 10
    assert stats["work_offloaded"]["discarded_lines"] == 40
    assert stats["review"] == {
        "accepted": 1, "rejected": 1, "unreviewed": 1, "acceptance_rate": 0.5,
        "accepted_run_seconds": 20, "accepted_lines_in_changed_files": 10,
        "subscription_savings_percentage": None,
    }
    text = CliRunner().invoke(cli.main, ["report"]).output
    assert "subscription savings: unknown" in text
    assert "Work the manager did not have to type" not in text


def test_legacy_claude_events_remain_visible_and_codex_prefix_is_recognised():
    old = {"event": "delegation", "session": "old"}
    codex = {"event": "delegation", "session": "codex:thread"}
    assert ledger.for_host([old, codex], "claude") == [old]
    assert ledger.for_host([old, codex], "codex") == [codex]

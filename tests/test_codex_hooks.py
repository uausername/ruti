"""Native lifecycle integration with no live config, network or model calls."""
import json
import time

import pytest
from click.testing import CliRunner

from ruti import cli, config, context_watch, flow, modes, quota, sessions
from ruti.hooks import codex as h


def payload(event='UserPromptSubmit', **extra):
    return {'session_id': 'abc', 'hook_event_name': event, **extra}


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(h.codex_telemetry, 'capture', lambda _: {})
    monkeypatch.setattr(h.codex_telemetry, 'load', lambda: {})


def test_policy_is_native_scoped_and_does_not_launch(capture, monkeypatch):
    monkeypatch.setattr(flow, 'launch', lambda *a, **k: pytest.fail('must never launch Claude'))
    modes.set_manager('codex:abc', True)
    modes.set_effplus('codex:abc', True)
    result = h.handle(payload())['hookSpecificOutput']['additionalContext']
    assert 'Codex budget' in result and 'hooks cannot switch' in result
    assert sessions.current_session_id() == 'codex:abc'
    assert h.runtime_status('abc')['events']['UserPromptSubmit'] > 0
    assert h.runtime_status('other') == {}
    assert h.runtime_status('abc')['capabilities']['flow_spawn'] is False
    assert h.handle(payload(session_id='../oops')) is None


def test_off_is_isolated_and_removes_gates(capture):
    modes.set_wait('codex:abc', True)
    sessions.set_disabled('codex:abc', True)
    assert 'OFF' in h.handle(payload())['hookSpecificOutput']['additionalContext']
    assert h.handle(payload('PreToolUse', tool_name='Bash', tool_input={'command': 'git status'})) is None
    assert not sessions.is_disabled('codex:other')


def test_quota_uses_native_observation_only(monkeypatch):
    monkeypatch.setenv('RUTI_HOST', 'codex')
    monkeypatch.setattr(h.codex_telemetry, 'load', lambda: {
        'five_hour': {'used_percentage': 42, 'resets_at': time.time() + 900},
        'captured_at_epoch': time.time() - 200, 'model': 'native-model', 'effort': 'high'})
    q = quota.load()
    assert q.five_hour.used_percentage == 42 and q.seven_day is None
    assert q.freshness == 'stale' and q.model == 'native-model'


def test_wait_denial_and_same_session_stop(capture, monkeypatch):
    from ruti import wait
    modes.set_wait('codex:abc', True)
    snapshot = quota.Quota(quota.Window(96, time.time() + 60), None, time.time())
    monkeypatch.setattr(h.quota, 'load', lambda: snapshot)
    result = h.handle(payload('PreToolUse', tool_name='Bash', tool_input={'command': 'git status'}))
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny'
    monkeypatch.setattr(wait, 'stop', lambda sid, q: {'decision': 'block', 'reason': 'resume this session'})
    assert h.handle(payload('Stop'))['decision'] == 'block'
    assert h.handle(payload('Stop', stop_hook_active=True)) is None


@pytest.mark.parametrize('tool', ['exec_command', 'shell_command', 'PowerShell', 'Bash'])
def test_native_cmd_recovery_survives_both_gates(capture, monkeypatch, tool):
    modes.set_wait('codex:abc', True)
    modes.set_flow('codex:abc', True)
    monkeypatch.setattr(h.quota, 'load', lambda: quota.Quota(
        quota.Window(96, time.time() + 60), None, time.time()))
    monkeypatch.setattr(h.codex_telemetry, 'capture', lambda _: {
        'context_used_percentage': 65, 'context_captured_at_epoch': time.time()})
    for command in ('ruti mode flow off', 'ruti mode wait off', 'ruti flow handoff --file checkpoint.md'):
        assert h.handle(payload('PreToolUse', tool_name=tool, tool_input={'cmd': command})) is None
    denied = h.handle(payload('PreToolUse', tool_name=tool, tool_input={'cmd': 'git status'}))
    assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_stale_context_not_freshened_or_borrowed(monkeypatch):
    monkeypatch.setattr(h.codex_telemetry, 'capture', lambda _: {
        'context_used_percentage': 90, 'context_captured_at_epoch': time.time() - 500})
    context_watch.record('codex:abc', 80)
    context_watch.record('claude-abc', 20)
    h._capture(payload(), 'codex:abc')
    assert context_watch.used('codex:abc') is None
    assert context_watch.used('claude-abc') == 20


def test_flow_gate_checkpoint_and_manager_receipt(capture, monkeypatch, tmp_path):
    modes.set_flow('codex:abc', True)
    monkeypatch.setattr(h.codex_telemetry, 'capture', lambda _: {
        'context_used_percentage': 65, 'context_captured_at_epoch': time.time()})
    monkeypatch.setattr(flow, 'stop', lambda *a, **k: pytest.fail('Claude stop cannot run'))
    result = h.handle(payload('PreToolUse', tool_name='apply_patch', tool_input={'command': '*** Begin Patch'}))
    reason = result['hookSpecificOutput']['permissionDecisionReason']
    assert 'independent top-level manager' in reason
    path = flow.write_handoff('codex:abc', 'checkpoint', str(tmp_path))
    assert ':' not in path.name and path.read_text().strip() == 'checkpoint'
    assert h.handle(payload('Stop', cwd=str(tmp_path)))['continue'] is False
    assert h.pending_handoffs()[0]['handoff'] == str(path)
    assert flow.save_draft('codex:other', 'draft')
    assert flow.load_draft('codex:other') == 'draft'


def test_native_installer_optin_preview_apply_backup(tmp_path, monkeypatch):
    from ruti import install
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setattr(install, 'CODEX_MEMORY', tmp_path / 'AGENTS.md')
    runner = CliRunner()
    assert runner.invoke(cli.main, ['install', '--native-hooks']).exit_code != 0
    args = ['install', '--host', 'codex', '--native-hooks']
    assert runner.invoke(cli.main, args).exit_code == 0
    target = tmp_path / 'codex' / 'hooks.json'
    assert not target.exists()
    target.parent.mkdir()
    target.write_text('{"user":"preserved"}')
    result = runner.invoke(cli.main, args + ['--apply'])
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())['user'] == 'preserved'
    assert list(target.parent.glob('hooks.json.ruti-backup-*'))[0].read_text() == '{"user":"preserved"}'
    assert '/hooks' in result.output


def test_apply_patch_time_machine_covers_moves(monkeypatch, tmp_path):
    from ruti import timemachine
    paths = []
    class Project:
        def __init__(self, root): pass
        def has_letters(self): return True
    monkeypatch.setattr(timemachine, 'Project', Project)
    monkeypatch.setattr(timemachine, 'event_root', lambda p: tmp_path)
    monkeypatch.setattr(timemachine, 'warn_text', lambda p, _: paths.append(p['tool_input']['file_path']) or 'warning')
    text = h._letters(payload(tool_name='apply_patch', cwd=str(tmp_path), tool_input={
        'command': '*** Update File: first.py\n*** Move to: second.py\n'}), 'codex:abc')
    assert text == 'warning' and len(paths) == 2

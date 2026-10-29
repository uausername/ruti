import hashlib
import json
import os
from pathlib import Path

import pytest

from ruti import codex_telemetry as t

NOW = 1800000000
STAMP = '2026-01-01T00:00:00Z'
AT = 1767225600


def row(kind, payload, timestamp=STAMP):
    return {'type': kind, 'payload': payload, 'timestamp': timestamp}


def event(limits=None, info=None, timestamp=STAMP):
    return row('event_msg', {'type': 'token_count', 'rate_limits': limits,
                             'info': info}, timestamp)


def window(used=40, duration=300, reset=1800000100):
    return {'used_percent': used, 'window_minutes': duration, 'resets_at': reset}


@pytest.fixture
def rollout(tmp_path, monkeypatch):
    monkeypatch.setattr(t.config, 'STATE_ROOT', tmp_path / 'state')
    monkeypatch.setenv('RUTI_HOST', 'codex')
    monkeypatch.setenv('CODEX_THREAD_ID', 'abc')
    monkeypatch.delenv('RUTI_SESSION_ID', raising=False)
    path = tmp_path / 'rollout.jsonl'

    def write(*rows, meta=None):
        records = [row('session_meta', {'id': 'abc'} if meta is None else meta), *rows]
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return path
    return write


def test_native_windows_context_and_null(rollout):
    path = rollout(row('turn_context', {'model': 'gpt-test', 'effort': 'high', 'prompt': 'secret'}),
                   event({'primary': window(12, 10080), 'secondary': window()}),
                   event(info={'last_token_usage': {'total_tokens': 40000},
                               'total_token_usage': {'total_tokens': 999999},
                               'model_context_window': 200000},
                         timestamp='2026-01-02T00:00:00Z'))
    data = t.read(path, 'codex:abc', NOW)
    assert data['five_hour']['used_percentage'] == 40
    assert data['seven_day']['used_percentage'] == 12
    assert data['context_used_percentage'] == 15
    assert data['captured_at_epoch'] == AT
    assert data['context_captured_at_epoch'] == AT + 86400
    assert data['model'] == 'gpt-test'
    assert 'prompt' not in data


@pytest.mark.parametrize('meta', [{}, {'id': 'other'}, {'id': 'abc', 'session_id': 'other'}])
def test_exact_header(rollout, meta):
    assert t.read(rollout(meta=meta), 'abc', NOW) == {}


@pytest.mark.parametrize('identity', ['../abc', '', 'codex:codex:abc', 'a' * 129, None])
def test_bad_identity(rollout, identity):
    assert t.read(rollout(), identity, NOW) == {}


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), -1, 101, '40'])
def test_invalid_numbers(rollout, bad):
    data = t.read(rollout(event({'primary': window(bad)})), 'abc', NOW)
    assert 'captured_at_epoch' not in data


@pytest.mark.parametrize('limits', [
    {'primary': window(duration=60)},
    {'primary': window(), 'secondary': window()},
    {'primary': window(reset=True)},
    {'primary': window(reset=1800000000000)},
    {'primary': window(reset='2026-01-01')},
])
def test_bad_windows(rollout, limits):
    assert 'captured_at_epoch' not in t.read(rollout(event(limits)), 'abc', NOW)


def test_partial_and_future(rollout):
    data = t.read(rollout(event({'primary': window()}),
                          event({'primary': window(22, 10080, None)}),
                          event({'primary': window(99)}, timestamp='2099-01-01T00:00:00Z')),
                  'abc', NOW)
    assert data['five_hour'] is None
    assert data['seven_day'] == {'used_percentage': 22, 'resets_at': None}


def test_cache_refresh_isolation_and_staleness(rollout):
    path = rollout(event({'primary': window()}))
    data = t.capture({'session_id': 'codex:abc', 'transcript_path': str(path),
                      'prompt': 'PRIVATE', 'account': 'PRIVATE'}, NOW)
    cache = t._cache_path('abc')
    assert cache.name == hashlib.sha256(b'abc').hexdigest() + '.json'
    assert 'PRIVATE' not in cache.read_text()
    assert t.load(now=NOW + 100)['captured_at_epoch'] == AT
    path.unlink()
    assert t.load(now=NOW + 200) == data
    assert t.load('codex:other', NOW) == {}
    assert t.load('other', NOW) == {}
    cache.write_text('{}')
    assert t.load(now=NOW) == {}


def test_refresh_new_metrics(rollout):
    path = rollout(event({'primary': window()}))
    t.capture({'session_id': 'abc', 'transcript_path': str(path)}, NOW)
    rollout(event({'primary': window(55)}, timestamp='2026-01-02T00:00:00Z'))
    assert t.load(now=NOW)['five_hour']['used_percentage'] == 55


def test_host_and_unavailable(rollout, monkeypatch):
    path = rollout()
    monkeypatch.setenv('RUTI_HOST', 'claude')
    monkeypatch.setattr(t, '_cache_path', lambda raw: pytest.fail('must not read cache'))
    assert t.load('codex:abc', NOW) == {}
    assert t.read(path.parent, 'abc', NOW) == {}
    link = path.with_name('link')
    try:
        link.symlink_to(path)
    except PermissionError:
        pytest.skip('symlink privilege unavailable')
    assert t.read(link, 'abc', NOW) == {}


def test_bounds_and_truncation(rollout):
    path = rollout()
    with path.open('ab') as stream:
        stream.write(b'x' * (t.TAIL_LIMIT + 10) + b'\n')
        stream.write(b'{broken}\n')
        stream.write((json.dumps(event({'primary': window()})) + '\n').encode())
        stream.write(json.dumps(event({'primary': window(99)})).encode())
    assert t.read(path, 'abc', NOW)['five_hour']['used_percentage'] == 40
    path.write_bytes(b'x' * (t.LINE_LIMIT + 1) + b'\n')
    assert t.read(path, 'abc', NOW) == {}


def test_tail_line_count(rollout):
    path = rollout(event({'primary': window()}), *[row('ignored', {}) for _ in range(t.MAX_LINES)])
    assert 'captured_at_epoch' not in t.read(path, 'abc', NOW)


@pytest.mark.parametrize('tokens,window_size,expected', [(12000, 200000, 0),
    (999999, 200000, 100), (12001, 12200, 0), (40000, 12000, None), (True, 200000, None)])
def test_context_rounding(rollout, tokens, window_size, expected):
    data = t.read(rollout(event(info={'last_token_usage': {'total_tokens': tokens},
                                    'model_context_window': window_size})), 'abc', NOW)
    assert data.get('context_used_percentage') == expected


def test_cache_failures_and_schema(rollout, monkeypatch):
    path = rollout(event({'primary': window()}))
    t.capture({'session_id': 'abc', 'transcript_path': str(path)}, NOW)
    cache = t._cache_path('abc')
    data = json.loads(cache.read_text())
    data['captured_at_epoch'] = True
    cache.write_text(json.dumps(data))
    assert t.load(now=NOW) == {}
    monkeypatch.setattr(t.config, 'write_json', lambda *args: (_ for _ in ()).throw(PermissionError()))
    assert t.capture({'session_id': 'abc', 'transcript_path': str(path)}, NOW)


def test_refresh_preserves_old_source_when_tail_has_only_context(rollout):
    path = rollout(event({'primary': window()}))
    t.capture({'session_id': 'abc', 'transcript_path': str(path)}, NOW)
    rollout(row('turn_context', {'model': 'new'}, '2026-01-02T00:00:00Z'))
    data = t.load(now=NOW)
    assert data['captured_at_epoch'] == AT
    assert data['five_hour']['used_percentage'] == 40
    assert data['model_captured_at_epoch'] == AT + 86400


def test_huge_number_is_invalid(rollout):
    assert 'captured_at_epoch' not in t.read(
        rollout(event({'primary': window(10 ** 1000)})), 'abc', NOW)


def test_nonwindow_objects_never_supply_quota(rollout):
    data = t.read(rollout(event({'credits': window(99), 'primary': window(12)})), 'abc', NOW)
    assert data['five_hour']['used_percentage'] == 12
    assert 'captured_at_epoch' not in t.read(rollout(event({'credits': window()})), 'abc', NOW)


def test_controls_and_nested_json_are_ignored(rollout):
    data = t.read(rollout(row('turn_context', {'model': 'gpt\u202eevil', 'effort': 'high'})), 'abc', NOW)
    assert 'model' not in data
    assert t._row(b'[' * 3000 + b'0' + b']' * 3000) == {}

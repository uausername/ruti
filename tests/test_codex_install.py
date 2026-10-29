import json
import shlex

import pytest

from ruti.codex_install import plan


EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')


def rendered(path, interpreter='/opt/ruti python/bin/python', **kwargs):
    kwargs.setdefault('platform', 'linux')
    changes = plan(path, interpreter=interpreter, **kwargs)
    return changes, json.loads(changes[0][3])


def test_schema_and_pure_plan(tmp_path):
    path = tmp_path / 'absent' / 'hooks.json'
    changes, result = rendered(path)
    assert not path.parent.exists()
    assert changes[0][:3] == ('Codex hooks.json', path, '')
    assert changes[0][3].endswith('\n')
    assert tuple(result['hooks']) == EVENTS
    for event, groups in result['hooks'].items():
        assert len(groups) == 1
        assert set(groups[0]) == {'hooks'}
        assert groups[0]['hooks'] == [{
            'type': 'command',
            'command': "'/opt/ruti python/bin/python' -I -X utf8 -m ruti.hooks.codex",
            'timeout': 19800 if event == 'Stop' else 5,
            'statusMessage': f'Ruti Codex: {event}',
            'additionalContextLimit': 5000,
        }]


@pytest.mark.parametrize('value', [None, [], 1, {'hooks': []},
    {'hooks': {'Other': {}}}, {'hooks': {'Other': [None]}},
    {'hooks': {'Other': [{}]}}, {'hooks': {'Other': [{'hooks': {}}]}},
    {'hooks': {'Other': [{'hooks': [None]}]}},
    {'hooks': {'Other': [{'hooks': [{'command': 'echo hi'}]}]}},
    {'hooks': {'Other': [{'hooks': [{'type': 'command', 'command': []}]}]}},
    {'hooks': {'Other': [{'matcher': [], 'hooks': []}]}},
])
def test_invalid_shapes_fail_unchanged(tmp_path, value):
    path = tmp_path / 'hooks.json'
    before = json.dumps(value)
    path.write_text(before)
    with pytest.raises(ValueError):
        plan(path)
    assert path.read_text() == before


@pytest.mark.parametrize('before', ['', '{oops', '   '])
def test_invalid_json(tmp_path, before):
    path = tmp_path / 'hooks.json'
    path.write_text(before)
    with pytest.raises(ValueError):
        plan(path)
    assert path.read_text() == before


def test_preserve_users_and_mixed_groups_and_update(tmp_path):
    path = tmp_path / 'hooks.json'
    _, initial = rendered(path, '/old/python')
    owned = initial['hooks']['SessionStart'][0]['hooks'][0]
    same_label = {**owned, 'command': 'echo ruti.hooks.codex'}
    user = {'type': 'command', 'command': 'echo café'}
    echo = {**owned, 'command': 'echo /old/python -I -X utf8 -m ruti.hooks.codex'}
    appended = {**owned, 'command': owned['command'] + ' && echo hi'}
    wrapper = {**owned, 'command': '/wrapper /old/python -I -X utf8 -m ruti.hooks.codex'}
    other_label = {**owned, 'statusMessage': 'User hook'}
    current = {'unknown': {'café': [1]}, 'hooks': {
        'PluginEvent': [{'matcher': 'plugin', 'hooks': [user]}],
        'SessionStart': [
            {'matcher': 'custom', 'extra': True, 'hooks': [user, owned, same_label]},
            {'hooks': [echo, appended, wrapper, other_label]},
            {'hooks': [owned]},
        ],
    }}
    before = json.dumps(current, ensure_ascii=False)
    path.write_text(before, encoding='utf-8')
    changes, result = rendered(path, '/new/python')
    assert path.read_text(encoding='utf-8') == before
    assert changes[0][2] == before
    assert 'café' in changes[0][3]
    assert result['unknown'] == current['unknown']
    assert result['hooks']['PluginEvent'] == current['hooks']['PluginEvent']
    groups = result['hooks']['SessionStart']
    assert groups[0] == {'matcher': 'custom', 'extra': True, 'hooks': [user, same_label]}
    assert groups[1] == current['hooks']['SessionStart'][1]
    assert len(groups) == 3
    assert groups[-1]['hooks'][0]['command'].startswith('/new/python ')
    path.write_text(changes[0][3], encoding='utf-8')
    assert plan(path, interpreter='/new/python', platform='linux') == []


def test_windows_spaces_upgrade_and_idempotence(tmp_path):
    path = tmp_path / 'hooks.json'
    interpreter = str(tmp_path / 'Python Dir' / 'python.exe')
    changes, result = rendered(path, interpreter, platform='win32')
    assert result['hooks']['Stop'][0]['hooks'][0]['command'] == (
        f'"{interpreter.replace(chr(92), chr(47))}" -I -X utf8 -m ruti.hooks.codex')
    path.write_text(changes[0][3])
    assert plan(path, interpreter=interpreter, platform='win32') == []
    changes, result = rendered(path, r'C:\Program Files\Python\python.exe', platform='win32')
    for groups in result['hooks'].values():
        assert len(groups) == 1
        assert groups[0]['hooks'][0]['command'] == (
            '"C:/Program Files/Python/python.exe" -I -X utf8 -m ruti.hooks.codex')
    path.write_text(changes[0][3])
    assert plan(path, interpreter=r'C:\Program Files\Python\python.exe', platform='win32') == []


@pytest.mark.parametrize('interpreter', ['python', 'C:python.exe', '/bad\npython',
    '/bad\rpython', '/bad\0python', '/bad"python', '/bad%PATH%/python',
    '/bad!python', '/bad&python', '/bad|python', '/bad<python', '/bad>python',
    '/bad^python', '/bad`python', '/bad\tpython', '//server/python', '/a/../python'])
def test_windows_rejects_unsafe_paths(tmp_path, interpreter):
    with pytest.raises(ValueError):
        plan(tmp_path / 'hooks.json', interpreter=interpreter, platform='win32')


@pytest.mark.parametrize('interpreter', ['python', '/bad\npython', '/bad\0python'])
def test_unix_rejects_invalid_paths(tmp_path, interpreter):
    with pytest.raises(ValueError):
        plan(tmp_path / 'hooks.json', interpreter=interpreter, platform='linux')


def test_unix_shell_quoting(tmp_path):
    interpreter = "/tmp/Python's $(echo hi);% & python"
    _, result = rendered(tmp_path / 'hooks.json', interpreter, platform='linux')
    command = result['hooks']['Stop'][0]['hooks'][0]['command']
    assert shlex.split(command) == [interpreter, '-I', '-X', 'utf8', '-m', 'ruti.hooks.codex']


def test_defaults_and_codex_home(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    changes = plan()
    assert changes[0][1] == tmp_path / 'codex' / 'hooks.json'
    assert shlex.split(json.loads(changes[0][3])['hooks']['Stop'][0]['hooks'][0]['command'])[0] == sys.executable.replace('\\', '/')
    assert not (tmp_path / 'codex').exists()

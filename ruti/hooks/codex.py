"""Native Codex lifecycle policies; no Claude launchers or implicit API calls."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time

from ruti import codex_telemetry, config, context_watch, modes, quota, sessions

EVENTS = frozenset({'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'})
MAX_INPUT_BYTES = 1024 * 1024
MAX_CONTEXT = 12000


def _raw_id(value):
    if not isinstance(value, str):
        return None
    raw = value.removeprefix('codex:')
    return raw if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', raw) else None


def _key(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _runtime_path(raw):
    return config.STATE_ROOT / 'codex' / 'hooks' / (_key(raw) + '.json')


def runtime_status(session_id=None):
    raw = _raw_id(session_id or sessions.current_session_id())
    if not raw:
        return {}
    try:
        data = config.read_json(_runtime_path(raw), default={})
        return data if isinstance(data, dict) and data.get('session_id') == raw else {}
    except (OSError, ValueError):
        return {}


def _record_runtime(raw, event):
    data = runtime_status(raw)
    seen = data.get('events') if isinstance(data.get('events'), dict) else {}
    config.write_json(_runtime_path(raw), {
        'session_id': raw, 'host': 'codex', 'events': {**seen, event: time.time()},
        'capabilities': {'policy': True, 'wait': True, 'timemachine': True,
                         'flow_gate': True, 'flow_spawn': False,
                         'model_switch': False, 'implicit_paid_calls': False},
    })


def _capture(payload, sid):
    data = codex_telemetry.capture(payload)
    at = data.get('context_captured_at_epoch')
    used = data.get('context_used_percentage')
    valid = (isinstance(at, (int, float)) and not isinstance(at, bool)
             and math.isfinite(at) and 0 <= time.time() - at <= quota.FRESH_SECONDS
             and isinstance(used, (int, float)) and not isinstance(used, bool)
             and math.isfinite(used) and 0 <= used <= 100
             and payload.get('source') != 'compact')
    state = context_watch._load()
    if valid:
        state[sid] = {'used_percentage': used, 'at': at}
    else:
        state.pop(sid, None)
    config.write_json(context_watch.CONTEXT_FILE, state)
    return data


def _output(event, text):
    return {'hookSpecificOutput': {'hookEventName': event,
                                  'additionalContext': str(text)[:MAX_CONTEXT]}}


def _policy(sid, payload):
    state = modes.current(sid)
    if sessions.is_disabled(sid):
        return 'ruti: OFF for this Codex session; route/delegate refuse. Run ruti on to restore routing.'
    lines = ['ruti Codex budget: ' + quota.load().summary()]
    if state.get('coding'):
        lines.append(modes.coding_note(state.get('free', 'off')))
    if state.get('free') == 'hard':
        lines.append('Free hard: paid metered delegates are forbidden; route/delegate enforce this policy.')
    elif state.get('free') == 'soft':
        lines.append('Free soft: prefer zero-cost executors; warn before a paid metered delegate.')
    if state.get('council') in ('on', 'auto'):
        lines.append('Council ' + state['council'] + ': assess consequential ambiguous choices before implementation; '
                     'do not spend on API council calls without the user\'s separate permission.')
    if state.get('jev'):
        lines.append('Jev is configured as a mode; this hook makes no implicit paid classifier call.')
    if state.get('manager'):
        lines.append('Manager: keep architecture and final review in the current Codex session; delegate bounded '
                     'implementation. Model changes require Codex\'s supported /model control; hooks cannot switch it.')
    if state.get('effplus'):
        lines.append('eff+: prefer higher reasoning for difficult work when measured budget permits; '
                     'this is advice, not an unverified change of the active model/effort.')
    if state.get('wait'):
        from ruti import wait
        lines.append(wait.prompt_note(sid, quota.load()))
    if state.get('flow'):
        lines.append('Flow: at measured 50% context finish the current step and write ruti flow handoff; '
                     'at 60% other tools are gated. End with the checkpoint; the independent top-level manager '
                     'must start the next harness. A child must never spawn nested harnesses.')
    return '\n'.join(lines)


def _tool_payload(payload, sid):
    result = {**payload, 'session_id': sid}
    if result.get('tool_name') in ('update_plan', 'TodoWrite'):
        result['tool_name'] = 'TodoWrite'
    elif result.get('tool_name') in ('exec_command', 'shell_command', 'PowerShell'):
        result['tool_name'] = 'Bash'
    if result.get('tool_name') == 'Bash' and isinstance(result.get('tool_input'), dict):
        value = result['tool_input']
        if not isinstance(value.get('command'), str) and isinstance(value.get('cmd'), str):
            result['tool_input'] = {**value, 'command': value['cmd']}
        command = result['tool_input'].get('command')
        if isinstance(command, str):
            result['tool_input'] = {**result['tool_input'], 'command': _recovery_command(command, sid)}
    return result


def _recovery_command(command, sid):
    """Recognize the required host/session bindings before a pure Ruti command.

    This only adapts policy inspection; it never executes or approves a command.
    Unknown prefixes remain unchanged and therefore stay gated.
    """
    raw = _raw_id(sid)
    if not raw:
        return command
    values = {raw, '$CODEX_THREAD_ID', '${CODEX_THREAD_ID}'}
    try:
        tokens = shlex.split(command)
        if tokens and tokens[0] == 'env':
            tokens = tokens[1:]
        if len(tokens) >= 3:
            bindings = dict(token.split('=', 1) for token in tokens[:2])
            if (set(bindings) == {'RUTI_HOST', 'RUTI_SESSION_ID'}
                    and bindings['RUTI_HOST'] == 'codex'
                    and bindings['RUTI_SESSION_ID'] in values and tokens[2] == 'ruti'):
                return shlex.join(tokens[2:])
    except ValueError:
        pass
    # PowerShell's standard per-invocation bindings contain semicolons. Strip
    # only these exact two assignments, never arbitrary shell setup/chaining.
    identity = re.escape(raw)
    host = r"\$env:RUTI_HOST\s*=\s*(?:'codex'|\"codex\")\s*;\s*"
    session = (r"\$env:RUTI_SESSION_ID\s*=\s*(?:'" + identity + r"'|\"" + identity
               + r"\"|\$env:CODEX_THREAD_ID|\"\$env:CODEX_THREAD_ID\")\s*;\s*")
    for prefix in (host + session, session + host):
        match = re.fullmatch(r'\s*' + prefix + r'(ruti(?:\s+.*)?)\s*', command)
        if match:
            return match[1]
    return command


def _flow_gate(sid, payload):
    from ruti import flow
    used = context_watch.used(sid)
    if not modes.flow_state(sid).get('handoff') and (used is None or used < flow.FORCE_AT):
        return None
    if flow._allowed(payload):
        return None
    return flow._deny('Ruti Codex flow: checkpoint required. Use ruti flow handoff, then end the turn. '
                      'The independent top-level manager must start the continuation; '
                      'this hook does not spawn sessions. ruti mode flow off releases the gate.')


def _letters(payload, sid, startup=False):
    from ruti import timemachine
    root = Path(payload.get('cwd') or Path.cwd())
    project = timemachine.Project(timemachine.event_root({**payload, 'session_id': sid}))
    if not project.has_letters():
        return ''
    # A .claude hook is not loaded by Codex: do not suppress these native warnings
    # just because the same repository also has Claude's time-machine hook.
    if startup:
        return timemachine.brief_text(project, source=str(payload.get('source') or 'startup')) or ''
    command = (payload.get('tool_input') or {}).get('command', '')
    if payload.get('tool_name') == 'apply_patch':
        paths = re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', str(command), re.M)[:64]
        notes = []
        for value in paths:
            path = Path(value)
            path = path if path.is_absolute() else root / path
            event = {**payload, 'session_id': sid, 'tool_name': 'Edit',
                     'tool_input': {'file_path': str(path)}}
            scoped = timemachine.Project(timemachine.event_root(event))
            text = timemachine.warn_text(event, scoped) if scoped.has_letters() else ''
            if text and text not in notes:
                notes.append(text)
        return '\n'.join(notes)
    if payload.get('tool_name') not in ('Write', 'Edit', 'Bash'):
        return ''
    return timemachine.warn_text(payload, project)


def pending_handoffs():
    result = []
    for path in sorted((config.STATE_ROOT / 'codex' / 'handoffs').glob('*.json'))[:128]:
        try:
            data = config.read_json(path, default={})
            if isinstance(data, dict) and data.get('status') == 'manager_required':
                result.append(data)
        except OSError:
            pass
    return result


def handle(payload):
    if not isinstance(payload, dict) or payload.get('hook_event_name') not in EVENTS:
        return None
    raw = _raw_id(payload.get('session_id'))
    if not raw:
        return None
    os.environ['RUTI_HOST'] = 'codex'
    os.environ['RUTI_SESSION_ID'] = raw
    sid = 'codex:' + raw
    event = payload['hook_event_name']
    _capture(payload, sid)
    _record_runtime(raw, event)
    state = modes.current(sid)
    if event in ('SessionStart', 'UserPromptSubmit'):
        text = _policy(sid, payload)
        if event == 'SessionStart' and state.get('timemachine') and not sessions.is_disabled(sid):
            from ruti.hooks.session_start import _within
            note = _within(lambda p: _letters(p, sid, startup=True), payload, 1.0)
            if note:
                text += '\n' + note
        return _output(event, text)
    if sessions.is_disabled(sid):
        return None
    from ruti import flow, wait
    adapted = _tool_payload(payload, sid)
    if event == 'PreToolUse':
        if state.get('wait'):
            denied = wait.pre_tool_use(sid, adapted, quota.load())
            if denied:
                return denied
        if state.get('flow'):
            denied = _flow_gate(sid, adapted)
            if denied:
                return denied
        if state.get('timemachine'):
            from ruti.hooks.session_start import _within
            note = _within(lambda p: _letters(p, sid), adapted, 1.0)
            return _output(event, note) if note else None
    if event == 'PostToolUse':
        notes = []
        if state.get('wait'):
            note = wait.post_tool_use(sid, quota.load())
            if note:
                notes.append(note['hookSpecificOutput']['additionalContext'])
        if state.get('flow'):
            used = context_watch.used(sid)
            if used is not None and used >= flow.FLOW_AT:
                notes.append(f'ruti flow: {used:.0f}% context used. Write the handoff and end; '
                             'the top-level manager continues it in a separate harness.')
        return _output(event, '\n'.join(notes)) if notes else None
    if event == 'Stop':
        if payload.get('stop_hook_active'):
            return None
        if state.get('wait'):
            result = wait.stop(sid, quota.load())
            if result:
                return result
        handoff = modes.flow_state(sid).get('handoff') if state.get('flow') else None
        if handoff:
            data = {'session_id': raw, 'host': 'codex', 'handoff': str(handoff),
                    'cwd': str(payload.get('cwd') or ''), 'status': 'manager_required',
                    'at': time.time(), 'nested_spawn': False}
            config.write_json(config.STATE_ROOT / 'codex' / 'handoffs' / (_key(raw) + '.json'), data)
            return {'continue': False, 'stopReason': 'Ruti flow handoff is ready for the independent manager.',
                    'systemMessage': 'Ruti: checkpoint preserved; manager must continue in a separate harness.'}
    return None


def main():
    try:
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return 0
        result = handle(json.loads(raw or '{}'))
        if result:
            json.dump(result, sys.stdout, ensure_ascii=False)
    except Exception:
        # A damaged cache or unavailable project must not brick the client.
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

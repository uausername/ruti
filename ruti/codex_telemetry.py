"""Bounded, exact-session metadata from native Codex rollouts."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
from datetime import datetime

from . import config, sessions

LINE_LIMIT = 256 * 1024
TAIL_LIMIT = 1024 * 1024
MAX_LINES = 4096


def _identity(value):
    if not isinstance(value, str):
        return None
    raw = value.removeprefix('codex:')
    return raw if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', raw) else None


def _number(value, maximum):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 <= value <= maximum)
    except OverflowError:
        return False


def _timestamp(value, now):
    try:
        if not isinstance(value, str):
            return None
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        epoch = date.timestamp()
        return epoch if _number(epoch, now) else None
    except (ValueError, OverflowError, OSError):
        return None


def _string(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c).startswith('C') for c in value))


def _open(path):
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise OSError('symlink/reparse point refused')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('not a regular file')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _row(line):
    if len(line) > LINE_LIMIT:
        return {}
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError, RecursionError):
        return {}


def _window(value):
    if not isinstance(value, dict) or not _number(value.get('used_percent'), 100):
        return None
    reset = value.get('resets_at')
    if reset is not None and not _number(reset, 253402300799):
        return None
    return {'used_percentage': value['used_percent'], 'resets_at': reset}


def read(transcript_path, session_id, now=None):
    """Read only the header and bounded tail of a matching regular rollout."""
    raw = _identity(session_id)
    now = time.time() if now is None else now
    if raw is None or not isinstance(transcript_path, (str, Path)):
        return {}
    try:
        with _open(transcript_path) as stream:
            header = stream.readline(LINE_LIMIT + 1)
            meta = _row(header)
            payload = meta.get('payload')
            if (not header.endswith(b'\n') or meta.get('type') != 'session_meta'
                    or not isinstance(payload, dict) or payload.get('id') != raw
                    or ('session_id' in payload and payload['session_id'] != raw)):
                return {}
            size = os.fstat(stream.fileno()).st_size
            start = max(len(header), size - TAIL_LIMIT)
            stream.seek(start)
            tail = stream.read(TAIL_LIMIT)
        lines = tail.split(b'\n')
        if start > len(header):
            lines = lines[1:]
        # An unterminated final record may be in flight.
        lines = lines[:-1][-MAX_LINES:]
        result = {'source': 'codex-rollout', 'session_id': raw,
                  'transcript_path': str(transcript_path)}
        for line in lines:
            row = _row(line)
            at = _timestamp(row.get('timestamp'), now)
            p = row.get('payload')
            if at is None or not isinstance(p, dict):
                continue
            if row.get('type') == 'turn_context':
                for key, limit in (('model', 96), ('effort', 16)):
                    if _string(p.get(key), limit):
                        result[key] = p[key]
                        result[key + '_captured_at_epoch'] = at
            if row.get('type') != 'event_msg' or p.get('type') != 'token_count':
                continue
            limits = p.get('rate_limits')
            if isinstance(limits, dict):
                windows = {}
                for name, duration in (('five_hour', 300), ('seven_day', 10080)):
                    candidates = [limits.get(slot) for slot in ('primary', 'secondary')
                                  if isinstance(v := limits.get(slot), dict)
                                  and not isinstance(v.get('window_minutes'), bool)
                                  and v.get('window_minutes') == duration]
                    windows[name] = _window(candidates[0]) if len(candidates) == 1 else None
                if any(v is not None for v in windows.values()):
                    result.update(windows, captured_at_epoch=at)
            info = p.get('info')
            if isinstance(info, dict):
                usage = info.get('last_token_usage')
                window = info.get('model_context_window')
                tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
                if (_number(window, 1e15) and window > 12000
                        and _number(tokens, 1e15)):
                    remaining = max(window - 12000 - max(tokens - 12000, 0), 0)
                    percent = min(100, remaining / (window - 12000) * 100)
                    result.update(context_used_percentage=100 - math.floor(percent + .5),
                                  context_captured_at_epoch=at)
        return result
    except (OSError, ValueError, TypeError):
        return {}


def _cache_path(raw):
    return config.STATE_ROOT / 'codex' / 'telemetry' / (hashlib.sha256(raw.encode()).hexdigest() + '.json')


def capture(payload: dict, now=None):
    """Register a verified transcript and cache only parsed metadata."""
    if not isinstance(payload, dict):
        return {}
    result = read(payload.get('transcript_path'), payload.get('session_id'), now)
    if result:
        old = _cached(result['session_id'], time.time() if now is None else now)
        if old.get('transcript_path') == result.get('transcript_path'):
            result = {**old, **result}
        try:
            config.write_json(_cache_path(result['session_id']), result)
        except OSError:
            pass
    return result


def _cached(raw, now):
    """Validate a single exact-identity cache without refreshing its timestamp."""
    try:
        with _open(_cache_path(raw)) as stream:
            data = _row(stream.read(LINE_LIMIT + 1))
        if (data.get('source') != 'codex-rollout' or data.get('session_id') != raw
                or not isinstance(data.get('transcript_path'), str)):
            return {}
        # Reject unexpected fields rather than propagating cached private content.
        allowed = {'source', 'session_id', 'transcript_path', 'five_hour', 'seven_day',
                   'captured_at_epoch', 'context_used_percentage', 'context_captured_at_epoch',
                   'model', 'effort', 'model_captured_at_epoch', 'effort_captured_at_epoch'}
        if data.keys() - allowed:
            return {}
        for key, value in data.items():
            if key.endswith('_epoch') and not _number(value, now):
                return {}
            if key in ('model', 'effort') and not _string(value, 96 if key == 'model' else 16):
                return {}
            if key == 'context_used_percentage' and not _number(value, 100):
                return {}
            if key in ('five_hour', 'seven_day') and value is not None:
                if (not isinstance(value, dict) or set(value) != {'used_percentage', 'resets_at'}
                        or _window({'used_percent': value.get('used_percentage'),
                                    'resets_at': value.get('resets_at')}) is None):
                    return {}
        if (any(data.get(k) is not None for k in ('five_hour', 'seven_day'))
                and 'captured_at_epoch' not in data):
            return {}
        for key in ('context', 'model', 'effort'):
            field = 'context_used_percentage' if key == 'context' else key
            if field in data and key + '_captured_at_epoch' not in data:
                return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def load(session_id=None, now=None):
    """Load only this Codex session's registered metadata, refreshing if possible."""
    if sessions.current_host() != 'codex':
        return {}
    identity = sessions.current_session_id() if session_id is None else session_id
    raw = _identity(identity)
    current = _identity(sessions.current_session_id())
    if raw is None or raw != current:
        return {}
    now = time.time() if now is None else now
    data = _cached(raw, now)
    if not data:
        return {}
    try:
        fresh = read(data['transcript_path'], raw, now)
        if not fresh:
            return data
        # A bounded tail may no longer contain the older quota observation.
        # Carry it forward with its original timestamp, never with refresh time.
        merged = dict(data)
        merged.update(fresh)
        try:
            config.write_json(_cache_path(raw), merged)
        except OSError:
            pass
        return merged
    except (OSError, ValueError, TypeError):
        return {}

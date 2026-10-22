"""Readable, credential-redacted turns from Steward's lossless local archive."""
import json
from pathlib import Path
import re

from .jobs import instant
from .state import StateError

LIMIT = 1_000_000
ID = re.compile(r'(?:wake-[0-9]+|run-[0-9a-f]{32})\Z')


def redact(text):
    text = re.sub(r'-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?-----END (?:[A-Z ]*PRIVATE KEY)-----', '[REDACTED]', text, flags=re.S)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', r'\1 [REDACTED]', text)
    text = re.sub(r'\b(?:ya29\.[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b', '[REDACTED]', text)
    text = re.sub(r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16}|[0-9]{8,12}:[A-Za-z0-9_-]{30,})\b', '[REDACTED]', text)
    # Covers env assignments, JSON tool inputs and command-line credential options.
    return re.sub(r'''(?ix)(?<![\w-])(["']?(?:[a-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key))["']?\s*[:=]\s*|--(?:password|token|api-key|secret)\s+)("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r'\1"[REDACTED]"', text)


def redact_input(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'(?i)password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key', k)
                else redact_input(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_input(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def output(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(output(v) for v in value)
    if isinstance(value, dict) and 'text' in value:
        return output(value['text'])
    return compact(value)


def turns(path, agent):
    """Stream native JSONL; ignore bookkeeping, incomplete trailing lines and images."""
    tools = {}
    index = 0
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            at = row.get('timestamp')
            pieces = []
            if agent == 'claude' and row.get('type') in ('user', 'assistant'):
                msg = row.get('message', {})
                role = row['type']
                content = msg.get('content', [])
                if isinstance(content, str):
                    content = [{'type': 'text', 'text': content}]
                for block in content:
                    kind = block.get('type')
                    if kind == 'text':
                        pieces.append((role, 'text', None, block.get('text', '')))
                    elif kind == 'thinking':
                        pieces.append(('assistant', 'thinking', None, block.get('thinking', '')))
                    elif kind == 'tool_use':
                        tools[block.get('id')] = block.get('name')
                        pieces.append(('assistant', 'tool_call', block.get('name'), compact(block.get('input', {}))))
                    elif kind == 'tool_result':
                        pieces.append(('tool', 'tool_result', tools.get(block.get('tool_use_id')), output(block.get('content', ''))))
            elif agent == 'codex' and row.get('type') == 'response_item':
                p = row.get('payload', {})
                kind = p.get('type')
                if kind == 'message' and p.get('role') in ('user', 'assistant'):
                    for block in p.get('content', []):
                        if block.get('type') in ('input_text', 'output_text', 'text'):
                            pieces.append((p['role'], 'text', None, block.get('text', '')))
                elif kind == 'reasoning':
                    for block in p.get('summary', []) + (p.get('content') or []):
                        if isinstance(block, dict) and 'text' in block:
                            pieces.append(('assistant', 'thinking', None, block['text']))
                elif kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
                    name = p.get('name') or ('shell' if kind == 'local_shell_call' else None)
                    tools[p.get('call_id') or p.get('id')] = name
                    value = p.get('arguments', p.get('input', p.get('action', {})))
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except ValueError:
                            pass
                    pieces.append(('assistant', 'tool_call', name, compact(value)))
                elif kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
                    pieces.append(('tool', 'tool_result', tools.get(p.get('call_id')), output(p.get('output', ''))))
            for role, kind, name, text in pieces:
                text = compact(redact_input(json.loads(text))) if kind == 'tool_call' else redact(text)
                yield dict(i=index, at=at, role=role, type=kind, name=redact(name) if name else None,
                           text=text[:8000], truncated=len(text) > 8000)
                index += 1


def end_from_sessions(directory, sessions):
    """Last timestamped native turn, not file mtime or bookkeeping time."""
    times = []
    for session in sessions:
        for turn in turns(directory / 'sessions' / session['file'], session['agent']):
            if turn['at'] is not None:
                try:
                    times.append(instant(turn['at']))
                except (ValueError, TypeError, StateError):
                    continue
    return max(times).isoformat() if times else None


def metadata(data, identity):
    if not ID.fullmatch(identity):
        raise StateError('unknown transcript id')
    if identity.startswith('wake-'):
        paths = [Path(data) / 'wakes' / identity[5:] / 'transcript-meta.json']
    else:
        paths = list((Path(data) / 'runs').glob('*/' + identity[4:] + '/transcript-meta.json'))
    if len(paths) != 1 or not paths[0].is_file():
        raise StateError('unknown or unarchived run')
    meta = json.loads(paths[0].read_text())
    if not meta['complete']:
        raise StateError('run transcript is not fully archived')
    if not meta['ended_at']:
        meta['ended_at'] = end_from_sessions(paths[0].parent, meta['sessions'])
    if not meta['ended_at']:
        raise StateError('run completion time is unknown')
    return paths[0].parent, meta


def document(data, identity):
    directory, meta = metadata(data, identity)
    doc = {k: meta[k] for k in ('id', 'kind', 'job', 'started_at', 'ended_at', 'status')}
    doc.update(schema='steward.transcript.v1', sessions=[], truncated=False,
               bytes=sum(s['bytes'] for s in meta['sessions']))
    doc['truncated'] = len(meta['sessions']) > 8
    for session in meta['sessions'][:8]:
        path = directory / 'sessions' / session['file']
        items = []
        for turn in turns(path, session['agent']):
            if len(items) == 2000:
                doc['truncated'] = True
                break
            items.append(turn)
            doc['truncated'] |= turn['truncated']
        doc['sessions'].append({k: redact(session[k]) if isinstance(session[k], str) else session[k]
                                for k in ('agent', 'model', 'thread', 'label')})
        doc['sessions'][-1]['turns'] = items
    size = lambda: len(compact(doc).encode())
    current = size()
    cap = LIMIT - 1  # CLI's trailing newline is part of the serialized payload.
    if current > cap:
        doc['truncated'] = True
        # Subtract exact serialized deltas instead of serializing a megabyte per turn.
        for session in doc['sessions']:
            for turn in session['turns']:
                if current <= cap:
                    break
                if turn['type'] == 'tool_result' and turn['text']:
                    before = len(compact(turn).encode())
                    turn.update(text='', truncated=True)
                    current -= before - len(compact(turn).encode())
        for session in doc['sessions']:
            while current > cap and session['turns']:
                removed = session['turns'].pop(0)
                current -= len(compact(removed).encode()) + (1 if session['turns'] else 0)
    return doc

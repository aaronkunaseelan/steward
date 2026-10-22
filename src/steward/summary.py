"""steward.run-summary.v1 plus transcript.json, written beside each complete archive for the edge."""
from collections import Counter
from datetime import timedelta
import json

from .jobs import instant
from .state import StateError, atomic_file
from .transcripts import compact, document, redact, turns

HORIZON = timedelta(days=28)
PER_SWEEP = 40


def records(directory):
    path, result = directory / 'records.jsonl', []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            result.append(record)
    return result


def tokens(path, agent):
    """Model responses and token totals from one native session; usage repeats per Claude content block."""
    calls, total, claude, last = 0, Counter(), {}, None
    with path.open() as stream:
        lines = stream.read().splitlines()
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        if agent == 'claude' and row.get('type') == 'assistant' and isinstance(message.get('usage'), dict):
            claude[message.get('id') or len(claude)] = message['usage']
        elif agent == 'codex' and row.get('type') == 'event_msg' and (row.get('payload') or {}).get('type') == 'token_count':
            usage = ((row['payload'].get('info') or {}).get('total_token_usage')) or {}
            if usage and usage.get('total_tokens') != last:
                calls, last = calls + 1, usage.get('total_tokens')
                cached = usage.get('cached_input_tokens', 0)
                total = Counter(input=usage.get('input_tokens', 0) - cached, output=usage.get('output_tokens', 0),
                                cache_write=usage.get('cache_write_input_tokens', 0), cache_read=cached)
        elif agent == 'pi' and isinstance(message.get('usage'), dict) and 'input' in message['usage']:
            u = message['usage']
            calls += 1
            total.update(input=u.get('input', 0), output=u.get('output', 0), cache_write=u.get('cacheWrite', 0), cache_read=u.get('cacheRead', 0))
    for u in claude.values():
        total.update(input=u.get('input_tokens', 0), output=u.get('output_tokens', 0),
                     cache_write=u.get('cache_creation_input_tokens', 0), cache_read=u.get('cache_read_input_tokens', 0))
    return calls + len(claude), total


def landed(directory, kind, row, changes):
    if kind == 'wake':
        start, end = instant(row['started_at']), instant(row['ended_at'])
        return dict(Counter(verb for at, verb in changes if start <= at <= end))
    counts, published = Counter(), 0
    for record in records(directory):
        counts.update({k: v for k, v in (record.get('counts') or {}).items() if type(v) is int and v > 0})
        published += len(record['published']) if isinstance(record.get('published'), list) else 0
    if published:
        counts['published'] = published
    return dict(counts.most_common(16))


def summary(data, directory, meta, row, changes):
    doc = document(data, meta['id'])
    agents, calls, total, tools = [], 0, Counter(), Counter()
    for session in meta['sessions'][:8]:
        path = directory / 'sessions' / session['file']
        n, t = tokens(path, session['agent'])
        calls, total = calls + n, total + t
        tools.update(turn['name'] for turn in turns(path, session['agent']) if turn['type'] == 'tool_call' and turn['name'])
        agents.append(dict(agent=session['agent'], model=(session.get('model') or None) and session['model'][:80],
                           label=(session.get('label') or None) and session['label'][:40], turns=session.get('turns', 0)))
    manifest = directory / 'manifest.json'
    manifest = json.loads(manifest.read_text()) if manifest.exists() else {}
    error = next((r['error'] for r in records(directory) if r.get('error')), None)
    start, end = instant(meta['started_at']), instant(meta['ended_at'])
    reason = row.get('gate_reason') if meta['kind'] == 'wake' else row.get('reason')
    return doc, dict(
        schema='steward.run-summary.v1', id=meta['id'], kind=meta['kind'], job=meta['job'], started_at=meta['started_at'],
        ended_at=meta['ended_at'], duration_s=round(max(0, (end - start).total_seconds()), 1), status=meta['status'][:40],
        reason=reason[:200] if reason else None, exit_code=manifest.get('exit_code'),
        error=redact(str(error))[:200] if error else None, agents=agents, model_calls=calls,
        tokens=dict(**{k: total[k] for k in ('input', 'output', 'cache_write', 'cache_read')},
                    weighted=round(total['input'] + total['output'] + 1.25 * total['cache_write'] + 0.1 * total['cache_read']))
        if meta['sessions'] else None,
        tools=dict(tools.most_common(40)), landed=landed(directory, meta['kind'], dict(row, ended_at=meta['ended_at']), changes),
        truncated=doc['truncated'], bytes=doc['bytes'])


def write(data, directory, meta, row, changes, now):
    """Write transcript.json then summary.json (its presence means shippable); rewrite when the archive changed."""
    target = directory / 'summary.json'
    if not meta['complete'] or not meta['ended_at'] or instant(meta['started_at']) < now - HORIZON:
        return False
    if target.exists() and target.stat().st_mtime_ns >= (directory / 'transcript-meta.json').stat().st_mtime_ns:
        return False
    try:
        doc, numbers = summary(data, directory, meta, row, changes)
    except StateError:
        return False
    atomic_file(directory / 'transcript.json', compact(doc).encode())
    atomic_file(target, compact(numbers).encode())
    return True

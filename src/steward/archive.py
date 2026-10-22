"""Steward-owned, lossless session copies. No database migrations or producer stores."""
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

from .jobs import UTC, current, instant, now_utc
from .state import atomic_file
from . import summary
from .transcripts import end_from_sessions, turns

NATIVE_ID = re.compile(r'[A-Za-z0-9_-]+\Z')


def tty_info(identity):
    result = subprocess.run(['tty', 'show', identity], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def links(value):
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def references(directory, manifest):
    found = links(manifest.get('links', []))[:]
    records = directory / 'records.jsonl'
    if records.exists():
        with records.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    found.extend(links(row.get('links', [])))
    return [r for r in found if isinstance(r, dict) and (r.get('tty_run') or r.get('session'))]


def resolve(ref, home):
    info = tty_info(ref['tty_run']) if ref.get('tty_run') else ref
    thread = info.get('thread') or info.get('session')
    agent = info.get('agent')
    if not isinstance(thread, str) or not NATIVE_ID.fullmatch(thread):
        raise ValueError('native session id unavailable')
    if agent == 'claude':
        root = home / '.claude/projects'
        cwd = info.get('cwd')
        if cwd:
            project = re.sub(r'[^A-Za-z0-9-]', '-', cwd)
            paths = [root / project / (thread + '.jsonl')]
        else:
            paths = list(root.glob('*/' + thread + '.jsonl'))
    elif agent == 'codex':
        paths = list((home / '.codex/sessions').glob('*/*/*/rollout-*-' + thread + '.jsonl'))
    else:
        raise ValueError('unsupported agent session')
    if len(paths) != 1 or not paths[0].is_file():
        raise ValueError('native session file unavailable')
    label, model = ref.get('label'), info.get('model')
    if label is not None and not isinstance(label, str):
        raise ValueError('invalid session label')
    return dict(agent=agent, model=model if isinstance(model, str) else None, thread=thread, label=label,
                source=str(paths[0]), ended_at=info.get('ended'))


def copy_session(source, target):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as stream:
            shutil.copyfileobj(stream, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def session_model(path, agent):
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            value = row.get('message', {}).get('model') if agent == 'claude' else row.get('payload', {}).get('model')
            if isinstance(value, str):
                return value
    return None


def archive(directory, header, refs, now=None, home=None):
    """Serialize the job/wake finalizer with the tick backfill for this run only."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'transcript.lock').open('a') as lock:
        os.chmod(directory / 'transcript.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _archive(directory, header, refs, now, home)


def _archive(directory, header, refs, now=None, home=None):
    """Retry missing sources; recopy growing sources only in the first hour after end."""
    now, home = now or now_utc(), Path(home or Path.home())
    path = directory / 'transcript-meta.json'
    old = json.loads(path.read_text()) if path.exists() else None
    if old and old['complete']:
        end = instant(old['ended_at']) if old['ended_at'] else None
        if end is not None and now > end + timedelta(hours=1):
            return old
        # No tty call on a settled archive, even during the growth window.
        resolved = old['sessions']
    else:
        resolved, missing = [], 0
        for ref in refs:
            try:
                session = resolve(ref, home)
                if not any((s['agent'], s['thread'], s['label']) == (session['agent'], session['thread'], session['label']) for s in resolved):
                    resolved.append(session)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                missing += 1
        if missing:
            # Keep partial copies, but never publish missing sessions as an empty success.
            header = dict(header, complete=False)
    meta = dict(header, complete=header.get('complete', True), sessions=[])
    if old and not meta['ended_at']:
        meta['ended_at'] = old['ended_at']
    for i, session in enumerate(resolved):
        session = dict(session)
        source = Path(session['source'])
        filename = session.get('file', f'{i:03}-{session["agent"]}-{session["thread"]}.jsonl')
        target = directory / 'sessions' / filename
        previous = next((s for s in (old or {}).get('sessions', []) if s['file'] == filename), None)
        source_end = session.pop('ended_at', None)
        end = meta['ended_at'] or source_end
        if end:
            meta['ended_at'] = end
        try:
            growing = previous and now <= instant(end) + timedelta(hours=1) if end else False
            if not target.exists() or (growing and source.stat().st_size > previous['bytes']):
                copy_session(source, target)
                session.update(bytes=target.stat().st_size, archived_at=now.isoformat(), file=filename)
                session['model'] = session.get('model') or session_model(target, session['agent'])
                session['turns'] = sum(1 for _ in turns(target, session['agent']))
            elif previous:
                session = previous
            else:
                session.update(bytes=target.stat().st_size, archived_at=now.isoformat(), file=filename,
                               turns=sum(1 for _ in turns(target, session['agent'])))
            meta['sessions'].append(session)
        except (OSError, UnicodeError):
            if previous:
                meta['sessions'].append(previous)
            else:
                meta['complete'] = False
    if not meta['ended_at']:
        meta['ended_at'] = end_from_sessions(directory, meta['sessions'])
    if not meta['ended_at']:
        meta['complete'] = False  # No honest completion evidence yet; retry, never invent an end.
    if meta != old:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_file(path, json.dumps(meta, sort_keys=True).encode())
    return meta


def wake(data, row, now=None, home=None):
    checkpoint = json.loads(row['checkpoint'] or '{}')
    header = dict(id=f'wake-{row["id"]}', kind='wake', job=None, started_at=row['started_at'],
                  ended_at=checkpoint.get('ended_at'), status=row['status'])
    refs = [{'tty_run': row['harness_run_id']}] if row['harness_run_id'] else []
    if not refs and checkpoint.get('model_calls') != 0 and row['status'] not in ('skipped', 'refused'):
        header['complete'] = False
    return archive(Path(data) / 'wakes' / str(row['id']), header, refs, now, home)


def job(data, row, manifest=None, now=None, home=None):
    directory = Path(data) / 'runs' / row['job'] / row['run_id']
    if manifest is None:
        path = directory / 'manifest.json'
        manifest = json.loads(path.read_text()) if path.exists() else {}
    header = dict(id='run-' + row['run_id'], kind='job', job=row['job'],
                  started_at=manifest.get('started_at') or row['started_at'] or row['planned_for'],
                  ended_at=manifest.get('ended_at') or row['ended_at'], status=manifest.get('status') or row['state'])
    refs = references(directory, manifest)
    spec = json.loads(row.get('spec_json') or '{}')
    argv = manifest.get('argv') or spec.get('argv', [])
    agent_job = spec.get('capability') or any(Path(a).name in ('agent-run', 'gsd') for a in argv)
    if agent_job and row['started_at'] and not refs:
        header['complete'] = False
    return archive(directory, header, refs, now, home)


def completion_times(since):
    """Legacy wakes did not store their end; their JSON completion lines did."""
    try:
        result = subprocess.run(['journalctl', '--user', '-u', 'steward-wake.service', '-u', 'steward.service',
                                 '--since', '@' + str(instant(since).timestamp()), '-o', 'json', '--no-pager'],
                                capture_output=True, text=True, timeout=1, check=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    ends = {}
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
            wake = json.loads(entry['MESSAGE'])
            if wake['status'] in ('completed', 'failed', 'skipped', 'refused') and type(wake['wake_id']) is int:
                ends[wake['wake_id']] = datetime.fromtimestamp(int(entry['__REALTIME_TIMESTAMP']) / 1_000_000, UTC)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return ends


def sweep(root, data, now=None, home=None):
    """Read only Steward state; failures are visible and retried next tick."""
    path = Path(root) / 'state.sqlite'
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    now = now or now_utc()
    result = dict(complete=0, pending=0, errors=0)
    missing_ends = []
    try:
        wakes = [dict(r) for r in db.execute("SELECT * FROM wakes WHERE status != 'gated'")]
        runs = [r for r in current(db, 'runs') if r['state'] not in ('planned', 'started')]
        for row in runs:
            if row['state'] in ('cancelled', 'refused') and not row['ended_at']:
                row['ended_at'] = db.execute('SELECT at FROM changes WHERE id=?', (row['change_id'],)).fetchone()[0]
        changes = [(instant(at), verb) for at, verb in db.execute(
            "SELECT at, verb FROM changes WHERE actor='model' AND at >= ?", ((now - summary.HORIZON).isoformat(),))]
    finally:
        db.close()
    archived = []
    for kind, rows in ((wake, wakes), (job, runs)):
        for row in rows:
            try:
                meta = kind(data, row, now=now, home=home)
                result['complete' if meta['complete'] else 'pending'] += 1
                archived.append((row, meta))
                if kind is wake and not meta['ended_at']:
                    missing_ends.append(row)
            except (OSError, ValueError, KeyError, TypeError):
                result['errors'] += 1
    if missing_ends:
        ends = completion_times(min(r['started_at'] for r in missing_ends))
        for row in missing_ends:
            end = ends.get(row['id'])
            if end is None or not instant(row['started_at']) <= end <= now:
                continue
            row['checkpoint'] = json.dumps(dict(json.loads(row['checkpoint'] or '{}'), ended_at=end.isoformat()))
            try:
                meta = wake(data, row, now=now, home=home)
                result['pending'] -= 1
                result['complete' if meta['complete'] else 'pending'] += 1
                archived.append((row, meta))
            except (OSError, ValueError, KeyError, TypeError):
                result['errors'] += 1
    # Edge files for the newest complete runs first; a backlog drains PER_SWEEP per tick.
    written = 0
    for row, meta in sorted(archived, key=lambda pair: pair[1]['started_at'], reverse=True):
        if written >= summary.PER_SWEEP:
            break
        directory = Path(data) / ('wakes/' + str(row['id']) if meta['kind'] == 'wake' else f"runs/{row['job']}/{row['run_id']}")
        try:
            written += summary.write(data, directory, meta, row, changes, now)
        except (OSError, ValueError, KeyError, TypeError):
            result['errors'] += 1
    return result

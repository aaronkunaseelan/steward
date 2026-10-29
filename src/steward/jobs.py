"""Versioned job and compute-calendar verbs. Call under the short writer lock."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from .settings import local_zone

from .state import StateError

UTC = timezone.utc


def run_severity(state):
    """Native run/wake classification; an unhandled state is a defect, never green."""
    if state in ('succeeded', 'completed', 'skipped', 'cancelled'):
        return 'ok'
    if state in ('planned', 'started', 'gated', 'running'):
        return 'running'
    return 'bad'
COMPUTE_GRID_MINUTES = 20
RELEASE_ROOT = Path.home() / '.local/lib'
SPEC_DEFAULTS = dict(heavy=False, deliver='digest', max_minutes=30, recurring=True, model='sol')


def now_utc():
    return datetime.now(UTC)


def instant(value):
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise StateError('an offset instant is required')
    return dt.astimezone(UTC)


def require(ok, message):
    if not ok:
        raise StateError(message)


def duration(value):
    require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*[mhd]', value), 'invalid duration')
    return timedelta(seconds=int(value[:-1]) * {'m': 60, 'h': 3600, 'd': 86400}[value[-1]])


def current(db, table):
    key = 'name' if table in ('jobs', 'interests', 'moments') else 'skip_id' if table == 'tick_skips' else 'run_id'
    return [view_row(db, table, r) for r in db.execute(f'SELECT t.* FROM {table} t WHERE version=(SELECT max(version) FROM {table} WHERE {key}=t.{key}) ORDER BY t.rowid')]


def list_jobs(db):
    """Current stored specs, without effective-run defaults or removed history."""
    return dict(schema='steward.jobs.v1', jobs=[
        dict(id=row['name'], state=row['state'], spec=json.loads(row['spec_json']))
        for row in sorted(current(db, 'jobs'), key=lambda row: row['name']) if row['state'] != 'removed'])


def get(db, table, identity):
    key = 'name' if table in ('jobs', 'interests', 'moments') else 'skip_id' if table == 'tick_skips' else 'run_id'
    row = db.execute(f'SELECT * FROM {table} WHERE {key}=? ORDER BY version DESC LIMIT 1', (identity,)).fetchone()
    require(row is not None, 'unknown ' + table)
    return view_row(db, table, row)


def is_extra(run):
    """Run-local metadata, not a job setting; retained across all run versions."""
    return json.loads(run.get('spec_json') or '{}').get('_extra', False)


def view_row(db, table, row):
    row = dict(row)
    if table == 'runs' and row['state'] == 'planned':
        extra = is_extra(row)
        row['spec_json'] = get(db, 'jobs', row['job'])['spec_json']
        if extra:
            try:
                spec = json.loads(row['spec_json'])
            except ValueError:
                spec = None
            # Invalid job specs are quarantined by the tick, including extra runs.
            row['spec_json'] = json.dumps(dict(spec if isinstance(spec, dict) else {}, _extra=True), sort_keys=True)
    return row


def change(db, verb, now, actor='owner', undo_of=None):
    require(actor in ('owner', 'model', 'maintenance', 'tick', 'migration'), 'invalid actor')
    identity = uuid.uuid4().hex
    db.execute('INSERT INTO changes VALUES (?,?,?,?,?,?)', (identity, now.isoformat(), actor, verb, verb, undo_of))
    return identity


def append(db, table, row, change_id):
    row = dict(row, change_id=change_id, version=row.get('version', 0) + 1)
    keys = list(row)
    db.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [row[k] for k in keys])
    return row


def validate_spec(spec, name, qmd_status=None):
    require(isinstance(name, str) and re.fullmatch('[a-z0-9-]{1,40}', name), 'invalid job name')
    require(isinstance(spec, dict), 'invalid spec')
    allowed = {'justification', 'argv', 'needs', 'heavy', 'deliver', 'max_minutes', 'recurring', 'capability', 'cwd', 'env_file', 'min_gap', 'model'}
    require(not set(spec) - allowed, 'unknown spec field')
    require(isinstance(spec.get('justification'), str) and spec['justification'].strip(), 'justification required')
    argv = spec.get('argv')
    require(isinstance(argv, list) and argv and all(isinstance(v, str) and v and '\0' not in v for v in argv), 'invalid argv')
    entry = Path(argv[0]).expanduser()
    command = str(entry) if entry.is_absolute() and entry.is_file() and os.access(entry, os.X_OK) else shutil.which(argv[0])
    resolved = Path(command).resolve() if command else None
    parts = resolved.relative_to(RELEASE_ROOT).parts if resolved and resolved.is_relative_to(RELEASE_ROOT) else ()
    require(len(parts) >= 4 and parts[1] == 'releases', 'Every job runs from a release: argv[0] must resolve inside an immutable release directory')
    needs = spec.get('needs')
    require(isinstance(needs, dict) and len(needs) == 1 and set(needs) <= {'ready_by', 'max_staleness', 'every'}, 'one need required')
    if 'ready_by' in needs:
        values = needs['ready_by']
        require(isinstance(values, list) and values and all(isinstance(v, str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', v) for v in values), 'invalid ready_by')
    elif 'every' in needs:
        every = needs['every']
        require(isinstance(every, dict) and set(every) == {'min', 'max'}, 'invalid every')
        require(duration(every['min']) <= duration(every['max']), 'reversed every')
    else:
        duration(needs['max_staleness'])
    if 'min_gap' in spec:
        duration(spec['min_gap'])
    spec = {**SPEC_DEFAULTS, **spec}
    require(spec['model'] in ('sol', 'haiku', 'opus'), 'job model must be sol, haiku or opus')
    for key in ('heavy', 'recurring'):
        require(type(spec[key]) is bool, 'invalid boolean')
    require(spec['deliver'] in ('digest', 'immediate', 'silent'), 'invalid delivery')
    require(type(spec['max_minutes']) is int and 1 <= spec['max_minutes'] <= 240, 'invalid runtime bound')
    for key in ('cwd', 'env_file'):
        if key in spec:
            require(isinstance(spec[key], str) and Path(spec[key]).is_absolute() and '\0' not in spec[key], 'absolute path required')
    if 'capability' in spec:
        require(isinstance(spec['capability'], str) and re.fullmatch('[a-z0-9-]{1,40}', spec['capability']), 'invalid capability')
        from .capabilities import validate
        if qmd_status is None:
            validate(spec['capability'])
        else:
            validate(spec['capability'], qmd_status=qmd_status)
    return spec


def owner_request(db, cid, owner_message):
    if owner_message is not None:
        require(db.execute('SELECT 1 FROM inbox WHERE update_id=?', (owner_message,)).fetchone(),
                'unknown owner message')
        db.execute('INSERT INTO message_meta VALUES (?,?)',
                   ('owner_request:' + cid, json.dumps(owner_message)))


def job_verb(db, verb, name, spec=None, now=None, actor='owner', owner_message=None):
    now = now or now_utc()
    with db:
        previous = next((r for r in current(db, 'jobs') if r['name'] == name), None)
        if verb == 'add':
            require(previous is None or previous['state'] == 'removed', 'job already exists')
            row = dict(name=name, version=previous['version'] if previous else 0, state='active')
        else:
            row = get(db, 'jobs', name)
            require(row['state'] != 'removed', 'job removed')
        if verb in ('add', 'edit'):
            row['spec_json'] = json.dumps(validate_spec(spec, name), sort_keys=True)
        else:
            row['state'] = {'pause': 'paused', 'resume': 'active', 'rm': 'removed'}[verb]
        cid = change(db, 'job ' + verb, now, actor)
        owner_request(db, cid, owner_message)
        append(db, 'jobs', row, cid)
        if verb == 'edit' and any(json.loads(previous['spec_json'])[key] != json.loads(row['spec_json'])[key]
                                  for key in ('heavy', 'max_minutes')):
            pending = [r for r in current(db, 'runs') if r['job'] == name and r['state'] == 'planned']
            for run in pending:
                append(db, 'runs', dict(run, state='cancelled', reason='job capacity edited; re-planned',
                                       ended_at=now.isoformat()), cid)
            if pending:
                from .runtime import placement, grid
                from .planner import rule
                job, runs = get(db, 'jobs', name), current(db, 'runs')
                default, reason = placement(job, runs, now)
                slots = [now] + [grid(now, True) + timedelta(minutes=COMPUTE_GRID_MINUTES*i) for i in range(109)]
                candidates = sorted(slots, key=lambda t: abs(t - default)) if default else slots
                at = next((t for t in candidates if rule(job, runs, t, now) is None), None)
                require(at is not None, 'edited job has no feasible reservation')
                plan(db, name, at, 'job capacity edited; ' + reason, now, cid)
        if verb == 'rm':
            # A removed job keeps no reservations; undo of the change restores them.
            for run in current(db, 'runs'):
                if run['job'] == name and run['state'] == 'planned':
                    append(db, 'runs', dict(run, state='cancelled', reason='job removed', ended_at=now.isoformat()), cid)
        return {'change_id': cid, 'job': name, 'status': row['state']}


def check_overlap(db, name, spec, at, exclude=None):
    if not spec['heavy']:
        return
    from .planner import expected
    runs = current(db, 'runs')
    # Reservations last their expected duration; the runaway limit bounds only started runs.
    end = at + timedelta(minutes=expected(runs, name, spec))
    for r in runs:
        if r['run_id'] == exclude or r['state'] not in ('planned', 'started'):
            continue
        other = json.loads(r['spec_json'])
        start = instant(r['started_at'] or r['planned_for'])
        if other['heavy']:
            minutes = other.get('_limit_minutes') or other['max_minutes'] if r['state'] == 'started' else expected(runs, r['job'], other)
            require(not (at < start + timedelta(minutes=minutes) and start < end), 'heavy runs overlap')


def plan(db, name, at, reason, now, cid, explicit=False, move=True, retry_of=None, extra=False):
    job = get(db, 'jobs', name)
    require(job['state'] == 'active', 'job is not active')
    spec = json.loads(job['spec_json'])
    old = next((r for r in current(db, 'runs') if r['job'] == name and r['state'] == 'planned' and not is_extra(r)), None) if move else None
    require(at >= now, 'cannot plan in the past')
    require(isinstance(reason, str) and reason.strip(), 'reason required')
    if retry_of:
        failed = get(db, 'runs', retry_of)
        require(failed['state'] == 'failed' and failed['job'] == name and not failed['retry_of'],
                'retry requires an original failed run')
        require(not any(r['retry_of'] == retry_of for r in current(db, 'runs')), 'already retried')
        # X-24 authorises a new reservation now; dispatch still enforces host/heavy guards.
    elif not explicit:
        from .planner import check
        check(db, job, at, now, old)
    else:
        check_overlap(db, name, spec, at, old['run_id'] if old else None)
    row = dict(old) if old else dict(run_id=uuid.uuid4().hex, version=0, job=name, started_at=None, ended_at=None, unit=None, exit_code=None, manifest=None)
    row.update(state='planned', planned_for=at.isoformat(), reason=reason,
               spec_json=json.dumps({'_extra': True} if extra else {}), retry_of=retry_of)
    return append(db, 'runs', row, cid)


def run_verb(db, verb, identity, reason, at=None, now=None, actor='owner', owner_message=None):
    now = now or now_utc()
    with db:
        cid = change(db, 'run ' + verb, now, actor)
        owner_request(db, cid, owner_message)
        if verb == 'cancel':
            row = get(db, 'runs', identity)
            require(row['state'] == 'planned', 'only planned runs can be cancelled')
            require(isinstance(reason, str) and reason.strip(), 'reason required')
            row.update(state='cancelled', reason=reason, ended_at=now.isoformat())
            row = append(db, 'runs', row, cid)
        else:
            extra = verb == 'now' and any(r['job'] == identity and r['state'] == 'planned'
                                         for r in current(db, 'runs'))
            if extra:
                require(actor != 'model', 'extra runs require a non-model actor')
                require(isinstance(reason, str) and reason.strip(), 'reason required')
                reason = 'extra run: ' + reason
            row = plan(db, identity, now if verb == 'now' else instant(at), reason, now, cid,
                       explicit=verb == 'now' and (actor == 'owner' or extra), move=not extra, extra=extra)
        return {'change_id': cid, 'run_id': row['run_id'], 'status': row['state']}


def undo(db, identity, now=None, actor='owner', owner_message=None, settings=None):
    """One change dispatcher for CLI and Telegram; each owner performs its undo."""
    now = now or now_utc()
    if db.execute('SELECT 1 FROM drafts WHERE change_id=?', (identity,)).fetchone():
        from . import drafts
        return drafts.undo(db, identity, actor=actor, now=now)
    if db.execute('SELECT 1 FROM memories WHERE change_id=?', (identity,)).fetchone():
        from . import memory
        return memory.undo(db, identity, actor)
    if db.execute('SELECT 1 FROM blocks WHERE change_id=? UNION SELECT 1 FROM demands WHERE change_id=?', (identity, identity)).fetchone():
        from . import calendar_b
        from .settings import config
        return calendar_b.undo(db, settings if settings is not None else config(), identity, now, actor)
    with db:
        original = db.execute('SELECT * FROM changes WHERE id=?', (identity,)).fetchone()
        require(original is not None, 'unknown change')
        require(original['verb'] != 'notify', 'sent messages cannot be undone')
        affected = []
        for table, key in (('jobs', 'name'), ('runs', 'run_id'), ('interests', 'name'), ('tick_skips', 'skip_id'), ('moments', 'name')):
            for r in db.execute(f'SELECT * FROM {table} WHERE change_id=?', (identity,)):
                r = dict(r)
                latest = get(db, table, r[key])
                require(latest['version'] == r['version'], 'a later change must be undone first')
                prior = db.execute(f'SELECT * FROM {table} WHERE {key}=? AND version=?', (r[key], r['version'] - 1)).fetchone()
                restored = dict(prior) if prior else dict(r, state='removed' if table in ('jobs', 'interests') else 'cancelled')
                restored['version'] = latest['version']
                if table == 'runs':
                    if restored['state'] == 'cancelled' and not restored['ended_at']:
                        restored['ended_at'] = now.isoformat()
                    require(latest['state'] != 'started' and restored['state'] != 'started', 'execution cannot be undone')
                    if restored['state'] == 'planned':
                        require(instant(restored['planned_for']) >= now, 'cannot restore an elapsed reservation')
                affected.append((table, restored))
        require(affected, 'change has no reversible versions')
        cid = change(db, 'undo', now, actor, identity)
        owner_request(db, cid, owner_message)
        for table, row in affected:
            append(db, table, row, cid)
        for table, row in affected:
            if table == 'runs' and row['state'] == 'planned':
                spec = json.loads(get(db, 'jobs', row['job'])['spec_json'])
                check_overlap(db, row['job'], spec, instant(row['planned_for']), row['run_id'])
        return {'change_id': cid, 'undo_of': identity}


def capability_sha(spec):
    """Git blob identity of the capability read by a run; missing stays unknown."""
    from . import mind
    import hashlib
    name = spec.get('capability')
    if not isinstance(name, str) or not re.fullmatch('[a-z0-9-]{1,40}', name):
        return None
    try:
        content = (mind.directory() / 'capabilities' / (name + '.md')).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


# The limit only stops a true runaway, not a slow run (owner X-35).
RUNAWAY_FACTOR = 10


def execution_profile(db, name, spec=None, runs=None):
    """One measured epoch and limit, shared by dispatch, state and promise readers."""
    import math
    spec = spec if spec is not None else json.loads(get(db, 'jobs', name)['spec_json'])
    sha = capability_sha(spec)
    since, previous, previous_state = None, None, None
    for row in db.execute('SELECT j.spec_json, j.state, c.at FROM jobs j JOIN changes c ON c.id=j.change_id '
                          'WHERE j.name=? ORDER BY j.version', (name,)):
        old = json.loads(row['spec_json'])
        identity = (old.get('argv'), old.get('capability'))
        if previous is None or identity != previous or previous_state == 'removed':
            since = instant(row['at'])
        previous, previous_state = identity, row['state']
    history = []
    for run in current(db, 'runs') if runs is None else runs:
        if run['job'] != name or not run['started_at'] or (since and instant(run['started_at']) < since):
            continue
        bound = json.loads(run.get('spec_json') or '{}')
        matches = (bound.get('argv') == spec.get('argv') and bound.get('capability') == spec.get('capability')
                   and (not spec.get('capability') or sha is not None and bound.get('_capability_sha') == sha))
        history.append((run, matches))
    # Returning to an earlier capability blob does not resurrect its old measurements.
    changed_at = max((instant(r['started_at']) for r, matches in history if not matches), default=None)
    succeeded = sorted((r for r, matches in history if matches and r['state'] == 'succeeded' and r['ended_at']
                        and (changed_at is None or instant(r['started_at']) > changed_at)),
                       key=lambda r: instant(r['ended_at']))
    minutes = [(instant(r['ended_at']) - instant(r['started_at'])).total_seconds() / 60 for r in succeeded[-5:]]
    return dict(capability_sha=sha,
                measured_at=succeeded[0]['ended_at'] if succeeded else None,
                measurement='measured' if succeeded else 'measuring',
                limit_minutes=max(5, math.ceil(RUNAWAY_FACTOR * max(minutes))) if minutes else spec.get('max_minutes', SPEC_DEFAULTS['max_minutes']),
                limit_source='learned' if minutes else 'first-run bound')


def spec_errors(db):
    row = db.execute("SELECT payload FROM senses WHERE name='job_specs' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0])['data'] if row else {}


def snapshot(db, now=None, interrupt_cap=3):
    from . import senses
    now = now or now_utc()
    runs = current(db, 'runs')
    jobs = []
    errors = spec_errors(db)
    for row in current(db, 'jobs'):
        if row['state'] == 'active':
            try:
                spec = json.loads(row['spec_json'])
            except ValueError:
                spec = {}
            if not isinstance(spec, dict):
                spec = {}
            planned = [r['planned_for'] for r in runs if r['job'] == row['name'] and r['state'] == 'planned']
            from .planner import cost, cadence_deadline
            deadline = cadence_deadline(row['name'], spec, runs) if row['name'] not in errors else None
            profile = execution_profile(db, row['name'], spec, runs) if row['name'] not in errors else dict(
                capability_sha=capability_sha(spec), measured_at=None, measurement='unknown', limit_minutes=None, limit_source=None)
            fields = dict.fromkeys(('argv', 'capability', 'min_gap', 'env_file'))
            fields.update(SPEC_DEFAULTS, cwd=str(Path.home()))
            fields.update(spec)
            jobs.append(dict(fields, spec='unknown: ' + errors[row['name']] if row['name'] in errors else 'ok', **profile, missed_at=deadline.isoformat() if deadline and deadline < now else None, cost=cost(runs, row['name']), name=row['name'], next_planned=min(planned, default=None)))
    from . import calendar_b
    fulfilment = calendar_b.fulfilments(db, now)
    live = calendar_b.live_demands(db, now)
    live_names = {r['name'] for r in live}
    inactive = [dict(r, state='expired' if r['state'] == 'active' else r['state'])
                for r in calendar_b.latest(db, 'demands') if r['name'] not in live_names]
    calendar_b_state = dict(demands=[dict(r, **fulfilment.get(r['name'], {})) for r in live],
                            inactive_demands=inactive,
                            fulfilment=fulfilment, movement=calendar_b.movement_ranking(db, now, fulfilment),
                            blocks=calendar_b.latest(db, 'blocks'))
    calendar = [dict(run_id=r['run_id'], job=r['job'], planned_for=instant(r['planned_for']).astimezone(local_zone()).isoformat(), reason=r['reason']) for r in runs if r['state'] == 'planned' and now <= instant(r['planned_for']) <= now + timedelta(days=1)]
    recent = sorted(runs, key=lambda r: r['started_at'] or r['planned_for'], reverse=True)[:10]
    from . import memory
    draw = db.execute('SELECT * FROM provocations ORDER BY week DESC LIMIT 1').fetchone()
    from .tick_skips import status
    from .notify import interrupt_status, notification_status
    from . import pages, messages
    return dict(notifications=notification_status(db), inbound_poll=messages.poll_status(db, now), pages=pages.status(db, now), interrupts=interrupt_status(db, now, interrupt_cap), tick_skips=status(db, now), experiments=memory.experiments(db), provocation=dict(draw) if draw else [], interests=current(db, 'interests'), jobs=jobs, calendar_b=calendar_b_state, calendar_c=calendar, last_runs=[{k: r[k] for k in ('run_id', 'job', 'state', 'planned_for', 'ended_at', 'reason')} for r in recent], changes=[dict(r) for r in db.execute('SELECT * FROM changes ORDER BY rowid DESC LIMIT 20')], job_count=sum(j.get('recurring', True) is True for j in jobs), senses=senses.snapshot(db, now))

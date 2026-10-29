"""Steward's intent/1 view for mech-d Page 2. No writes or producer calls."""
from .errors import safe_reason
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess

from .calendar_b import live_demands
from .settings import local_zone
from .jobs import now_utc, current, run_severity
from .notify import interrupt_data, notification_status
from .messages import poll_status
from .runtime import wake_times, data_root, manifest_path, finished_manifest, failure_reason
from .senses import snapshot
from .state import StateError
from .timing import moments, moment_obligation, job_obligation, model_called, clock_on, owner_time



def when(text):
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hm(dt):
    return dt.astimezone(local_zone()).strftime('%H:%M')


def clip(text, n=160):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'


def latest(db, table, key):
    return db.execute(f'SELECT * FROM {table} t WHERE version = (SELECT max(version) FROM {table} u WHERE u.{key} = t.{key})').fetchall()


def sounded(row):
    data = interrupt_data(row)
    if data is None or data.get('over_cap') is not False:
        return False
    return data.get('buzz') is True


def chain(db, now, inbound=None, runs=None):
    inbound = inbound if inbound is not None else poll_status(db, now)
    senses = {name: value for name, value in snapshot(db, now, runs=runs).items() if name not in ('health', 'place')}
    ages = [s['age'] for s in senses.values() if s['age'] is not None]
    bad = [f"{name} {s['verdict']}: {s['reason']}" for name, s in senses.items() if s['verdict'] != 'fresh']
    return {'kind': 'chain', 'layer': 'process', 'hops': [
        {'name': 'Steward senses', 'lag_s': int(max(ages)) if ages else None,
         'note': clip('; '.join(bad) if bad else f'all {len(senses)} fresh', 200),
         'warn': bool(bad)},
        {'name': 'Telegram inbound poll', 'lag_s': int((now - when(inbound['last_poll_ok_at'])).total_seconds()) if inbound['last_poll_ok_at'] else None,
         'note': inbound['reason'] or 'ok', 'warn': inbound['status'] != 'ok'}]}


# Retired, unenforced intent/1 field; removed in intent/2.
RETIRED_JOB_CAP = 8


def ticks(now, runner=None, db=None):
    out = {'window_h': 24, 'total': 0, 'idle': 0, 'launched': 0, 'woke': 0, 'skipped': 0}
    model_wakes = [when(r['started_at']) for r in db.execute('SELECT * FROM wakes')
                   if model_called(r) and now - timedelta(hours=24, minutes=5) <= when(r['started_at']) <= now] if db is not None else []
    try:
        lines = (runner or subprocess.run)(['journalctl', '--user', '-u', 'steward-tick.service',
                        '--since', '@' + str((now - timedelta(hours=24)).timestamp()), '--until', '@' + str(now.timestamp()),
                        '-o', 'json', '--no-pager'], capture_output=True, text=True, timeout=1, check=True).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(reason='tick journal unreadable: ' + safe_reason(exc))
    for line in lines:
        if not line.startswith('{'):
            continue
        try:
            entry = json.loads(line)
            tick = json.loads(entry['MESSAGE']) if isinstance(entry, dict) and 'MESSAGE' in entry else entry
            if not isinstance(tick, dict):
                continue
            at = when(tick.get('at'))
            if at is None and isinstance(entry, dict) and entry.get('__REALTIME_TIMESTAMP'):
                at = datetime.fromtimestamp(int(entry['__REALTIME_TIMESTAMP']) / 1_000_000, timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue  # Partial or human journal lines are not completed ticks.
        if at is not None and not now - timedelta(hours=24) <= at <= now:
            continue
        out['total'] += 1
        # Timestamp matching also repairs old event ticks that had no wake key.
        # The journal timestamp is emitted just after the nonblocking unit start.
        matched = next((w for w in model_wakes if at and -60 <= (w - at).total_seconds() <= 300), None)
        if matched is not None:
            out['woke'] += 1
            model_wakes.remove(matched)
        elif tick.get('skipping') or tick.get('status') == 'writer_busy':
            out['skipped'] += 1
        elif tick.get('started'):
            out['launched'] += 1
        else:
            out['idle'] += 1
    return out


def document(state, settings, now=None, data=None):
    path = Path(state).expanduser().resolve()
    if path.is_dir():
        path /= 'state.sqlite'
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')  # One coherent read snapshot, without Steward's writer lock.
        return steward_doc(db, now or now_utc(), settings, data=data)
    finally:
        db.close()


def schedule(runs, now, settings, owner_moments=()):
    from .planner import observed_minutes
    runs = [dict(r) for r in runs]
    end = now + timedelta(hours=36)
    items = []
    for row in runs:
        at = when(row['planned_for'])
        if row['state'] != 'planned' or not now <= at <= end:
            continue
        spec = json.loads(row['spec_json'])
        learned = observed_minutes(runs, row['job'])
        items.append(dict(at=at.astimezone(timezone.utc).isoformat(), kind='job', job=clip(row['job'], 200),
                          est_min=learned, heavy=bool(spec.get('heavy')), reason=clip(row['reason'], 200) if row['reason'] else None,
                          run_id=row['run_id']))
    day = now.astimezone(local_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
    wake_slots = set()
    for offset in range(3):
        for clock in set(wake_times(settings)) | {m['start'] for m in owner_moments}:
            for fold in (0, 1):
                slot = clock_on((day + timedelta(days=offset)).date(), clock, fold)
                if slot is not None and now <= slot <= end:
                    wake_slots.add(slot)
    items.extend(dict(at=slot.isoformat(), kind='wake', job=None, est_min=None, heavy=False, reason=None, run_id=None)
                 for slot in wake_slots)
    return dict(kind='schedule', layer='process', now=now.astimezone(timezone.utc).isoformat(), window_h=36,
                items=sorted(items, key=lambda i: i['at'])[:400])


def recent_runs(db, runs, now, data):
    from . import archive, transcripts
    candidates = []
    for row in db.execute('SELECT * FROM wakes'):
        checkpoint = json.loads(row['checkpoint'] or '{}')
        candidates.append(dict(id=f'wake-{row["id"]}', kind='wake', job=None, started_at=row['started_at'],
                               ended_at=checkpoint.get('ended_at'), status='running' if row['status'] == 'gated' else row['status'],
                               sessions=int(bool(row['harness_run_id']) or bool(checkpoint.get('model_calls'))),
                               turns=None, reason=clip(row['gate_reason'], 200) if row['gate_reason'] else None, transcript=False))
    for row in runs:
        if row['state'] == 'planned':
            continue
        candidates.append(dict(id='run-' + row['run_id'], kind='job', job=clip(row['job'], 200),
                               started_at=row['started_at'] or row['planned_for'], ended_at=row['ended_at'], status=row['state'],
                               sessions=0, turns=None, reason=clip(row['reason'], 200) if row['reason'] else None, transcript=False))
    candidates = sorted((r for r in candidates if now - timedelta(days=7) <= when(r['started_at']) <= now),
                        key=lambda r: when(r['started_at']), reverse=True)[:100]
    for item in candidates:
        item['severity'] = run_severity(item['status'])
        try:
            directory, meta = transcripts.metadata(data, item['id'])
            if not all((directory / 'sessions' / s['file']).is_file() for s in meta['sessions']):
                continue
            item.update(ended_at=meta['ended_at'], sessions=len(meta['sessions']),
                        turns=sum(s['turns'] for s in meta['sessions']), transcript=True)
        except (StateError, OSError, ValueError, KeyError, TypeError):
            if item['kind'] == 'job':
                directory = data / 'runs' / item['job'] / item['id'][4:]
                try:
                    manifest = json.loads((directory / 'manifest.json').read_text())
                    refs = archive.references(directory, manifest)
                    item['sessions'] = len({(r.get('tty_run'), r.get('session'), r.get('label')) for r in refs})
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # No archive: turns stay unknown and transcript stays unavailable.
    return dict(kind='runs', layer='process', items=candidates)


def steward_doc(db, now, settings, data=None):
    inbound = poll_status(db, now)
    local = now.astimezone(local_zone())
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + timedelta(days=1)
    today = lambda dt: dt is not None and day0 <= dt < day1
    from .jobs import spec_errors, execution_profile
    invalid_specs = spec_errors(db)
    jobs = {r['name']: json.loads(r['spec_json']) for r in latest(db, 'jobs', 'name')
            if r['state'] == 'active' and r['name'] not in invalid_specs}
    runs = current(db, 'runs')
    # mech-d samples independently of the tick: completed manifests can precede
    # their folded rows. Resolve them once, read-only, for every intent judgement.
    for row in runs:
        if row['state'] != 'started':
            continue
        path = manifest_path(data_root(data), row)
        manifest = finished_manifest(path, row, now)
        if manifest:
            row.update(state=manifest['status'], ended_at=manifest['ended_at'],
                       exit_code=manifest['exit_code'], manifest=str(path))
            if manifest['exit_code']:
                row['reason'] = failure_reason(manifest['exit_code'], path.parent)
    # A digest's several outbox items share one Telegram message id.
    sent = list({r['message_id']: r for r in db.execute("SELECT * FROM outbox WHERE state = 'sent'")
                 if today(when(r['sent_at']))}.values())
    summaries = {r['id']: r['summary'] for r in db.execute('SELECT id, summary FROM changes')}

    def at(hhmm):
        return clock_on(day0.date(), hhmm)

    def promise(label, target, result, why_kept, why_due):
        met = when(result['completed_at'])
        why = why_kept.format(owner_time(met, '%H:%M')) if met else why_due
        if result['state'] == 'recovered':
            why = f'Missed at {target}; recovered: ' + why
        return {'at': target, 'label': label, 'state': result['state'],
                'met_at': hm(met) if met else None, 'why': clip(why)}

    def activated(name):
        """When the job last became active (add or resume): a target before that was never promised (F-59)."""
        since, previous = None, None
        for state, moment in db.execute('SELECT j.state, c.at FROM jobs j JOIN changes c ON c.id=j.change_id '
                                        'WHERE j.name=? ORDER BY j.version', (name,)):
            if state == 'active' and previous != 'active':
                since = when(moment)
            previous = state
        return since

    promises = []
    for name, spec in jobs.items():
        since = activated(name)
        profile = execution_profile(db, name, spec, runs)
        for target in spec.get('needs', {}).get('ready_by', []):
            if since is not None and at(target) < since:
                continue
            if profile['measured_at'] is None or at(target) < when(profile['measured_at']):
                promises.append(dict(at=target, label=name, state='quiet', met_at=None,
                                     why=f'{name}: measuring; no ready_by promise before a succeeded measured run.'))
            else:
                promises.append(promise(name, target, job_obligation(runs, name, at(target), now),
                                        'Finished at {}.', f'{name} is reserved to finish before {target}.'))
    for moment in moments(db):
        result = moment_obligation(db, moment, now)
        item = promise(moment['name'].replace('_', ' ').capitalize(), moment['deadline'],
                       result, 'Posted to Today at {}.', f"{moment['name']} in Today; starts {moment['start']}.")
        if result['state'] == 'missed' and now < at(moment['deadline']):
            item['why'] = 'Completed model wake did not deliver this moment to Today.'
        promises.append(item)
    promises.sort(key=lambda p: p['at'])

    pending_blocks = [b for b in latest(db, 'blocks', 'event_key') if b['state'] in ('intended', 'written')]
    yours = []
    # Pending writes cannot replace the last confirmed provider baseline.
    for b in db.execute("SELECT b.* FROM blocks b WHERE b.state='applied' AND b.version="
                        "(SELECT max(version) FROM blocks WHERE event_key=b.event_key "
                        "AND state IN ('applied','removed','deleted'))"):
        p = json.loads(b['payload'])
        start, end = when(p.get('start')), when(p.get('end'))
        if today(start):
            kind = 'block'
            yours.append({'from': hm(start), 'to': hm(min(end, day1 - timedelta(minutes=1))), 'kind': kind, 'label': clip(b['demand'], 40),
                          'why': clip(f"{b['demand']}: placed by Steward")})
    cal = db.execute("SELECT payload FROM senses WHERE name = 'calendars' AND status = 'ok' ORDER BY id DESC LIMIT 1").fetchone()
    for e in (json.loads(cal[0])['data']['events'] if cal else []):
        if e.get('all_day') or e.get('transparency') == 'transparent' or '@group.calendar.google.com' in e.get('calendar', ''):
            continue
        start, end = when(e['start'].get('dateTime')), when(e['end'].get('dateTime'))
        if start and end and start < day1 and end > day0:
            ooo = e.get('eventType') == 'outOfOffice'
            yours.append({'from': hm(max(start, day0)), 'to': hm(min(end, day1 - timedelta(minutes=1))), 'kind': 'busy',
                          'label': 'Out of office' if ooo else 'Busy', 'why': 'Your main calendar: Steward places nothing here.' if ooo else 'A meeting on your main calendar.'})

    machine = []
    for r in runs:
        start = when(r['started_at']) or when(r['planned_for'])
        if not today(start) or r['state'] == 'cancelled':
            continue
        end = when(r['ended_at']) or start + timedelta(minutes=int(jobs.get(r['job'], {}).get('max_minutes') or 15) if r['state'] == 'planned' else 5)
        state = {'succeeded': 'ok', 'failed': 'failed', 'refused': 'failed', 'planned': 'planned', 'started': 'running'}.get(r['state'], 'failed')
        why = jobs.get(r['job'], {}).get('justification') if state in ('ok', 'planned', 'running') else r['reason']
        machine.append({'from': hm(start), 'to': hm(min(max(end, start + timedelta(minutes=3)), day1 - timedelta(minutes=1))),
                        'job': clip(r['job']), 'state': state, 'severity': run_severity(r['state']), 'heavy': bool(jobs.get(r['job'], {}).get('heavy')),
                        'why': clip(f"{r['reason']} · {why}" if state == 'planned' else why or r['reason'])})
    machine.sort(key=lambda m: m['from'])

    attention = []
    for r in sorted(sent, key=lambda r: r['sent_at']):
        t = when(r['sent_at'])
        what = {'Today': 'Orientation or day close', 'Finds': 'A Find (digest)' if r['delivery'] == 'digest' else 'A Find',
                'Alerts': 'Something Steward could not fix itself', 'Talk': 'A reply to you'}.get(r['topic'])
        attention.append({'at': hm(t), 'topic': r['topic'], 'buzz': sounded(r),
                          'why': clip(what or summaries.get(r['change_id']) or 'What changed and how to undo it')})

    skips = [[hm(max(when(r['from_at']), day0)), hm(min(when(r['to_at']), day1 - timedelta(minutes=1)))]
             for r in latest(db, 'tick_skips', 'skip_id') if r['state'] == 'active' and when(r['to_at']) > day0 and when(r['from_at']) < day1]
    signals = db.execute("SELECT count(*) FROM senses WHERE at >= ? AND name NOT IN ('health','place')", (day0.astimezone(timezone.utc).isoformat(),)).fetchone()[0]
    wakes = sum(model_called(r) and today(when(r['started_at'])) for r in db.execute('SELECT * FROM wakes'))
    by = dict(db.execute('SELECT actor, count(*) FROM changes WHERE at >= ? GROUP BY actor', (day0.astimezone(timezone.utc).isoformat(),)))
    buzzes = sum(a['buzz'] for a in attention)
    sol_sense = snapshot(db, now)['sol']
    sol = sol_sense.get('data', {}) if sol_sense['status'] == 'ok' else {}
    fraction = sol.get('window_elapsed_fraction')
    demands = [(r['name'], json.loads(r['spec_json'])) for r in live_demands(db, now)]
    from .calendar_b import fulfilments, movement_ranking
    doses = fulfilments(db, now)
    ranking = movement_ranking(db, now, doses)

    def demand_line(name, spec):
        dose = doses.get(name)
        if dose is None:
            return f"1 × {spec['minutes']} min"
        if 'target_min' in dose:
            if dose['unknown']:
                return f"unknown/{dose['target_min']} min; held: {dose['unknown']}"
            return (f"{dose['done_min']:g}/{dose['target_min']} min; "
                    f"{dose['planned_min']} planned; {dose['remaining']:g} remaining; "
                    f"{len(dose['unclassified'])} unclassified")
        if dose['unknown']:
            return f"unknown/{spec['count']} a week; held: {dose['unknown']}"
        return (f"{len(dose['done'])}/{spec['count']} a week; "
                f"{len(dose['planned'])} planned; {dose['remaining']} remaining; "
                f"{len(dose['unclassified'])} unclassified")
    demand_rows = [[clip(n), clip(demand_line(n, s) +
                   (', tentative' if s.get('transparency') == 'transparent' else ''), 200),
                   clip(s.get('justification'), 120)] for n, s in demands]
    if ranking['options'] or ranking['unknown']:
        demand_rows.extend(['Movement options', clip(line, 200), 'Watch targets, not calendar slots']
                           for line in ranking['description'].splitlines())
    owner_edits = {block['event_key'] for block in latest(db, 'blocks', 'event_key') if block['state'] == 'removed'}
    for block in db.execute("SELECT b.* FROM blocks b JOIN changes c ON c.id=b.change_id WHERE c.actor='tick'"):
        if block['state'] == 'removed':
            owner_edits.add(block['event_key'])
        elif block['state'] == 'applied':
            prior = db.execute('SELECT payload FROM blocks WHERE event_key=? AND version<? ORDER BY version DESC LIMIT 1',
                               (block['event_key'], block['version'])).fetchone()
            if prior:
                before, after = json.loads(prior[0]), json.loads(block['payload'])
                if any(before.get(key) != after.get(key) for key in ('start', 'end', 'title', 'description')):
                    owner_edits.add(block['event_key'])
    memories = db.execute("SELECT key FROM memories WHERE layer = 'day' ORDER BY key DESC LIMIT 1").fetchone()
    synth = db.execute("SELECT length(content) FROM memories WHERE layer = 'synthesis' ORDER BY version DESC LIMIT 1").fetchone()
    lessons = db.execute("SELECT content FROM memories WHERE layer = 'lessons' ORDER BY version DESC LIMIT 1").fetchone()
    tick_evidence = ticks(now, db=db)
    chain_block = chain(db, now, inbound, runs=runs)
    if 'reason' in tick_evidence:
        chain_block['hops'].insert(0, dict(name='ticks', lag_s=None, note=tick_evidence['reason'], warn=True))
    blocks = [
        {'kind': 'day', 'layer': 'intent', 'tz': local_zone().key, 'date': day0.strftime('%Y-%m-%d'), 'promises': promises,
         'yours': yours, 'machine': machine, 'attention': attention, 'quiet': [], 'skips': skips,
         'buzz_cap': settings.get('interrupt_cap', 3)},
        {'kind': 'funnel', 'layer': 'intent', 'title': 'Your attention', 'sub': 'today · filtered down', 'note': 'Bar length is the square root of the count, so small numbers stay visible.',
         'steps': [['signals seen', signals, 'Calendar, gsd, qmd, machine and Sol reads today'],
                   ['wakes', wakes, 'Times the gate judged something new enough to think about'],
                   ['decisions', by.get('model', 0), f"Changes Steward made (you made {by.get('owner', 0)})"],
                   ['told you', len(sent), 'Messages sent today'],
                   ['interrupted', buzzes, 'Explicit interrupts sent with sound within the daily cap']]},
        {'kind': 'meters', 'layer': 'intent', 'title': 'Machine time', 'items': [
            {'label': 'Sol weekly pool', 'used': sol.get('used_percent'), 'pace': round(fraction * 100, 1) if fraction is not None else None,
             'resets': sol.get('resets_at')}]},
        {'kind': 'chips', 'layer': 'intent', 'title': 'Recurring jobs', 'cap': RETIRED_JOB_CAP,
         'items': [[clip(n), clip(s.get('justification'))] for n, s in jobs.items() if s.get('recurring', True)]},
        {'kind': 'learned', 'layer': 'intent', 'title': 'Your time, and what it learned',
         'demands': demand_rows,
         'edits': len(owner_edits),
         'undos': db.execute('SELECT count(*) FROM changes WHERE undo_of IS NOT NULL').fetchone()[0],
         'lessons': len([l for l in (lessons[0] if lessons and lessons[0] else '').splitlines() if l.strip().startswith(('-', '*'))]),
         'memory': clip(f"day memory {memories[0] if memories else 'none yet'} · synthesis {(synth[0] if synth and synth[0] else 0) / 1024:.1f} KB", 80)},
        chain_block,
        *([{'kind': 'ticks', 'layer': 'process', **tick_evidence}] if 'reason' not in tick_evidence else []),
        schedule(runs, now, settings, moments(db)),
        recent_runs(db, runs, now, data_root(data)),
    ]
    kept = sum(p['state'] == 'kept' for p in promises)
    warnings = ([f"unconfirmed calendar effects: {len(pending_blocks)}"] if pending_blocks else [])
    warnings.extend(f"{p['label']} missed {p['at']}" for p in promises if p['state'] == 'missed')
    warnings.extend(hop['note'] for block in blocks if block['kind'] == 'chain'
                    for hop in block['hops'] if hop['warn'])
    for name in jobs:
        finished = [r for r in runs if r['job'] == name and r['ended_at'] and when(r['ended_at']) <= now]
        newest = max(finished, key=lambda r: when(r['ended_at']), default=None)
        if newest and newest['state'] == 'failed':
            warnings.append(f"{name} latest run failed: {newest['reason'] or 'no reason recorded'}")
    for state, failures in notification_status(db).items():
        if failures:
            warnings.append(f"outbound {state}: " + ', '.join(sorted({r['code'] for r in failures})))
    # The newest finished wake, model or not: only one still running ('gated') is not judged yet.
    newest_wake = next((r for r in db.execute('SELECT * FROM wakes ORDER BY id DESC')
                        if r['status'] != 'gated'), None)
    dropped = json.loads(newest_wake['checkpoint']).get('briefing', {}).get('dropped_keys', []) if newest_wake else []
    if dropped:
        warnings.append('wake briefed without ' + ', '.join(dropped))
    info = json.loads(newest_wake['checkpoint']).get('briefing', {}) if newest_wake else {}
    if newest_wake and (not info.get('complete', False) or newest_wake['status'] not in ('completed', 'skipped')):
        warnings.append('newest wake briefing incomplete: ' + info.get('error', 'wake not complete or no complete page checkpoint'))
    warnings = [name + ': ' + reason for name, reason in invalid_specs.items()] + warnings
    return {'schema': 'intent/1', 'system': 'steward', 'at': int(now.timestamp()),
            'state': 'red' if warnings else 'green',
            'headline': clip(f'kept {kept} promises; {buzzes} interruptions' + ''.join('; ' + note for note in warnings)), 'blocks': blocks}

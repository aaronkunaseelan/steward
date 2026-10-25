"""Calendar B: pure inventory construction, versioned intents, restricted gog effects."""
from datetime import datetime, time, timedelta
import json
import re
import subprocess

from . import jobs, memory, pages, scheduler, senses
from .fulfilment import (MOVEMENT_KINDS, MOVEMENT_MIN_MINUTES,
                         MOVEMENT_QUIET_MINUTES, movement_options)
from .settings import local_zone
from .jobs import UTC, require, instant, change, append
from .timing import planning_end, owner_time


def block_time(start, end):
    start, end = (instant(t).astimezone(local_zone()) for t in (start, end))
    return start.strftime('%a %d %b %H:%M') + '–' + end.strftime(
        '%H:%M' if start.date() == end.date() else '%a %d %b %H:%M')


def change_summary(before, after, action, reason):
    """The calendar's owner-facing change, shared by the log and delivery."""
    when = before if action == 'drop' else after
    happened = 'removed' if action == 'drop' else 'updated' if action == 'keep' else 'added'
    if action == 'place' and before:
        old = instant(before['start']).astimezone(local_zone())
        new = instant(after['start']).astimezone(local_zone())
        happened = 'moved from ' + old.strftime('%H:%M' if old.date() == new.date() else '%a %d %b %H:%M')
    # A model sometimes repeats the move before its explanation. Keep the reason once.
    reason = re.sub(r'^Moved from \d{2}:\d{2} to \d{2}:\d{2}[-–]\d{2}:\d{2}:\s*', '', reason, flags=re.I)
    line = block_time(when['start'], when['end']) + ' ' + when['title'] + ' · ' + happened
    return line + '\n' + reason.strip()


def latest(db, table):
    key = 'name' if table == 'demands' else 'event_key'
    return [dict(r) for r in db.execute(f'SELECT t.* FROM {table} t WHERE version=(SELECT max(version) FROM {table} WHERE {key}=t.{key}) ORDER BY t.rowid')]


def live_demands(db, now):
    """Expiry is derived from the latest version, never persisted."""
    return [r for r in latest(db, 'demands') if r['state'] == 'active'
            and (not json.loads(r['spec_json']).get('deadline')
                 or instant(json.loads(r['spec_json'])['deadline']) > now)]


def fulfilments(db, now, observed=None, week_start=None):
    """Read cached senses only; all readers use the same pure dose accounting."""
    from .fulfilment import fulfilment, validate_week
    from .contracts import ContractError
    snapshot = senses.snapshot(db, now)
    health = snapshot['health']
    unknown = health.get('reason') if health['status'] != 'ok' else None
    data = health.get('data', {})
    if health['status'] != 'ok':
        unknown = unknown or 'Garmin health unknown'
    elif health['verdict'] != 'fresh':
        unknown = 'Garmin health ' + health['verdict']
    elif data.get('date') != memory.local_day(now).isoformat():
        unknown = 'Garmin health is not for today'
    elif data.get('week_start') != memory.week_start(memory.local_day(now)).isoformat():
        unknown = 'Garmin Sunday week has not been read'
    week = data.get('week')
    if not unknown:
        try:
            validate_week(week)
        except ContractError as exc:
            unknown = str(exc)
    if observed is None:
        calendar = snapshot['calendars']
        calendar_data = calendar.get('data', {})
        observed = {e['event_key']: e for e in calendar_data.get('events', [])
                    if e.get('event_key') and e.get('status') != 'cancelled'
                    and e['calendar'] == calendar_data['schedule_calendar_id']
                    } if calendar['status'] == 'ok' and calendar_data.get('schedule_calendar_id') else None
    placements = ([dict(payload(e), demand=k.split(':')[0]) for k, e in observed.items()]
                  if observed is not None else
                  [dict(json.loads(r['payload']), demand=r['demand'], event_key=r['event_key']) for r in latest(db, 'blocks')
                   if r['state'] == 'applied'])
    result = {r['name']: fulfilment(spec, week or [], now,
                [p for p in placements if p['demand'] == r['name']], unknown, week_start)
            for r in live_demands(db, now) if 'count' in (spec := json.loads(r['spec_json']))}
    specs = {r['name']: json.loads(r['spec_json']) for r in live_demands(db, now)}
    ranking = movement_options(specs, result)
    for name, spec in specs.items():
        if spec['kind'] == 'movement' and name in result:
            dose = result[name]
            dose['unknown'] = unknown or ranking['unknown']
            dose['complete'] = ranking['complete'] and not dose['unknown']
            if dose['unknown']:
                dose['remaining'] = dose['remaining_blocks'] = None
            elif dose['complete']:
                dose['remaining'] = dose['remaining_blocks'] = 0
    return result


def movement_ranking(db, now, doses=None):
    specs = {r['name']: json.loads(r['spec_json']) for r in live_demands(db, now)}
    return movement_options(specs, fulfilments(db, now) if doses is None else doses)


def briefing(db, now):
    day_start = datetime.combine(memory.local_day(now), time.min, local_zone())
    doses = fulfilments(db, now)
    return dict(demands=live_demands(db, now), fulfilment=doses,
                movement=movement_ranking(db, now, doses),
                blocks=[r for r in latest(db, 'blocks')
                        if r['state'] not in ('removed', 'deleted')
                        and instant(json.loads(r['payload'])['end']) > day_start])


def validate(spec):
    require(isinstance(spec, dict) and not set(spec) - {'title', 'minutes', 'kind', 'transparency', 'windows', 'count', 'deadline', 'source', 'justification', 'items'}, 'invalid demand fields')
    for key in ('title', 'source', 'justification'):
        require(isinstance(spec.get(key), str) and 0 < len(spec[key].strip()) <= 500, key + ' required')
    require(re.fullmatch(r'charter|gsd:[^\s]+|talk:[0-9]+', spec['source']), 'invalid source')
    require(type(spec.get('minutes')) is int and 15 <= spec['minutes'] <= 240, 'invalid minutes')
    require(spec.get('kind') in ('focus', 'movement', 'gym', 'vo2max', 'anaerobic', 'endurance', 'prep', 'other'), 'invalid kind')
    require(spec.get('transparency') in ('opaque', 'transparent'), 'invalid transparency')
    require(not ('count' in spec and 'deadline' in spec), 'count or deadline, not both')
    if 'count' in spec:
        require(type(spec['count']) is int and 1 <= spec['count'] <= 14, 'invalid weekly count')
    if 'deadline' in spec:
        instant(spec['deadline'])
    if 'items' in spec:
        # A block with items: gsd ids the block is for. gsd owns the items; only their ids are kept here.
        items = spec['items']
        require(isinstance(items, list) and 1 <= len(items) <= 5 and len(set(items)) == len(items)
                and all(isinstance(i, str) and re.fullmatch(r'[A-Za-z0-9-]{1,64}', i) for i in items)
                and spec['kind'] not in MOVEMENT_KINDS + ('movement',), 'invalid items')
    windows = spec.get('windows')
    require(isinstance(windows, dict) and windows and set(windows) <= set('0123456'), 'windows use weekday 0=Monday through 6=Sunday')
    for spans in windows.values():
        require(isinstance(spans, list) and len(spans) <= 8, 'invalid windows')
        for span in spans:
            require(isinstance(span, list) and len(span) == 2 and all(isinstance(x, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in span) and span[0] < span[1], 'invalid local window')
    return spec


def worklist(runner, required=False):
    """gsd owns the owner's items; this is Steward's only read of them, through gsd's contract."""
    try:
        return senses.worklist(runner)
    except (ValueError, OSError, subprocess.SubprocessError):
        require(not required, 'gsd worklist unknown')
        return None


def demand(db, action, name, spec=None, now=None, actor='owner', runner=senses.run):
    now = now or jobs.now_utc()
    require(isinstance(name, str) and re.fullmatch('[a-z0-9-]{1,40}', name), 'invalid demand name')
    old = next((r for r in latest(db, 'demands') if r['name'] == name), None)
    available = old is None or old['state'] == 'removed'
    require(available if action == 'add' else old is not None, 'demand exists or is missing')
    if action != 'rm':
        spec = validate(spec)
        if 'items' in spec:
            open_ids = {i['id'] for i in worklist(runner, required=True)['items']}
            for item in spec['items']:
                require(item in open_ids, 'unknown gsd item: ' + item)
    with db:
        cid = change(db, 'block demand ' + action, now, actor)
        row = dict(name=name, version=old['version'] if old else 0, spec_json=json.dumps(spec) if action != 'rm' else old['spec_json'], state='removed' if action == 'rm' else 'active')
        append(db, 'demands', row, cid)
        db.execute('UPDATE changes SET summary=? WHERE id=?', (f'{action} demand {name}: ' + (spec['justification'] if spec else 'removed'), cid))
    return dict(change_id=cid, demand=name, state=row['state'])


def target(settings):
    cal = settings.get('schedule_calendar_id')
    mains = settings.get('main_calendar_ids')
    require(isinstance(cal, str) and cal and isinstance(mains, list) and mains and all(isinstance(x, str) and x for x in mains) and cal not in mains, 'schedule calendar must differ from every main calendar')
    return cal


def event_span(event):
    def dt(value):
        return instant(value['dateTime']) if 'dateTime' in value else datetime.combine(datetime.fromisoformat(value['date']).date(), time.min, local_zone()).astimezone(UTC)
    return dt(event['start']), dt(event['end'])


def merge(spans):
    result = []
    for a, b in sorted(spans):
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def payload(event):
    a, b = event_span(event)
    return dict(start=a.astimezone(local_zone()).isoformat(), end=b.astimezone(local_zone()).isoformat(), title=event.get('summary', ''), transparency=event.get('transparency') or 'opaque', event_id=event['id'], updated=event.get('updated'), event_key=event.get('event_key') or event.get('extendedProperties', {}).get('private', {}).get('event_key'), description=event.get('description', ''))


def same(a, b):
    return all(a.get(k) == b.get(k) for k in ('title', 'transparency', 'event_key')) and a.get('description', '') == b.get('description', '') and all(instant(a[k]) == instant(b[k]) for k in ('start', 'end'))


def projected(db, observation, settings):
    """Derive owner signals without writes (also used by read-only block plan)."""
    cal = target(settings)
    observed = {}
    for event in observation['events']:
        if event['calendar'] == cal and event.get('event_key') and event.get('status') != 'cancelled':
            require(event['event_key'] not in observed, 'duplicate event_key; repair calendar before planning')
            observed[event['event_key']] = event
    rows = []
    for row in latest(db, 'blocks'):
        row = dict(row)
        p = json.loads(row['payload'])
        e = observed.get(row['event_key'])
        if row['state'] == 'applied':
            # A bounded list is not an identity lookup. Only a resolved missing id is deletion.
            if not e and row['event_key'] in observation.get('missing_event_keys', []):
                row['state'] = 'removed'
            elif e:
                actual = payload(e)
                if not same(p, actual) or (actual['updated'] and p.get('updated') and instant(actual['updated']) > instant(p['updated'])):
                    row.update(payload=json.dumps(actual))
        rows.append(row)
    return rows, observed


def reconcile(db, observation, settings, cid):
    rows, _ = projected(db, observation, settings)
    old = {r['event_key']: r for r in latest(db, 'blocks')}
    learned = []
    for r in rows:
        if r['state'] != old[r['event_key']]['state'] or r['payload'] != old[r['event_key']]['payload']:
            append(db, 'blocks', r, cid)
            # Persist metadata, but tell only a new owner signal, not a JSON
            # shape/ordering change or an updated-only bump.
            p = json.loads(r['payload'])
            prior = old[r['event_key']]
            if r['state'] == prior['state'] and same(p, json.loads(prior['payload'])):
                continue
            # Written for the owner's phone: their block, what Steward saw, what it will do.
            when = owner_time(datetime.fromisoformat(p['start']), '%a %d %b %H:%M') if p.get('start') else ''
            learned.append(f"You deleted \"{p.get('title', r['event_key'])}\"; Steward will use this as a suggestion." if r['state'] == 'removed'
                           else f"You moved \"{p.get('title', r['event_key'])}\" to {when}; Steward will use this as a suggestion.")
    if learned:
        db.execute('UPDATE changes SET summary=summary || ? WHERE id=?', ('; Learned ' + '; '.join(learned), cid))
    return learned


def deadlines(db, now):
    day = memory.local_day(now)
    week_end = datetime.combine(memory.period('week', memory.week_start(day).isoformat())[1], time.min, local_zone())
    return [spec.get('deadline') or week_end.isoformat() for r in live_demands(db, now)
            if (spec := json.loads(r['spec_json'])).get('deadline') or 'count' in spec]


def tracked_events(db):
    return {r['event_key']: p for r in latest(db, 'blocks')
            if r['state'] in ('applied', 'intended', 'written') and (p := json.loads(r['payload'])).get('event_id')}


def read(settings, now, runner, db=None):
    target(settings)
    observation = senses.calendars(settings, now, runner, deadlines(db, now) if db is not None else (),
                                   tracked_events(db) if db is not None else {})
    if db is not None and any('items' in json.loads(r['spec_json']) for r in live_demands(db, now)):
        observation['worklist'] = worklist(runner)  # None holds blocks with items as they are
    return observation


def item_block_description(db, name, spec, start, worklist, now):
    """Render a block's prepared page when live, then its still-open items."""
    start = instant(start) if isinstance(start, str) else start
    slug = f"prep-{name}-{start.astimezone(local_zone()):%Y%m%d}"
    row = db.execute("SELECT expires_at FROM pages WHERE slug=? AND state='live'", (slug,)).fetchone()
    lines = ['Prepared: ' + pages.pages_host() + '/' + slug] if row and instant(row['expires_at']) > now else []
    projects = {p['id']: p['name'] for p in worklist['projects']}
    for item in worklist['items']:
        if item['id'] in spec['items']:
            lines.append(item['title'] + (' (' + projects[item['project_id']] + ')' if item['project_id'] in projects else ''))
    return '\n'.join(lines)


def build(db, settings, now, observation, restoring=None, accounting=None):
    """Pure scheduler input from read-only rows and a complete calendar observation."""
    cal = target(settings)
    day = memory.local_day(now)
    horizon = planning_end(now, local_zone(), deadlines(db, now))
    dates = [day + timedelta(days=i) for i in range((horizon.date() - day).days + bool(horizon.time() != time.min))]
    rows, observed = projected(db, observation, settings)
    busy, focus, owner_busy = [], [], []
    for e in observation['events']:
        if e.get('status') == 'cancelled':
            continue
        span = event_span(e)
        if e['calendar'] in settings['main_calendar_ids']:
            if e.get('eventType') == 'outOfOffice' or e.get('transparency') != 'transparent':
                owner_busy.append(span)
            if e.get('eventType') == 'focusTime':
                focus.append(span)
            elif e.get('eventType') == 'outOfOffice' or e.get('transparency') != 'transparent':
                busy.append(span)
        elif e['calendar'] == cal and not e.get('event_key') and e.get('transparency') != 'transparent':
            busy.append(span)
    busy, focus = merge(busy), merge(focus)
    demands = {r['name']: json.loads(r['spec_json']) for r in live_demands(db, now)}
    tasks, blocks, deleted, task_demands = [], [], [], {}
    active_rows = {r['event_key']: r for r in rows}
    weeks = sorted({memory.week_start(date) for date in dates})
    accounting = accounting if accounting is not None else {}
    accounting.update({str(w): fulfilments(db, now, observed, w) for w in weeks})
    completed = set()
    held = {name for week in accounting.values() for name, dose in week.items() if dose['unknown']}
    open_ids = {i['id'] for i in observation['worklist']['items']} if observation.get('worklist') is not None else None
    for name, spec in demands.items():
        if spec['kind'] in MOVEMENT_KINDS:
            continue  # Watch targets, never calendar tasks.
        if spec.get('items') and open_ids is not None and not set(spec['items']) & open_ids:
            # Every item closed in gsd: future blocks go, elapsed ones stay as history.
            completed.update(key for key in observed if key.split(':')[0] == name)
            continue
        # Slots are stable within a Sunday-week; owner deletions release their slot.
        suffixes = [str(w) for w in weeks] if 'count' in spec else ['once']
        keys = []
        for suffix in suffixes:
            # Steward drops release budget, but never reuse their external key.
            drops = sum(r['state'] == 'deleted' for r in rows
                        if r['event_key'].startswith(f'{name}:{suffix}:')) if spec['kind'] == 'movement' else 0
            keys.extend(f'{name}:{suffix}:{i}' for i in range(spec.get('count', 1) + drops))
        pending_counts = {}
        for key in keys:
            dose = accounting[key.split(':')[1]][name] if 'count' in spec else None
            if dose and dose['unknown']:
                # Held containers can only keep their geometry. Their description
                # must still say that watch progress is unknown, not show old ranks.
                event, old = observed.get(key), active_rows.get(key)
                if spec['kind'] == 'movement' and event and old and old['state'] == 'applied':
                    a, z = event_span(event)
                    if now <= a < z <= horizon.astimezone(UTC):
                        p = payload(event)
                        task_demands[key] = name
                        tasks.append(dict(id=key, minutes=int((z-a).total_seconds()/60),
                                          deadline=p['end'], windows=[]))
                        blocks.append(dict(id=key, task_id=key, kind='scheduled', pinned=True,
                                           start=p['start'], end=p['end']))
                continue
            if dose and dose['complete']:
                completed.update(p['event_key'] for p in dose['planned'])
                continue
            old = active_rows.get(key)
            event = observed.get(key)
            if old and old['state'] == 'applied' and not event:
                continue  # Unjudged outside the read window; not authority to create a replacement.
            if old and old['state'] == 'deleted' and key not in (restoring or {}):
                deleted.append(key)
                continue
            if event:
                a, b = event_span(event)
                if a < now or b > horizon.astimezone(UTC):
                    continue  # Already placed this week, outside the mutable planning horizon.
            if 'count' in spec and not event:
                slot_week = datetime.fromisoformat(key.split(':')[1]).date()
                # Accounting includes foreign-week keys in their actual week, not past movement blocks.
                limit = min(1, dose['remaining_blocks']) if spec['kind'] == 'movement' else dose['remaining_blocks']
                if pending_counts.get(slot_week, 0) >= limit:
                    continue
            due = instant(spec['deadline']) if spec.get('deadline') else horizon.astimezone(UTC)
            windows = []
            for date in dates:
                if 'count' in spec and str(memory.week_start(date)) != key.split(':')[1]:
                    continue
                for lo, hi in spec['windows'].get(str(date.weekday()), []):
                    a = datetime.combine(date, time.fromisoformat(lo), local_zone()).astimezone(UTC)
                    b = datetime.combine(date, time.fromisoformat(hi), local_zone()).astimezone(UTC)
                    for c, d in (focus if spec['kind'] == 'focus' else [(a, b)]):
                        start, end = max(a, c, now), min(b, d, due, horizon.astimezone(UTC))
                        if start < end:
                            windows.append(dict(start=start.astimezone(local_zone()).isoformat(), end=end.astimezone(local_zone()).isoformat()))
            if 'count' in spec and not event:
                pending_counts[slot_week] = pending_counts.get(slot_week, 0) + 1
            task_demands[key] = name
            minutes = spec['minutes']
            tasks.append(dict(id=key, minutes=minutes, deadline=max(due, event_span(event)[1]).astimezone(local_zone()).isoformat() if event else due.astimezone(local_zone()).isoformat(), windows=windows))
            if spec['kind'] == 'movement':
                occupied = busy + [event_span(e) for k, e in observed.items() if k != key]
                tasks[-1]['day_minutes'] = movement_day_minutes(windows, occupied, owner_busy, spec['minutes'])
                if key in (restoring or {}) and not event:
                    prior = restoring[key]
                    a, z = instant(prior['start']), instant(prior['end'])
                    tasks[-1].update(minutes=int((z-a).total_seconds()/60),
                        windows=[dict(start=prior['start'], end=prior['end'])],
                        day_minutes={memory.local_day(a).isoformat(): int((z-a).total_seconds()/60)})
            if event:
                a, b = event_span(event)
                blocks.append(dict(id=key, task_id=key, kind='scheduled', pinned=False, start=a.astimezone(local_zone()).isoformat(), end=b.astimezone(local_zone()).isoformat()))
    represented = {b['id'] for b in blocks}
    # Future keyed events of non-live demands may be deleted.
    # Elapsed events stay as history, not planning obstacles.
    # Events without a key remain owner obstacles (collected above).
    for key in sorted(set(observed) | set(restoring or {})):
        if key in represented or key in task_demands:
            continue
        e = observed.get(key)
        p = payload(e) if e else restoring[key]
        a, b = instant(p['start']), instant(p['end'])
        old = active_rows.get(key)
        name = old['demand'] if old else key.split(':')[0]
        if a < now:
            continue
        if (name not in demands or (demands[name]['kind'] in MOVEMENT_KINDS and name not in held) or key in completed or (key in (restoring or {}) and name not in held)):
            task_demands[key] = name
            tasks.append(dict(id=key, minutes=int((b-a).total_seconds()/60),
                              deadline=p['end'], windows=[dict(start=p['start'], end=p['end'])]
                              if b <= horizon.astimezone(UTC) else []))
            if key in (restoring or {}) and demands.get(name, {}).get('kind') == 'movement':
                tasks[-1]['day_minutes'] = {memory.local_day(a).isoformat(): int((b-a).total_seconds()/60)}
            if key not in (restoring or {}):
                deleted.append(key)
            if e:
                blocks.append(dict(id=key, task_id=key, kind='scheduled', pinned=False,
                                   start=p['start'], end=p['end']))
        elif e:
            busy.append((a, b))
    busy = merge(busy)
    for i, (a, b) in enumerate(busy):
        if a < horizon.astimezone(UTC) and b > now:
            blocks.append(dict(id=f'busy-{i}', task_id=None, kind='busy', pinned=True, start=a.astimezone(local_zone()).isoformat(), end=b.astimezone(local_zone()).isoformat()))
    data = dict(schema_version=1, timezone=local_zone().key, timezone_confirmed=True, now=now.astimezone(local_zone()).isoformat(), deadline=(now + timedelta(minutes=40)).astimezone(local_zone()).isoformat(), external_state='known', horizon_end=horizon.isoformat(), windows=[dict(start=now.astimezone(local_zone()).isoformat(), end=horizon.isoformat())], blocks=blocks, tasks=tasks, deleted_task_ids=deleted)
    return data, task_demands


def movement_day_minutes(windows, busy, owner_busy, default):
    """One feasible length per local day, not duration variants per slot."""
    default = max(MOVEMENT_MIN_MINUTES, min(default, MOVEMENT_QUIET_MINUTES))
    days = {}
    for window in windows:
        a, z = instant(window['start']), instant(window['end'])
        day = memory.local_day(a).isoformat()
        days.setdefault(day, []).append((a, z))
    result = {}
    for day, spans in days.items():
        quiet = not any(a < d and c < z for a, z in spans for c, d in owner_busy)
        longest = 0
        for a, z in spans:
            cursor = a
            for c, d in merge(busy):
                if d <= cursor or c >= z:
                    continue
                longest = max(longest, (min(c, z) - cursor).total_seconds() / 60)
                cursor = max(cursor, d)
            longest = max(longest, (z - cursor).total_seconds() / 60)
        result[day] = (MOVEMENT_QUIET_MINUTES if quiet and longest >= MOVEMENT_QUIET_MINUTES
                       else default if longest >= default
                       else MOVEMENT_MIN_MINUTES if longest >= MOVEMENT_MIN_MINUTES else None)
    return result


def plan(db, settings, now=None, runner=senses.run):
    now = now or jobs.now_utc()
    observation = read(settings, now, runner, db)
    accounting = {}
    data, names = build(db, settings, now, observation, accounting=accounting)
    day = memory.local_day(now)
    return dict(candidates=scheduler.generate(data), demands=names, context=data,
                fulfilment=accounting[str(memory.week_start(day))],
                fulfilment_weeks=accounting,
                movement=movement_ranking(db, now, accounting[str(memory.week_start(day))]),
                description_refreshes=description_refreshes(db, settings, now, observation, accounting))


def description_refreshes(db, settings, now, observation, accounting):
    rows, observed = projected(db, observation, settings)
    specs = {r['name']: json.loads(r['spec_json']) for r in live_demands(db, now)}
    result = {}
    for row in rows:
        key = row['event_key']
        spec = specs.get(row['demand'], {})
        if row['state'] != 'applied' or key not in observed:
            continue
        start = event_span(observed[key])[0]
        if start < now:
            continue
        if spec.get('kind') == 'movement':
            day = memory.local_day(start)
            ranking = movement_options(specs, accounting.get(str(memory.week_start(day)), {}))
            if ranking['complete']:
                continue
            text = ranking['description']
        elif spec.get('items') and observation.get('worklist') is not None:
            text = item_block_description(db, row['demand'], spec, start, observation['worklist'], now)
        else:
            continue
        if observed[key].get('description', '') != text:
            result[key] = text
    return result


def gog(settings, command, args, runner):
    cal = target(settings)
    argv = ['gog', '--enable-commands-exact=calendar.' + command, '--no-input', '-j', 'calendar', command, cal, *args]
    return json.loads(runner(argv))


def apply_one(db, settings, row, cid, runner):
    """Intent is durably committed before every provider mutation; retry by event_key."""
    p = json.loads(row['payload'])
    if p.get('delete'):
        if p.get('event_id'):
            gog(settings, 'delete', [p['event_id'], '--send-updates=none', '--force'], runner)
        # A complete list read verifies absence; failures leave the intent pending.
        observation = read(settings, instant(p['start']), runner, db)
        require(not any(e.get('event_key') == row['event_key'] for e in observation['events']), 'delete read-back mismatch')
        with db:
            append(db, 'blocks', dict(row, version=latest_version(db, row['event_key']), state='deleted'), cid)
        return
    args = ['--summary', p['title'], '--from', p['start'], '--to', p['end'], '--start-timezone', local_zone().key, '--end-timezone', local_zone().key, '--transparency', p['transparency'], '--private-prop', 'event_key=' + row['event_key'], '--private-prop', 'steward=1', '--no-reminders', '--send-updates=none']
    if 'description' in p:
        args += ['--description', p['description']]
    raw = gog(settings, 'update' if p.get('event_id') else 'create', ([p['event_id']] if p.get('event_id') else []) + args, runner)
    event = raw.get('event', raw)
    p.update(event_id=event['id'], updated=event.get('updated'))
    with db:
        row = append(db, 'blocks', dict(row, version=latest_version(db, row['event_key']), payload=json.dumps(p), state='written'), cid)
    confirm(db, settings, row, cid, runner)


def confirm(db, settings, row, cid, runner):
    p = json.loads(row['payload'])
    actual = gog(settings, 'event', [p['event_id']], runner)
    actual = actual.get('event', actual)
    require(actual.get('id') == p['event_id'] and actual.get('updated') == p.get('updated') and same(p, payload(actual)) and actual.get('extendedProperties', {}).get('private', {}).get('steward') == '1' and not actual.get('attendees') and not actual.get('conferenceData') and actual.get('reminders', {}).get('useDefault') is not True and not actual.get('reminders', {}).get('overrides'), 'calendar read-back mismatch; intent remains unapplied')
    with db:
        p['updated'] = actual.get('updated')
        append(db, 'blocks', dict(row, payload=json.dumps(p), state='applied'), cid)


def latest_version(db, key):
    return db.execute('SELECT max(version) FROM blocks WHERE event_key=?', (key,)).fetchone()[0] or 0


def choose(db, settings, proposal, now=None, actor='owner', runner=senses.run, undo_of=None, restoring=None):
    now = now or jobs.now_utc()
    observation = read(settings, now, runner, db)
    accounting = {}
    data, names = build(db, settings, now, observation, restoring, accounting)
    refreshes = description_refreshes(db, settings, now, observation, accounting)
    inventory = scheduler.generate(data)
    checked = scheduler.check(data, inventory, proposal, now.astimezone(local_zone()).isoformat())
    items = {c['id']: c for c in inventory['choices']}
    projected_rows, observed = projected(db, observation, settings)
    rows = {r['event_key']: r for r in latest(db, 'blocks')}
    require(not any(r['state'] in ('intended', 'written') for r in rows.values()), 'pending intent must be resolved with block apply first')
    demands = {r['name']: json.loads(r['spec_json']) for r in latest(db, 'demands')}
    intents = []
    with db:
        cid = change(db, 'block choose', now, actor, undo_of)
        learned = reconcile(db, observation, settings, cid)
        reasons = []
        for selection in proposal['selections']:
            c = items[selection['candidate_id']]
            key = c['task_id']
            if c['action'] == 'keep' and key not in refreshes and key not in (restoring or {}):
                continue
            require(isinstance(selection['reason'], str) and selection['reason'].strip(), 'calendar change needs reason')
            spec = demands.get(names[key]) or (restoring or {}).get(key) or payload(observed[key])
            if restoring and key in restoring:
                spec = dict(spec, title=restoring[key]['title'], transparency=restoring[key]['transparency'])
            p = dict(title=spec['title'], transparency=spec['transparency'], start=c['start'], end=c['end'], event_key=key, reason=selection['reason'])
            if spec.get('kind') == 'movement' and c['action'] != 'drop':
                day = memory.local_day(instant(c['start']))
                p['description'] = movement_options(demands, accounting[str(memory.week_start(day))])['description']
            elif spec.get('items') and c['action'] != 'drop':
                require(observation.get('worklist') is not None, 'gsd worklist unknown')
                p['description'] = item_block_description(db, names[key], spec, c['start'], observation['worklist'], now)
            if restoring and key in restoring:
                p['description'] = restoring[key].get('description', '')
            if key in observed:
                p.update(event_id=observed[key]['id'])
                if key not in rows:
                    # The checked pre-image is also the recovery/undo baseline for untracked events.
                    rows[key] = append(db, 'blocks', dict(event_key=key, version=0,
                        demand=names[key], state='applied', payload=json.dumps(payload(observed[key]))), cid)
            if c['action'] == 'drop':
                p.update(payload(observed[key]), delete=True, reason=selection['reason'])
            row = dict(event_key=key, version=latest_version(db, key), demand=names[key], state='intended', payload=json.dumps(p))
            intents.append(append(db, 'blocks', row, cid))
            before = payload(observed[key]) if key in observed else None
            reasons.append(change_summary(before, p, c['action'], selection['reason']))
        summary = '\n\n'.join(reasons + (['Learned: ' + '; '.join(learned)] if learned else []))
        db.execute('UPDATE changes SET summary=? WHERE id=?', (summary, cid))
    for row in intents:
        apply_one(db, settings, row, cid, runner)
    return dict(change_id=cid, status='applied', checked=checked)


def resume(db, settings, identity, now=None, runner=senses.run):
    """Recover a partial APPLY only after a fresh independent check."""
    now = now or jobs.now_utc()
    observation = read(settings, now, runner, db)
    rows, observed = projected(db, observation, settings)
    pending = [r for r in rows if r['change_id'] == identity and r['state'] in ('intended', 'written')]
    require(pending, 'no pending block intents')
    data, _ = build(db, settings, now, observation)
    inventory = scheduler.generate(data)
    proposal = dict(schema_version=1, selections=[])
    postimages = set()
    for row in pending:
        p = json.loads(row['payload'])
        event = observed.get(row['event_key'])
        if event:
            actual = payload(event)
            prior = db.execute("SELECT payload FROM blocks WHERE event_key=? AND version<? "
                               "AND state='applied' ORDER BY version DESC LIMIT 1",
                               (row['event_key'], row['version'])).fetchone()
            before = json.loads(prior[0]) if prior else None
            postimage = not p.get('delete') and same(p, actual)
            preimage = (before and before.get('event_id') == actual['event_id']
                        and before.get('updated') == actual.get('updated') and same(before, actual))
            require(postimage or preimage or (p.get('delete') and same(p, actual)),
                    'pending event changed; refusing overwrite')
            if postimage:
                postimages.add(row['event_key'])
        if p.get('delete') and not event:
            continue
        candidate = next((c for c in inventory['choices'] if c['task_id'] == row['event_key'] and ((p.get('delete') and c['action'] == 'drop') or (not p.get('delete') and c['action'] in ('keep', 'place') and c['start'] == p['start'] and c['end'] == p['end']))), None)
        require(candidate is not None, 'pending intent no longer feasible')
        proposal['selections'].append(dict(candidate_id=candidate['id'], reason=p['reason']))
    scheduler.check(data, inventory, proposal, now.astimezone(local_zone()).isoformat())
    for row in pending:
        p = json.loads(row['payload'])
        event = observed.get(row['event_key'])
        if event:
            p.update(event_id=event['id'], updated=event.get('updated'))
        if p.get('delete') and not event:
            with db:
                append(db, 'blocks', dict(row, state='deleted'), identity)
            continue
        with db:
            row = append(db, 'blocks', dict(row, payload=json.dumps(p), state='intended'), identity)
        if row['event_key'] in postimages:
            confirm(db, settings, row, identity, runner)
        else:
            apply_one(db, settings, row, identity, runner)
    return dict(change_id=identity, status='applied')


def undo(db, settings, identity, now=None, actor='owner', runner=senses.run):
    now = now or jobs.now_utc()
    affected = [dict(r) for r in db.execute('SELECT * FROM demands WHERE change_id=?', (identity,))]
    if affected:
        with db:
            restored = []
            for r in affected:
                current = next(x for x in latest(db, 'demands') if x['name'] == r['name'])
                require(current['version'] == r['version'], 'a later demand change must be undone first')
                prior = db.execute('SELECT * FROM demands WHERE name=? AND version=?', (r['name'], r['version']-1)).fetchone()
                restored.append(dict(prior) if prior else dict(r, state='removed'))
            cid = change(db, 'undo block demand', now, actor, identity)
            for r in restored:
                r['version'] = next(x['version'] for x in latest(db, 'demands') if x['name'] == r['name'])
                append(db, 'demands', r, cid)
        return dict(change_id=cid, undo_of=identity)
    affected = [dict(r) for r in db.execute('SELECT * FROM blocks WHERE change_id=? ORDER BY version', (identity,))]
    # An untracked event's undo baseline is recorded before its delete intent;
    # it is the prior placement, not itself an effect to reverse.
    affected = [r for r in affected if not (r['state'] == 'applied' and any(
        x['event_key'] == r['event_key'] and x['version'] > r['version']
        and x['state'] == 'intended' for x in affected))]
    require(affected, 'change has no reversible block versions')
    observation = read(settings, now, runner, db)
    restoring = {}
    for row in affected:
        if row['event_key'] not in restoring:
            prior = db.execute("SELECT * FROM blocks WHERE event_key=? AND version<? AND state='applied' ORDER BY version DESC LIMIT 1", (row['event_key'], row['version'])).fetchone()
            if prior:
                restoring[row['event_key']] = json.loads(prior['payload'])
    data, _ = build(db, settings, now, observation, restoring)
    inventory = scheduler.generate(data)
    selections, seen = [], set()
    for row in affected:
        key = row['event_key']
        if key in seen:
            continue
        seen.add(key)
        current = next(r for r in latest(db, 'blocks') if r['event_key'] == key)
        require(current['change_id'] == identity and current['state'] in ('applied', 'deleted'), 'later change, owner signal or incomplete apply prevents undo')
        prior = db.execute("SELECT * FROM blocks WHERE event_key=? AND version<? AND state='applied' ORDER BY version DESC LIMIT 1", (key, row['version'])).fetchone()
        p = json.loads(prior['payload']) if prior else None
        candidate = next((c for c in inventory['choices'] if c['task_id'] == key and ((p is None and c['action'] == 'drop') or (p and c['action'] in ('keep', 'place') and c['start'] == p['start'] and c['end'] == p['end']))), None)
        require(candidate is not None, 'undo placement no longer feasible')
        selections.append(dict(candidate_id=candidate['id'], reason='Restore previous placement; undo ' + identity[:8]))
    return choose(db, settings, dict(schema_version=1, selections=selections), now, actor, runner, undo_of=identity, restoring=restoring)




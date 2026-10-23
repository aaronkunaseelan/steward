"""Read-only, bounded observations. Failures never imply an empty source."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from .contracts import ContractError, read
from . import memory
from .settings import local_zone
from .timing import planning_end, owner_time
from .jobs import COMPUTE_GRID_MINUTES

UTC = timezone.utc
# gsd is read every tick (0.27 s CPU): a 30-min interval hid a finished gsd-steady from the next ready_by judgement (F-64).
INTERVALS = {'calendars': 900, 'gsd': 0, 'qmd': 900, 'health': 1800, 'place': 600, 'machine': 0, 'sol': 0}


class ReadNotFound(ValueError):
    """gog's documented exit 5: the requested provider identity is missing."""


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30 if argv == ['qmd', 'status', '--json'] else 60)
    if result.returncode:
        if argv[0] == 'gog' and result.returncode == 5:
            raise ReadNotFound('provider identity missing')
        raise ValueError('external command failed')
    return result.stdout


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def rows(value, key):
    result = value if isinstance(value, list) else value[key]
    if not isinstance(result, list):
        raise ValueError('invalid list response')
    return result


def calendars(settings, now, runner, deadlines=(), tracked=None):
    ids = [settings['schedule_calendar_id'], *settings['main_calendar_ids']]
    if not all(isinstance(x, str) and x for x in ids):
        raise ValueError('invalid calendar configuration')
    day = memory.local_day(now)
    first, last = memory.period('week', memory.week_start(day).isoformat())
    start = datetime.combine(first, datetime.min.time(), local_zone())
    end = max(planning_end(now, local_zone(), deadlines),
              datetime.combine(last, datetime.min.time(), local_zone()))
    events, missing = [], []
    for calendar in dict.fromkeys(ids):
        raw = json.loads(runner(['gog', '--enable-commands-exact=calendar.events', '--readonly', '-j', 'cal', 'events', calendar, '--from', start.isoformat(), '--to', end.isoformat(), '--all-pages', '--max', '250']))
        listed = list(rows(raw, 'events'))
        if calendar == ids[0]:
            keys = {e.get('extendedProperties', {}).get('private', {}).get('event_key') for e in listed
                    if e.get('status') != 'cancelled'}
            for key, placement in (tracked or {}).items():
                if (key in keys or not start <= datetime.fromisoformat(placement['start']) < end
                        or datetime.fromisoformat(placement['end']) <= now):
                    continue
                identity = placement.get('event_id')
                if not identity:
                    raise ValueError('tracked calendar identity unknown')
                try:
                    raw = json.loads(runner(['gog', '--enable-commands-exact=calendar.event', '--readonly',
                        '--no-input', '-j', 'calendar', 'event', calendar, identity]))
                except ReadNotFound:
                    missing.append(key)
                    continue
                event = raw.get('event', raw)
                if event.get('id') != identity:
                    raise ValueError('calendar identity read mismatch')
                if event.get('status') == 'cancelled':
                    missing.append(key)
                    continue
                if event.get('extendedProperties', {}).get('private', {}).get('event_key') != key:
                    raise ValueError('tracked calendar key changed')
                listed.append(event)
        for event in listed:
            if event.get('status') == 'cancelled':
                continue
            events.append(dict(calendar=calendar, id=event['id'], start=event['start'], end=event['end'],
                               all_day='date' in event['start'], transparency=event.get('transparency'),
                               status=event.get('status'), summary=event.get('summary'),
                               eventType=event.get('eventType', 'default'), updated=event.get('updated'),
                               event_key=event.get('extendedProperties', {}).get('private', {}).get('event_key')))
            if calendar == ids[0] and events[-1]['event_key']:
                events[-1]['description'] = event.get('description', '')
    events.sort(key=lambda e: (e['calendar'], e['id']))
    # The digest marks news: Google's bare `updated` bumps are not, and Steward's own blocks
    # come back as owner learning from calendar_b.reconcile, not as calendar movement.
    news = [{k: v for k, v in e.items() if k != 'updated'} for e in events if not e['event_key']]
    return dict(events=events, missing_event_keys=missing, schedule_calendar_id=ids[0], digest=digest(news), **{'from': start.isoformat(), 'to': end.isoformat()})


def worklist(runner=run):
    """The one validated gsd worklist reader for ticks, blocks and prep pages."""
    return read('gsd.worklist.v1', runner(['gsd', 'worklist', '--json']))


def gsd(now, runner):
    raw = worklist(runner)
    counts = {key: {} for key in ('state', 'next_actor')}
    projects = {p['id']: p['name'] for p in raw['projects']}
    deadlines, older, horizon = [], [], []

    def timestamp(value, field):
        try:
            result = datetime.fromisoformat(value)
            if result.utcoffset() is None:
                raise ValueError
            return result
        except ValueError:
            raise ContractError('gsd.worklist.v1: ' + field + ': expected ISO 8601 with offset') from None

    for item in raw['items']:
        for key in counts:
            label = str(item[key])
            counts[key][label] = counts[key].get(label, 0) + 1
        if item['due_at'] is None:
            continue
        due = timestamp(item['due_at'], 'due_at')
        if due <= now - timedelta(days=7):
            older.append(item['due_at'])
        elif due <= now + timedelta(days=7):
            deadlines.append({k: item[k] for k in ('id', 'title', 'due_at', 'state', 'next_actor')} |
                             {'project': projects.get(item['project_id'])})
        else:
            horizon.append({k: item[k] for k in ('id', 'title', 'due_at', 'state', 'next_actor')} |
                           {'project': projects.get(item['project_id'])})
    deadlines.sort(key=lambda item: timestamp(item['due_at'], 'due_at'))
    horizon.sort(key=lambda item: timestamp(item['due_at'], 'due_at'))
    cut = raw['cut']
    cut_age = (now - timestamp(cut['until'], 'cut.until')).total_seconds() if cut['until'] is not None else None
    past_due_older = dict(count=len(older), oldest_due_at=min(older, key=lambda due: timestamp(due, 'due_at'), default=None))
    closures = None
    if raw.get('done') is not None:
        closures = []
        for event in raw['done']:
            at = timestamp(event['at'], 'done.at')
            if memory.local_day(at) == memory.local_day(now) and at <= now:
                closures.append(event)
    return dict(counts=counts, deadlines=deadlines, horizon_deadlines=horizon,
                done=[event for event in closures if event['state'] == 'done'] if closures is not None else None,
                canceled_today=[event for event in closures if event['state'] == 'canceled'] if closures is not None else None,
                waiting_on_you=raw['priorities']['mine'], waiting_on_others=raw['priorities']['waiting_on_others'],
                notion_urls={item['id']: item['notion_url'] for item in raw['items']},
                past_due_older=past_due_older, cut=cut, cut_age_seconds=cut_age,
                digest=digest({k: v for k, v in raw.items() if k != 'at'}))


def qmd(settings, runner, previous):
    status = read('qmd.status.v1', runner(['qmd', 'status', '--json']))
    # First qmd.feed.v1 deployment; earlier arrivals are unknowable (F23).
    epoch = datetime.fromisoformat('2026-10-17T12:14:40.844838+00:00')
    counts, feed = {}, {}
    for source in settings.get('qmd_watch_sources', ['instapaper', 'x']):
        checkpoint = previous.get('feed', {}).get(source, {})
        token = checkpoint.get('token')
        since = checkpoint.get('since_arrival')
        start = max(datetime.fromisoformat(since), epoch) if since else epoch
        position = ['--after', token] if token is not None else ['--since-arrival', start.isoformat()]
        raw = read('qmd.feed.v1', runner(['qmd', 'feed', '--source', source,
                                        *position, '--json', '--limit', '100']))
        if raw['source'] != source or any(item['source'] != source for item in raw['items']):
            raise ContractError('qmd.feed.v1: source mismatch')
        seen = [tuple(pair) for pair in checkpoint.get('seen', [])]
        known = set(seen)
        count = 0
        for item in raw['items']:
            pair = (item['id'], item['version'])
            if pair not in known:
                count += not item['tombstone']
                known.add(pair)
                seen.append(pair)
        counts[source] = dict(count=count, at_least=not raw['complete'])
        feed[source] = dict(token=raw['next'], seen=seen[-100:], since_arrival=start.isoformat())
    return dict(alive=status['alive'],
                verify={k: status['verify'][k] for k in ('state', 'at', 'red_checks')},
                sources={name: dict({k: source[k] for k in ('fresh_through', 'current', 'cadence_s')},
                                    valid_until=source.get('valid_until'))
                         for name, source in status['sources'].items()},
                new_items=counts, feed=feed)


def watch_current(data, now):
    """The producer owns upload expiry; cached observations age at read time."""
    try:
        expiry = datetime.fromisoformat(data['watch_fresh_until'])
        return expiry.utcoffset() is not None and now < expiry
    except (KeyError, TypeError, ValueError):
        return False


def dated_health(data, now):
    """Date producer values without inventing measurement times or health scores."""
    today = str(memory.local_day(now))
    sleep = data.get('sleep', {})
    upload = data.get('watch_uploaded_at')
    end = sleep.get('end')
    synced = bool(data.get('date') == today and sleep.get('date') == today
                  and sleep.get('duration_s') is not None and upload and end
                  and datetime.fromisoformat(end) <= datetime.fromisoformat(upload) <= now)
    times = {}
    for key in ('body_battery', 'sleep', 'stress', 'hrv', 'resting_hr', 'steps', 'training_readiness', 'last_activity'):
        value = data.get(key)
        at = (value.get('end') if key == 'sleep' else value.get('start') if key == 'last_activity'
              else value.get('at')) if isinstance(value, dict) else None
        date = str(memory.local_day(datetime.fromisoformat(at))) if at else (
            sleep.get('date') if key in ('sleep', 'hrv') else data.get('date'))
        times[key] = dict(at=at, date=date,
                          period='today' if value is not None and synced and date == today else 'unknown')
    return dict(data, morning_synced=synced, value_times=times)


def health(now, runner):
    from .fulfilment import validate_week

    def reading(argv):
        raw = read('garmin.today.v1', runner(argv))
        validate_week(raw['week'])
        for field in ('at', 'pulled_at', 'watch_uploaded_at', 'watch_fresh_until', 'fresh_through'):
            if raw[field] is not None:
                try:
                    if datetime.fromisoformat(raw[field]).utcoffset() is None:
                        raise ValueError
                except ValueError:
                    raise ContractError('garmin.today.v1: ' + field + ': expected ISO 8601 with offset') from None
        return raw

    raw = reading(['garmin', 'today', '--json'])
    today = memory.local_day(now)
    sunday = memory.week_start(today)
    values = {key: raw[key] for key in ('date', 'body_battery', 'sleep', 'stress', 'hrv',
                                      'resting_hr', 'steps', 'training_readiness', 'last_activity', 'auth',
                                      'watch_uploaded_at', 'watch_fresh_until')}
    def session_day(session):
        return memory.local_day(datetime.fromisoformat(session['start']))
    if today == sunday:
        week = [s for s in raw['week'] if session_day(s) == sunday]
    else:
        prior = reading(['garmin', 'today', '--json', '--date', sunday.isoformat()])
        if prior['date'] != sunday.isoformat():
            raise ContractError('garmin.today.v1: Sunday date mismatch')
        # Disjoint dates: never count Sunday twice, even if it appears in both reads.
        week = [s for s in prior['week'] if session_day(s) == sunday]
        week += [s for s in raw['week'] if sunday < session_day(s) <= today]
        values['week_source'] = {key: prior[key] for key in ('auth', 'watch_uploaded_at', 'watch_fresh_until')}
    values['week_start'] = sunday.isoformat()
    values['week'] = sorted(week, key=lambda s: datetime.fromisoformat(s['start']))
    def age(field):
        return max(0, (now - datetime.fromisoformat(raw[field])).total_seconds()) if raw[field] else None
    return dated_health(dict(values, watch_upload_age_s=age('watch_uploaded_at'), pulled_age_s=age('pulled_at'),
                             digest=digest(values)), now)


def place(runner):
    raw = read('phone.place.v1', runner(['phone', 'where', '--json']))

    def timestamp(value):
        try:
            result = datetime.fromisoformat(value)
            if result.utcoffset() is None:
                raise ValueError
            return result
        except ValueError:
            raise ContractError('phone.place.v1: expected ISO 8601 with offset') from None

    timestamp(raw['at'])
    if raw['since'] is not None:
        timestamp(raw['since'])
    if raw['last_event'] is not None:
        timestamp(raw['last_event']['at'])
    today = [{key: event[key] for key in ('type', 'place', 'at')} for event in raw['today']]
    today.sort(key=lambda event: timestamp(event['at']))
    return dict(state=raw['state'], place=raw['place'], since=raw['since'], today=today)


def machine(runner):
    timers = runner(['systemctl', '--user', 'list-timers', '--all', '--no-legend', '--plain'])
    units = runner(['systemctl', 'list-units', 'batch-*', '--no-legend', '--plain'])
    # is-enabled exits 1 for a disabled timer: show yields a successful read of that fact.
    load_state, enabled = (runner(['systemctl', '--user', 'show', 'gsd-step.timer', '--property=LoadState,UnitFileState', '--value']).split('\n') + ['', ''])[:2]
    enabled = 'not-installed' if load_state == 'not-found' else enabled.strip()
    if not enabled:
        raise ValueError('gsd timer enablement unknown')
    memory = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    return dict(loadavg=list(os.getloadavg()), memory_available=memory, root_disk_free=shutil.disk_usage('/').free,
                timers=[s for s in timers.splitlines() if re.search(r'(steward|gsd).*\.timer', s)],
                batch_units=units.splitlines(), gsd_step_timer=enabled,
                violation='gsd-step.timer enabled' if enabled in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime') else None)


def rate_observation(root, now):
    found = []
    for offset in range(8):
        day = now.date() - timedelta(days=offset)
        for path in (root / day.strftime('%Y/%m/%d')).glob('rollout-*.jsonl'):
            with path.open('rb') as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 262144))
                if size > 262144:
                    stream.readline()
                for line in stream:
                    try:
                        event = json.loads(line)
                        limits = event.get('payload', {}).get('rate_limits')
                        if isinstance(limits, dict):
                            observed = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                            if observed <= now:
                                found.append((observed, limits))
                    except (ValueError, KeyError, TypeError):
                        continue
    if not found:
        raise ValueError('no recent rate_limits observation')
    observed, limits = max(found, key=lambda x: x[0])
    primary = limits.get('primary') or {}
    reset = primary.get('resets_at')
    reset = datetime.fromtimestamp(reset, UTC) if reset is not None else None
    window = primary.get('window_minutes')
    fraction = (now - (reset - timedelta(minutes=window))).total_seconds() / (window * 60) if reset and window else None
    return dict(used_percent=primary.get('used_percent'), window_minutes=window,
                resets_at=reset.isoformat() if reset else None,
                resets_at_athens=reset.astimezone(local_zone()).isoformat() if reset else None,
                rate_limit_reached_type=limits.get('rate_limit_reached_type'), plan_type=limits.get('plan_type'),
                observed_at=observed.isoformat(), window_elapsed_fraction=fraction)


def read_age(at, now):
    return max(0, (now - datetime.fromisoformat(at)).total_seconds()) if at else None


def snapshot(db, now, runs=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='senses'").fetchone():
        return {name: dict(status='unknown', at=None, age=None, verdict='unknown', as_of=None,
                           reason='not yet observed') for name in INTERVALS}
    result = {}
    for name in INTERVALS:
        row = db.execute('SELECT * FROM senses WHERE name=? ORDER BY id DESC LIMIT 1', (name,)).fetchone()
        result[name] = (dict(status=row['status'], at=row['at'], age=read_age(row['at'], now),
                             **json.loads(row['payload'])) if row else dict(status='unknown', at=None, age=None, reason='not yet observed'))
    health = result['health']
    if health['status'] == 'ok':
        health['data'] = dated_health(health['data'], now)
    if result['gsd']['status'] == 'ok':
        from .timing import latest_job_obligation, GSD_JOB
        result['gsd']['data'] = dict(result['gsd']['data'], promise=latest_job_obligation(db, GSD_JOB, now, runs=runs))
    for name, sense in result.items():
        sense.get('data', {}).pop('stale', None)  # Retire the persisted pre-verdict flag.
        sense.update(freshness(name, sense, now))
    return result


def freshness(name, sense, now):
    """One verdict per sense; producer completeness clocks are not read clocks."""
    data = sense.get('data', {})
    as_of = sense.get('at')
    verdict, reason = 'fresh', 'read succeeded'
    if sense.get('status') != 'ok':
        verdict, reason = 'unknown', sense.get('reason', 'not yet observed')
    elif name == 'health':
        as_of = data.get('watch_uploaded_at')
        sources = [data] + ([data['week_source']] if 'week_source' in data else [])
        if any(not s.get('watch_uploaded_at') or not s.get('watch_fresh_until') for s in sources):
            verdict, reason = 'unknown', 'watch upload or expiry unknown'
        elif any(s.get('auth') != 'ok' or not watch_current(s, now) for s in sources):
            verdict, reason = 'stale', 'watch expired or auth not ok'
        else:
            reason = 'watch within producer expiry; individual values remain dated'
    elif name == 'qmd':
        # Only scheduled lanes make a currency claim; qmd publishes null cadence_s for unscheduled ones (design K-status).
        sources = {k: s for k, s in data.get('sources', {}).items() if s.get('cadence_s', 0) is not None}
        times = [s.get('fresh_through') for s in sources.values()]
        as_of = datetime.fromtimestamp(min(times), UTC).isoformat() if times and all(t is not None for t in times) else None
        if not sources or any(s.get('fresh_through') is None or s.get('current') is None for s in sources.values()):
            verdict, reason = 'unknown', 'source completeness unknown'
        elif not data.get('alive') or any(not s['current'] for s in sources.values()):
            verdict, reason = 'stale', 'capture stopped or producer reports non-current sources'
        elif any(s.get('valid_until') is None for s in sources.values()):
            verdict, reason = 'unknown', 'source validity deadline unknown'
        elif any(now.timestamp() > s['valid_until'] for s in sources.values()):
            verdict, reason = 'stale', 'producer validity deadline passed'
        else:
            reason = 'all sources current within producer validity'
    elif name == 'gsd':
        as_of = data.get('cut', {}).get('until')
        promise = data.get('promise')
        committed = data.get('cut', {}).get('committed_at')
        target_label = owner_time(datetime.fromisoformat(promise['target']), '%a %d %b %H:%M') if promise else ''
        if not promise:
            verdict, reason = 'unknown', 'gsd-steady ready_by target unknown'
        elif promise['state'] == 'measuring':
            verdict, reason = 'unknown', 'gsd-steady measuring; no ready_by promise yet'
        elif promise['state'] == 'missed':
            verdict, reason = 'stale', 'gsd ready_by missed: ' + target_label
        elif not as_of or not committed or not promise['started_at']:
            verdict, reason = 'unknown', 'landed cut or kept run start unknown'
        elif datetime.fromisoformat(committed) <= datetime.fromisoformat(promise['started_at']):
            verdict, reason = 'stale', 'no newer cut for gsd ready_by: ' + target_label
        elif promise['state'] == 'recovered':
            recovered = datetime.fromisoformat(promise['completed_at']).astimezone(local_zone())
            reason = f'gsd ready_by missed: {target_label}; recovered {owner_time(recovered, '%H:%M')}'
        else:
            reason = 'gsd ready_by kept: ' + target_label
    elif name == 'calendars':
        reason = 'calendar read succeeded'
    elif name == 'sol':
        as_of = data.get('observed_at')
        if not as_of:
            verdict, reason = 'unknown', 'provider observation time unknown'
    if sense.get('status') == 'ok':
        age = sense.get('age')
        if age is None:
            age = read_age(sense.get('at'), now)
        limit = 2 * (INTERVALS[name] or COMPUTE_GRID_MINUTES * 60)
        if age is None:
            verdict, reason = 'unknown', 'reading time unknown; ' + reason
        elif age > limit:
            verdict, reason = 'stale', f'read age {int(age)}s exceeds {limit}s; ' + reason
    return dict(verdict=verdict, reason=reason, as_of=as_of)


def collect(root, settings, now, runner=run, rollout_root=None):
    db = sqlite3.connect(f'{(Path(root) / "state.sqlite").as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        from .calendar_b import deadlines, tracked_events
        calendar_deadlines = deadlines(db, now)
        tracked = tracked_events(db)
        previous = snapshot(db, now)
    finally:
        db.close()
    observations = {}
    for name, interval in INTERVALS.items():
        old = previous[name]
        if old['age'] is not None and old['age'] < interval:
            continue
        try:
            if name == 'calendars':
                data = calendars(settings, now, runner, calendar_deadlines, tracked)
            elif name == 'gsd':
                data = gsd(now, runner)
            elif name == 'qmd':
                data = qmd(settings, runner, old.get('data', {}))
            elif name == 'health':
                data = health(now, runner)
            elif name == 'place':
                data = place(runner)
            elif name == 'machine':
                data = machine(runner)
            else:
                data = rate_observation(Path(rollout_root or Path.home() / '.codex/sessions'), now)
                prior = old.get('data', {})
                elapsed = (datetime.fromisoformat(data['observed_at']) - datetime.fromisoformat(prior['observed_at'])).total_seconds() / 3600 if prior.get('observed_at') else 0
                data['burn_percent_per_hour'] = ((data['used_percent'] - prior['used_percent']) / elapsed if elapsed > 0 and data['used_percent'] is not None and prior.get('used_percent') is not None and data['resets_at'] == prior.get('resets_at') else prior.get('burn_percent_per_hour') if elapsed == 0 else None)
            observations[name] = dict(status='ok', data=data)
        except ContractError as exc:
            observations[name] = dict(status='unknown', reason=str(exc))
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as exc:
            # Exception messages may contain command output, argv or owner data.
            reason = 'missing configuration or response field' if isinstance(exc, KeyError) else 'read failed: ' + type(exc).__name__
            observations[name] = dict(status='unknown', reason=reason)
    return observations


def save(db, observations, now, change_id):
    for name, observation in observations.items():
        old = db.execute('SELECT * FROM senses WHERE name=? ORDER BY id DESC LIMIT 1', (name,)).fetchone()
        old_payload = json.loads(old['payload']) if old else {}
        # An event is a change in the world, not in Steward's ability to see it: going unknown
        # and coming back are not events (a long unknown reaches Alerts instead).
        changed = False
        if name in ('calendars', 'gsd') and observation['status'] == 'ok':
            last_ok = db.execute("SELECT payload,at FROM senses WHERE name=? AND status='ok' ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            changed |= bool(last_ok and json.loads(last_ok[0])['data']['digest'] != observation['data']['digest'])
            # What a manual run lands is the owner's own work: the next wake that happens anyway reads it.
            if name == 'gsd' and changed and manual_run_since(db, last_ok[1]):
                changed = False
        payload = {k: v for k, v in observation.items() if k != 'status'}
        # Checkpoints share the observation's atomic write; unknown never consumes arrivals.
        if name == 'qmd' and observation['status'] == 'unknown' and old_payload.get('data'):
            payload['data'] = {k: old_payload['data'][k] for k in ('feed',)
                               if k in old_payload['data']}
        db.execute('INSERT INTO senses(name,at,status,payload,change_id,event) VALUES (?,?,?,?,?,?)',
                   (name, now.isoformat(), observation['status'], json.dumps(payload), change_id, int(bool(changed))))
        db.execute('DELETE FROM senses WHERE name=? AND id NOT IN (SELECT id FROM senses WHERE name=? ORDER BY id DESC LIMIT 96)', (name, name))


def manual_run_since(db, at):
    """A run someone other than Steward started with `run now`, running at any point after `at`."""
    return db.execute("""SELECT 1 FROM runs r WHERE r.version=(SELECT max(version) FROM runs WHERE run_id=r.run_id)
        AND r.started_at IS NOT NULL AND (r.ended_at IS NULL OR r.ended_at > ?)
        AND r.run_id IN (SELECT x.run_id FROM runs x JOIN changes c ON c.id=x.change_id
                         WHERE c.verb='run now' AND c.actor NOT IN ('model', 'tick')) LIMIT 1""", (at,)).fetchone() is not None


def events(db, cursor):
    return [r[0] for r in db.execute('SELECT DISTINCT s.name FROM senses s JOIN changes c ON c.id=s.change_id WHERE c.rowid>? AND s.event=1', (cursor,))]

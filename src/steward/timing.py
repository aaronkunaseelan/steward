"""Shared planning minimum, ritual and digest times."""
from datetime import datetime, time, timedelta, timezone
import json
from .settings import local_zone
from .memory import local_day


def owner_time(dt, fmt='%a %d %b %Y %H:%M'):
    """Owner-facing instants name the configured zone's actual abbreviation."""
    if dt.utcoffset() is None:
        raise ValueError('owner time requires an aware instant')
    return dt.astimezone(local_zone()).strftime(fmt + ' %Z')


def clock_on(day, clock, fold=0):
    """Configured wall clock to UTC; None for a DST gap, fold selects an overlap."""
    wall = datetime.combine(day, time.fromisoformat(clock), local_zone()).replace(fold=fold)
    instant = wall.astimezone(timezone.utc)
    return instant if instant.astimezone(local_zone()).replace(tzinfo=None) == wall.replace(tzinfo=None) else None

PLAN_AHEAD_MIN_DAYS = 3  # Today plus the next two full local days.


def planning_end(now, zone, deadlines=()):
    minimum = datetime.combine(now.astimezone(zone).date() +
                               timedelta(days=PLAN_AHEAD_MIN_DAYS), time.min, zone)
    return max([minimum, *(datetime.fromisoformat(d).astimezone(zone) for d in deadlines)])


def moments(db):
    from .jobs import current
    return sorted(current(db, 'moments'), key=lambda r: r['start'])


def set_moment(db, name, start, deadline, now=None, actor='owner'):
    from .jobs import get, require, change, append, now_utc
    import re
    for clock in (start, deadline):
        require(isinstance(clock, str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock), 'invalid moment clock')
    require(start < deadline, 'moment deadline must follow start on the same day')
    with db:
        row = get(db, 'moments', name)
        cid = change(db, 'moment set', now or now_utc(), actor)
        append(db, 'moments', dict(row, start=start, deadline=deadline), cid)
    return dict(change_id=cid, name=name)


GSD_JOB = 'gsd-steady'


def deadline_window(target, span=None):
    """Fresh finish window, or feasible start window including scheduler slack."""
    target = target.astimezone(timezone.utc)  # elapsed hours, not wall-clock arithmetic across DST
    return target - timedelta(hours=6), target if span is None else target - span - timedelta(minutes=15)


def kept(finishes, target, start=None):
    lo, hi = deadline_window(target)
    return max((at for at in finishes if (start or lo) <= at <= hi), default=None)


def obligation(target, completions, now, start=None):
    """Judge one deadline from real completions, keeping punctuality distinct from recovery.

    Completions pair a finish instant with its run start (None for receipts).
    """
    finishes = [(at, began) for at, began in completions if at <= now]
    met = kept((at for at, _ in finishes), target, start)
    late = min((at for at, _ in finishes if at > target), default=None) if met is None else None
    finished = met or late
    began = next((began for at, began in finishes if at == finished), None)
    return dict(target=target.isoformat(),
                state='kept' if met else 'recovered' if late else 'due' if now < target else 'missed',
                completed_at=finished.isoformat() if finished else None,
                started_at=began.isoformat() if began else None)


def job_obligation(runs, name, target, now):
    return obligation(target, [(datetime.fromisoformat(r['ended_at']),
                               datetime.fromisoformat(r['started_at']) if r['started_at'] else None)
                              for r in runs if r['job'] == name and r['state'] == 'succeeded' and r['ended_at']], now)


def latest_job_obligation(db, name, now, runs=None):
    """Newest passed target, including yesterday before today's first deadline."""
    from .jobs import current, execution_profile
    job = next((r for r in current(db, 'jobs') if r['name'] == name and r['state'] == 'active'), None)
    clocks = json.loads(job['spec_json'])['needs'].get('ready_by', []) if job else []
    day = local_day(now)
    targets = [target for offset in (-1, 0) for clock in clocks
               if (target := clock_on(day + timedelta(days=offset), clock)) is not None]
    target = max((t for t in targets if t <= now), default=None)
    if target is None:
        return None
    profile = execution_profile(db, name, json.loads(job['spec_json']), runs)
    if profile['measured_at'] is None or target < datetime.fromisoformat(profile['measured_at']):
        return dict(target=target.isoformat(), state='measuring', completed_at=None, started_at=None)
    return job_obligation(current(db, 'runs') if runs is None else runs,
                          name, target.astimezone(local_zone()), now)


def moment_at(moment, now, field):
    return clock_on(local_day(now), moment[field])


def moment_due_at(db, moment, now):
    """Day close also waits for the evening prepare finish, or its ready_by."""
    from .jobs import current
    start = moment_at(moment, now, 'start')
    if moment['name'] != 'day_close':
        return start
    prepare = next((j for j in current(db, 'jobs') if j['name'] == 'prepare' and j['state'] == 'active'), None)
    if prepare is None:
        return start
    day = local_day(now)
    clocks = json.loads(prepare['spec_json'])['needs'].get('ready_by', [])
    target = max((at for clock in clocks if (at := clock_on(day, clock)) is not None), default=None)
    # The same freshness window as prepare's scheduler excludes its morning/yesterday run.
    fresh = deadline_window(target)[0] if target else clock_on(day, '00:00')
    finishes = [datetime.fromisoformat(r['ended_at']) for r in current(db, 'runs')
                if r['job'] == 'prepare' and r['state'] in ('succeeded', 'failed') and r['ended_at']]
    ready = min([at for at in finishes if fresh <= at <= now] + ([target] if target else []), default=None)
    return max(start, ready) if ready else None


def moment_key(db, name, now):
    from .jobs import get
    get(db, 'moments', name)  # Refuse unknown names before any notification effect.
    return name + ':' + str(local_day(now))


def moment_receipts(db, name, now):
    row = db.execute("SELECT at FROM told WHERE key=? AND topic='Today'",
                     (moment_key(db, name, now),)).fetchone()
    return [datetime.fromisoformat(row['at'])] if row else []


def moment_obligation(db, moment, now):
    result = obligation(moment_at(moment, now, 'deadline'),
                        [(at, None) for at in moment_receipts(db, moment['name'], now)], now,
                        start=clock_on(local_day(now), '00:00'))
    if result['state'] == 'due' and completed_moment_wake(db, moment, now):
        result['state'] = 'missed'
    return result


def model_called(row):
    return bool(row['harness_run_id']) or bool(json.loads(row['checkpoint'] or '{}').get('model_calls'))


def completed_moment_wake(db, moment, now):
    # By start, not finish: a wake's briefing (and its rituals) is built when it starts.
    start = moment_due_at(db, moment, now)
    if start is None or now < start:
        return None
    starts = (datetime.fromisoformat(r['started_at'])
              for r in db.execute("SELECT * FROM wakes WHERE status='completed'") if model_called(r))
    return kept(starts, now, start)


def undelivered(db, now):
    """Moments that have started today and have no keyed message yet: what a wake should post."""
    # A late receipt is delivered and recovers its promise, but never counts as kept.
    return [m['name'] for m in moments(db)
            if (start := moment_due_at(db, m, now)) is not None and start <= now
            and not kept(moment_receipts(db, m['name'], now), now,
                         clock_on(local_day(now), '00:00'))]


def due(db, now):
    """Undelivered moments the tick should start a wake for: retry failures, not a completed turn."""
    current = {m['name']: m for m in moments(db)}
    return [name for name in undelivered(db, now) if not completed_moment_wake(db, current[name], now)]

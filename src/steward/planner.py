"""Read-only feasible calendar C choices and the shared reservation checker."""
from datetime import datetime, timedelta
import json
import statistics
from .timing import deadline_window

from .settings import local_zone
from .jobs import UTC, current, duration, instant, require, COMPUTE_GRID_MINUTES


def cost(runs, name):
    done = [r for r in runs if r['job'] == name and r['state'] in ('succeeded', 'failed')]
    minutes = [r.get('cost_minutes') for r in done if r.get('cost_minutes') is not None]
    tokens = [r.get('cost_tokens') for r in done if r.get('cost_tokens') is not None]
    return dict(median_minutes=statistics.median(minutes) if minutes else None,
                median_tokens=statistics.median(tokens) if tokens else None,
                run_count=len(done), last_status=done[-1]['state'] if done else None)


def expected(runs, name, spec):
    learned = cost(runs, name)['median_minutes']
    if learned is not None:
        return learned
    values = [(instant(r['ended_at']) - instant(r['started_at'])).total_seconds() / 60
              for r in runs if r['job'] == name and r['started_at'] and r['ended_at'] and r['state'] in ('succeeded', 'failed')]
    return statistics.median(values) if values else spec['max_minutes']


def gap_floor(runs, name, spec):
    """An automatic start keeps min_gap after the last successful start, manual runs included."""
    starts = [instant(r['started_at']) for r in runs if r['job'] == name and r['state'] == 'succeeded' and r['started_at']]
    return max(starts) + duration(spec['min_gap']) if spec.get('min_gap') and starts else None


def cadence_deadline(name, spec, runs):
    history = [r for r in runs if r['job'] == name]
    needs = spec['needs']
    if 'every' in needs:
        starts = [instant(r['started_at']) for r in history if r['started_at']]
        return max(starts) + duration(needs['every']['max']) if starts else None
    if 'max_staleness' in needs:
        ends = [instant(r['ended_at']) for r in history if r['state'] == 'succeeded' and r['ended_at']]
        return max(ends) + duration(needs['max_staleness']) if ends else None
    return None


def rule(job, runs, at, now, old=None, frozen=True):
    spec = json.loads(job['spec_json'])
    span = timedelta(minutes=expected(runs, job['name'], spec))
    if at < now or at > now + timedelta(hours=36):
        return 'planning window'
    # The frozen horizon keeps what is already planned for the next 2 h from moving.
    # A new reservation moves nothing, so it may land anywhere from now on.
    if frozen and old is not None and instant(old['planned_for']) != at:
        if instant(old['planned_for']) < now + timedelta(hours=2) or at < now + timedelta(hours=2):
            return 'frozen horizon'
    if at != now and at.timestamp() % (COMPUTE_GRID_MINUTES * 60):
        return f'{COMPUTE_GRID_MINUTES}-minute grid'
    floor = gap_floor(runs, job['name'], spec)
    if floor and at < floor:
        return 'min_gap'
    needs = spec['needs']
    history = [r for r in runs if r['job'] == job['name']]
    deadline = cadence_deadline(job['name'], spec, runs)
    if 'every' in needs:
        starts = [instant(r['started_at']) for r in history if r['started_at']]
        if starts and (at < max(starts) + duration(needs['every']['min']) or (now < deadline and at > deadline)):
            return 'every.min/max'
    elif 'max_staleness' in needs:
        # A missed latest safe start is urgency, not a permanent liveness gate.
        if deadline and now < deadline - span and at + span > deadline:
            return 'max_staleness'
    else:
        deadlines = []
        day = now.astimezone(local_zone()).date()
        for offset in range(3):
            for clock in needs['ready_by']:
                from .timing import clock_on
                deadline = clock_on(day + timedelta(days=offset), clock)
                if deadline is not None:
                    deadlines.append(deadline)
        if not any(lo <= at <= hi for lo, hi in (deadline_window(d, span) for d in deadlines)):
            return 'ready_by'
    if spec.get('after'):
        deps = [r for r in runs if r['job'] == spec['after'] and r['state'] in ('planned', 'started')]
        if not deps:
            return 'after dependency'
        dep = min(deps, key=lambda r: r['planned_for'])
        end = instant(dep['started_at'] or dep['planned_for']) + timedelta(minutes=expected(runs, dep['job'], json.loads(dep['spec_json'])))
        if at < end:
            return 'after dependency'
    if spec['heavy']:
        for r in runs:
            if (old and r['run_id'] == old['run_id']) or r['state'] not in ('planned', 'started') or not json.loads(r['spec_json'])['heavy']:
                continue
            start = instant(r['started_at'] or r['planned_for'])
            end = start + timedelta(minutes=expected(runs, r['job'], json.loads(r['spec_json'])))
            if at < end and start < at + span:
                return 'heavy overlap'
    return None


def churn(db, now, cap):
    count = db.execute("""SELECT count(*) FROM runs r JOIN runs p ON p.run_id=r.run_id AND p.version=r.version-1
        JOIN changes c ON c.id=r.change_id WHERE c.at>=? AND c.at<=? AND p.state='planned'
        AND (r.state='cancelled' OR (r.state='planned' AND r.planned_for!=p.planned_for))""",
        ((now - timedelta(hours=24)).isoformat(), now.isoformat())).fetchone()[0]
    require(count < cap, 'churn cap')


def check(db, job, at, now, old=None, cap=6, cancel=False):
    if cancel:
        require(instant(old['planned_for']) >= now + timedelta(hours=2), 'frozen horizon')
    else:
        violation = rule(job, current(db, 'runs'), at, now, old)
        require(violation is None, violation)
    if old and (cancel or instant(old['planned_for']) != at):
        churn(db, now, cap)


def choices(db, now):
    from .runtime import placement, grid
    runs = current(db, 'runs')
    result = []
    for job in current(db, 'jobs'):
        spec = json.loads(job['spec_json'])
        if job['state'] != 'active' or not spec['recurring']:
            continue
        old = next((r for r in runs if r['job'] == job['name'] and r['state'] == 'planned'), None)
        times = sorted(set([now] + [grid(now, True) + timedelta(minutes=COMPUTE_GRID_MINUTES*i) for i in range(109)]))
        feasible = [t for t in times if rule(job, runs, t, now, old) is None]
        if len(feasible) > 24:
            feasible = [feasible[round(i*(len(feasible)-1)/23)] for i in range(24)]
        deadline = cadence_deadline(job['name'], spec, runs)
        missed = deadline if deadline and deadline < now else None
        default, why = placement(job, [r for r in runs if not old or r['run_id'] != old['run_id']], now)
        result.append(dict(job=job['name'], expected_minutes=expected(runs, job['name'], spec),
            missed_at=missed.isoformat() if missed else None,
            feasible=[dict(at=t.isoformat(), reason='recovery after missed ' + missed.isoformat() if missed else 'meets declared needs, dependency and heavy lane') for t in feasible],
            default_choice=default.isoformat() if default else None, default_reason=why,
            current_reservation=old['planned_for'] if old else None,
            meets_needs=not missed and rule(job, runs, instant(old['planned_for']), now, old, frozen=False) is None if old else None))
    return result

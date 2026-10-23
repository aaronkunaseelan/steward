"""Watch facts fulfil doses; calendar placements only reserve future sessions."""
from datetime import datetime, time
from math import ceil
from fractions import Fraction

from .contracts import ContractError, issues, load
from .settings import local_zone
from .jobs import instant
from . import memory

MOVEMENT_KINDS = ('gym', 'vo2max', 'anaerobic', 'endurance')
MOVEMENT_MIN_MINUTES = 90
MOVEMENT_QUIET_MINUTES = 150


def movement_options(specs, doses):
    """One ordering for calendar descriptions, wake, state and intent (watch only)."""
    entries = [(name, spec, doses[name]) for name, spec in specs.items()
               if spec['kind'] in MOVEMENT_KINDS and name in doses]
    unknown = next((doses[name]['unknown'] for name, spec in specs.items()
                    if spec['kind'] in (*MOVEMENT_KINDS, 'movement') and name in doses
                    and doses[name]['unknown']), None)
    if unknown:
        return dict(unknown=unknown, complete=False, options=[],
                    description='Watch unknown: ' + unknown)
    priority = ('vo2max', 'gym', 'anaerobic', 'endurance')
    labels = dict(vo2max='VO2 max', gym='Strength', anaerobic='Anaerobic',
                  endurance='Long-duration')
    options = []
    for name, spec, dose in entries:
        if dose['complete']:
            continue
        endurance = spec['kind'] == 'endurance'
        done = dose['done_min'] if endurance else len(dose['done'])
        target = dose['target_min'] if endurance else spec['count']
        options.append(dict(name=name, kind=spec['kind'], done=done, target=target,
                            minutes=spec['minutes']))
    options.sort(key=lambda o: (-Fraction(str(o['target'] - o['done'])) / o['target'],
                                priority.index(o['kind']), o['name']))
    lines = []
    for i, o in enumerate(options, 1):
        endurance = o['kind'] == 'endurance'
        lines.append(f"{i}. {labels[o['kind']]}, {'≥' if endurance else ''}{o['minutes']} min: "
                     f"{o['done']:g}/{o['target']:g}{' min' if endurance else ''} this week")
    lines.append('2 h: warm-up, shower and walking included.')
    return dict(unknown=None, complete=bool(entries) and not options,
                options=options, description='\n'.join(lines))


def validate_week(week):
    errors = issues(load('garmin.today.v1', vendor=True)['properties']['week'], week)
    if errors:
        raise ContractError('garmin.today.v1: week: ' + '; '.join(errors[:3]))
    for session in week:
        try:
            instant(session['start'])
        except (ValueError, TypeError):
            raise ContractError('garmin.today.v1: week.start: expected ISO 8601 with offset') from None
        if session['zone_s'] is not None and len(session['zone_s']) != 5:
            raise ContractError('garmin.today.v1: week.zone_s: expected five zones')
    return week


def fulfilment(spec, week, now, planned=(), unknown=None, week_start=None):
    """Pure O(week + placements), first-match classification, no title/HR inference.

    done/planned contain the supporting facts, not just totals. remaining is unknown
    (None) on a held reading. Future weeks have no completed watch sessions yet.
    """
    first, last = memory.period('week', (week_start or memory.week_start(memory.local_day(now))).isoformat())
    start = datetime.combine(first, time.min, local_zone())
    end = datetime.combine(last, time.min, local_zone())
    placements = [{k: p[k] for k in ('start', 'end', 'event_key')} for p in planned
                  if start <= instant(p['start']) < end]
    future = [p for p in placements if now <= instant(p['start'])]
    movement = spec['kind'] in MOVEMENT_KINDS
    result = dict(done=[], planned=future, remaining=None, remaining_blocks=None,
                  complete=False, unclassified=[], unknown=unknown if movement else None)
    if spec['kind'] == 'endurance':
        result.update(done_min=None, planned_min=len(future)*spec['minutes'],
                      target_min=spec['count']*spec['minutes'])
    if movement and unknown:
        return result
    done, unclassified = result['done'], result['unclassified']
    if not movement:
        done.extend(p for p in placements if instant(p['start']) < now)
    for session in week if movement else ():
        if not start <= instant(session['start']) < min(now, end):
            continue
        if session['type'] in ('strength_training', 'other'):  # owner 2026-10-21: "\"Other\", = strength"
            kind = 'gym'
        elif session['label'] == 'VO2MAX':
            kind = 'vo2max'
        elif session['label'] in ('ANAEROBIC_CAPACITY', 'SPEED'):
            kind = 'anaerobic'
        elif session['label'] in ('AEROBIC_BASE', 'RECOVERY') and session['duration_s'] is not None and session['duration_s'] >= spec['minutes'] * 60:
            kind = 'endurance'
        else:
            kind = None
        if kind is None:
            unclassified.append(session)
        elif kind == spec['kind']:
            done.append(session)
    if spec['kind'] == 'endurance':
        done_min = sum(s['duration_s'] for s in done) / 60
        result['done_min'] = done_min
        result['remaining'] = max(0, result['target_min'] - done_min - result['planned_min'])
        result['remaining_blocks'] = ceil(result['remaining'] / spec['minutes'])
        result['complete'] = done_min >= result['target_min']
    else:
        result['remaining'] = max(0, spec['count'] - len(done) - len(future))
        result['remaining_blocks'] = result['remaining']
        result['complete'] = len(done) >= spec['count']
    return result

"""Deterministic checks that a Today message does not restate a fact the state contradicts.

Only the five shapes the owner has seen go wrong are checked; each is a short regex over
the visible text, so a wording the model invents is never guessed at.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta
import json
import re

from . import messages
from .memory import local_day
from .settings import local_zone

WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

NO_FINDS = re.compile(r'\bno (?:new )?finds\b', re.I)
FINDS_COUNT = re.compile(r'\b(\d+) finds? tonight\b', re.I)
AHEAD = re.compile(r"Can['’]t see: calendar after (" + '|'.join(WEEKDAYS) + r')\b')
NO_MEETINGS = re.compile(r'no[- ]meetings', re.I)
DUE = re.compile(r'\bdue (' + '|'.join(WEEKDAYS) + r')(?: (\d{1,2}))?\b')
PAST_TENSE = re.compile(r'\b(?:was|were|overdue|past)\b', re.I)
SEE = re.compile(r"^Can['’]t see")
LIMIT = re.compile(r"^(?:Can['’]t see|Not placed)")
LINKED_END = re.compile(r'\]\([^)\s]+\)\s*$')


def refusals(db, text, now):
    """Every contradiction the message contains, in the order the owner reads them."""
    from .notify import render
    _, visible = render(db, text, now)
    groups = [[line.strip() for line in block.split('\n') if line.strip()]
              for block in re.split(r'\n[ \t]*\n', visible)]
    groups = [group for group in groups if group]
    found = (finds(db, visible, now) + coverage(db, visible, now) + meetings(db, groups, now)
             + due_tense(db, groups, now) + limits(text, groups))
    return list(dict.fromkeys(found))


def sense(db, name):
    """The latest reading only: an old row is not what the message would have said."""
    row = db.execute('SELECT status, payload FROM senses WHERE name=? ORDER BY id DESC LIMIT 1',
                     (name,)).fetchone()
    if row is None:
        return None
    return dict(status=row[0], data=json.loads(row[1]).get('data') or {})


def at(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=local_zone())


def day_of(value):
    parsed = at(value)
    return local_day(parsed) if parsed is not None else None


def weekday_date(now, name):
    today = local_day(now)
    return today + timedelta(days=(WEEKDAYS.index(name) - today.weekday()) % 7)


def month_date(today, number, name):
    """The day number in this, last or next month whose weekday is the one named; None if none is."""
    for months in (0, -1, 1):
        year, month = divmod(today.year * 12 + today.month - 1 + months, 12)
        if number <= monthrange(year, month + 1)[1]:
            found = date(year, month + 1, number)
            if found.weekday() == WEEKDAYS.index(name):
                return found
    return None


def finds(db, visible, now):
    sent = messages.finds_since(db, messages.evening_cutoff(now))
    waiting = messages.queued(db)
    say = f'finds: {len(sent)} sent since 18:00 yesterday, {waiting} waiting; say that'
    if NO_FINDS.search(visible) and (sent or waiting):
        return [say]
    if any(int(count) not in (waiting, waiting + len(sent)) for count in FINDS_COUNT.findall(visible)):
        return [say]
    return []


def coverage(db, visible, now):
    reading = sense(db, 'calendars')
    if reading is None or reading['status'] != 'ok':
        return []
    ahead = reading['data'].get('ahead') or {}
    until = day_of(ahead.get('to')) if ahead.get('status') == 'ok' else None
    if until is None:
        return []
    return [f"coverage: Ahead already covers the calendar through {ahead['to']}; "
            'say what it covers instead'
            for name in AHEAD.findall(visible) if until > weekday_date(now, name) + timedelta(days=1)]


def meetings(db, groups, now):
    reading = sense(db, 'calendars')
    if reading is None or reading['status'] != 'ok':
        return []
    blocks = {row[0] for row in db.execute('SELECT DISTINCT event_key FROM blocks')}
    events = reading['data'].get('events') or []
    found = []
    for group in groups:
        label = group[0]
        if not NO_MEETINGS.search(label):
            continue
        named = next((name for name in WEEKDAYS if re.search(r'\b' + name + r'\b', label)), None)
        day = weekday_date(now, named) if named else (
            local_day(now) if re.search(r'\btoday\b', label, re.I) else None)
        if day is None:
            continue
        busy = [e for e in events
                if isinstance(e.get('start'), dict) and 'dateTime' in e['start']
                and day_of(e['start']['dateTime']) == day
                and e.get('transparency') != 'transparent'
                and (e.get('event_key') is None or e['event_key'] not in blocks)
                and not NO_MEETINGS.search(e.get('summary') or '')]
        if busy:
            found.append(f'no meetings: {len(busy)} timed event(s) on {day:%a %d %b}; name them')
    return found


def due_tense(db, groups, now):
    reading = sense(db, 'gsd')
    dues = ([item.get('due_at') for key in ('deadlines', 'horizon_deadlines')
             for item in (reading['data'].get(key) or [])]
            if reading is not None and reading['status'] == 'ok' else [])
    found = []
    for line in [line for group in groups for line in group]:
        match = DUE.search(line)
        if match is None or PAST_TENSE.search(line):
            continue
        name, number = match[1], match[2]
        if number:
            resolved = month_date(local_day(now), int(number), name)
            if resolved is not None and resolved < local_day(now):
                found.append(f"due {name}: the deadline was {resolved:%a %d %b}; "
                             "say 'was due' or 'overdue'")
            continue
        past = [due for due in (at(value) for value in dues)
                if due is not None and local_day(due).weekday() == WEEKDAYS.index(name) and due < now]
        upcoming = weekday_date(now, name)
        if past and not [due for due in (day_of(value) for value in dues) if due == upcoming]:
            found.append(f"due {name}: the deadline was {max(past):%a %d %b}; "
                         "say 'was due' or 'overdue'")
    return found


def limits(text, groups):
    if not groups:
        return []
    last = len(groups) - 1
    raw = [line for line in text.strip().split('\n') if line.strip()]
    if len(groups[last]) == 1 and raw and LINKED_END.search(raw[-1]):
        # The closing finds / Your list line is a link, not a limit.
        last -= 1
    found = []
    for index, group in enumerate(groups):
        for line in group:
            if LIMIT.match(line) and index != last:
                found.append(f'limits: "{line}" belongs only in the last group '
                             f'({index + 1} of {len(groups)})')
    unseen = [line for group in groups for line in group if SEE.match(line)]
    if len(unseen) > 1:
        found.append("limits: at most one \"Can't see\" line; merge them into one")
    return found

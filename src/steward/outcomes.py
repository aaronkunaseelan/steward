"""Weekly observations, not inferred owner intent. Nothing is persisted."""
from .settings import local_zone
from datetime import datetime, time
import json
import subprocess

from . import jobs, memory, senses
from .contracts import ContractError, read


def row(metric, count=None, total=None, reason=None, definition=None, **extra):
    return dict(metric=metric, status='unknown' if reason else 'ok', count=count,
                total=total, reason=reason, definition=definition, **extra)


def r1(since, lo, hi, now, runner):
    metric = 'r1_amendment_rate'
    definition = f"R1 amended A changes / A changes, runs ending in this {local_zone().key.rsplit('/', 1)[-1]} week"
    try:
        contract = json.loads(runner(['gsd', 'contract', 'gsd.runs.v1']))
        if contract.get('$id') != 'gsd.runs.v1':
            return row(metric, reason='no contract', definition=definition)
    except (OSError, subprocess.SubprocessError, ValueError):
        return row(metric, reason='no contract', definition=definition)
    try:
        payload = read('gsd.runs.v1', runner(['gsd', 'runs', '--since', since, '--json']))
        if any(r['until'] is None for r in payload['runs']):
            return row(metric, reason='gsd run end unknown', definition=definition)
        runs = [r for r in payload['runs'] if lo <= jobs.instant(r['until']) < hi and jobs.instant(r['until']) <= now]
        if not runs:
            return row(metric, reason='no measured gsd runs in week', definition=definition)
        if any(any(r['counts'].get(k) is None for k in ('a_changes', 'r1_amended')) for r in runs):
            return row(metric, reason='gsd A/R1 counts missing', definition=definition)
        total = sum(r['counts']['a_changes'] for r in runs)
        count = sum(r['counts']['r1_amended'] for r in runs)
        if not total:
            return row(metric, count=count, total=total, reason='no A changes; rate undefined', definition=definition)
        return row(metric, count, total, definition=definition, rate=count/total)
    except (OSError, subprocess.SubprocessError, ValueError, ContractError):
        return row(metric, reason='gsd runs unavailable or invalid contract payload', definition=definition)


def closures(since, runner):
    """gsd's closure events at the closure's own time (gsd.closures.v1); None when the view is missing."""
    try:
        return read('gsd.closures.v1', runner(['gsd', 'closures', '--since', since, '--json']))['closures']
    except (OSError, subprocess.SubprocessError, ValueError, ContractError):
        return None


def item_blocks(db, lo, hi, now, events):
    """Rows for the owner's blocks with items: a block of a demand with items that started in the week and has ended."""
    specs = {}
    for r in db.execute('SELECT name, spec_json FROM demands ORDER BY version'):
        specs[r['name']] = json.loads(r['spec_json'])  # latest version wins
    blocks = {}
    for r in db.execute('SELECT * FROM blocks ORDER BY version'):
        blocks[r['event_key']] = r
    used, ignored, opened, elapsed, applied = 0, 0, 0, 0, 0
    for r in blocks.values():
        items = specs.get(r['demand'], {}).get('items')
        if not items or r['state'] not in ('applied', 'removed'):
            continue
        p = json.loads(r['payload'])
        start, end = jobs.instant(p['start']), jobs.instant(p['end'])
        if not (lo <= start < hi and end <= now):
            continue
        elapsed += 1
        day = memory.local_day(start)
        page = db.execute('SELECT opened_at FROM pages WHERE slug=?', (f"prep-{r['demand']}-{day:%Y%m%d}",)).fetchone()
        opened += bool(page and page['opened_at'] and jobs.instant(page['opened_at']) < start)
        if events is None:
            continue
        closed = any(e['id'] in items and e['state'] == 'done' and memory.local_day(jobs.instant(e['at'])) == day
                     for e in events)
        used += closed
        if r['state'] == 'applied':
            applied += 1
            ignored += not closed
    reason = None if events is not None else 'gsd closures view unavailable or invalid'
    return [
        row('blocks_used', None if reason else used, elapsed, reason=reason,
            definition=f"blocks with items ended in week with an item closed done on the block's {local_zone().key.rsplit('/', 1)[-1]} day, by gsd's closure time / blocks with items ended in week"),
        row('blocks_ignored', None if reason else ignored, None if reason else applied, reason=reason,
            definition='blocks with items ended in week, kept by the owner, with nothing closed that day / blocks with items ended in week and kept'),
        row('pages_opened_before_block', opened, elapsed,
            definition='blocks with items whose prepared page was opened before the block started / blocks with items ended in week'),
    ]


def weekly(db, week=None, now=None, runner=senses.run):
    now = now or jobs.now_utc()
    week = week or memory.week_start(memory.local_day(now)).isoformat()
    start, end = memory.period('week', week)
    lo = datetime.combine(start, time(), local_zone())
    hi = datetime.combine(end, time(), local_zone())
    jobs.require(lo <= now, 'week has not started')
    first = db.execute("SELECT at FROM changes WHERE actor!='migration' ORDER BY rowid LIMIT 1").fetchone()
    coverage = None if first and jobs.instant(first[0]) <= lo else 'week starts before recorded history'
    def inside(value):
        return value is not None and lo <= jobs.instant(value) < hi and jobs.instant(value) <= now
    # Only reconciliation by tick records owner edits/deletions; model APPLY is not an owner action.
    moved, deleted = set(), set()
    for r in db.execute('SELECT b.*, c.at, c.actor FROM blocks b JOIN changes c ON c.id=b.change_id'):
        if r['actor'] != 'tick' or not inside(r['at']):
            continue
        prior = db.execute('SELECT * FROM blocks WHERE event_key=? AND version<? ORDER BY version DESC LIMIT 1', (r['event_key'], r['version'])).fetchone()
        if prior is None or prior['state'] != 'applied':
            continue
        if r['state'] == 'removed':
            deleted.add(r['event_key'])
        elif r['state'] == 'applied':
            before, after = json.loads(prior['payload']), json.loads(r['payload'])
            if any(jobs.instant(before[k]) != jobs.instant(after[k]) for k in ('start', 'end')):
                moved.add(r['event_key'])
    rows = [row('calendar_kept', reason='no affirmative owner-kept signal; unchanged is not acceptance',
                definition='owner confirmed a block was kept')]
    for metric, values in [('calendar_moved', moved), ('calendar_deleted', deleted)]:
        rows.append(row(metric, None if coverage else len(values), reason=coverage,
                        observed_count=len(values), definition='distinct block keys with owner transition recorded by tick in week'))
    replies = {r['reply_to'] for r in db.execute('SELECT reply_to,at FROM inbox WHERE reply_to IS NOT NULL') if inside(r['at'])}
    sent = [r for r in db.execute("SELECT * FROM outbox WHERE state='sent'") if inside(r['sent_at'])]
    rows.append(row('messages_answered', reason='messages have no structured asked flag; cannot identify the denominator',
                    observed_replied_messages=len({r['message_id'] for r in sent} & replies),
                    definition='owner replies / messages explicitly asking for an answer'))
    finds = {r['message_id'] for r in sent if r['topic'] == 'Finds'}
    rows.append(row('finds_acted_on', None if coverage else len(finds & replies), None if coverage else len(finds),
                    reason=coverage, observed_count=len(finds & replies), observed_total=len(finds),
                    definition='Finds messages with a recorded owner reply by week end / sent Finds messages (digest counts once; not individual links; not proof of reading)'))
    rows.append(r1(start.isoformat(), lo, hi, now, runner))
    events = None if coverage else closures(lo.isoformat(), runner)
    for r in item_blocks(db, lo, hi, now, events):
        rows.append(dict(r, status='unknown', reason=coverage, count=None, total=None) if coverage else r)
    rows.append(row('items_closed', None if events is None else sum(e['state'] == 'done' and inside(e['at']) for e in events),
                    reason=coverage or (None if events is not None else 'gsd closures view unavailable or invalid'),
                    definition="items closed done in week, by gsd's closure time (gsd.closures.v1)"))
    moved_interests = 0
    for r in db.execute('SELECT i.name, i.version, i.spec_json, c.at FROM interests i JOIN changes c ON c.id=i.change_id ORDER BY i.name, i.version'):
        prior = db.execute('SELECT spec_json FROM interests WHERE name=? AND version=?', (r['name'], r['version'] - 1)).fetchone()
        if prior and inside(r['at']) and json.loads(prior['spec_json']).get('stage') != json.loads(r['spec_json']).get('stage'):
            moved_interests += 1
    rows.append(row('interests_moved', None if coverage else moved_interests, reason=coverage,
                    definition='interest versions in week whose stage differs from the previous version'))
    return dict(week=week, since=lo.isoformat(), until=min(hi, now).isoformat(), as_of=now.isoformat(), rows=rows)

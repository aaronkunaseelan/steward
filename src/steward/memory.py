"""Filed experience and bounded, fading recall. All instants are offset-aware."""
from .settings import local_zone
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import re
import secrets

from . import jobs, mind
from .state import atomic_file, StateError

CAPS = dict(day=2048, week=3072, month=4096, lessons=6144, synthesis=2048)
FIRST = date(2026, 10, 16)
SOURCE = Path(__file__).resolve().parents[2]


def home(root=None):
    if root is not None:
        return Path(root)
    return Path.home() / '.local/share/steward/memory'


def storage(db, root=None):
    if root is not None:
        return home(root)
    database = Path(db.execute('PRAGMA database_list').fetchone()[2])
    production = Path.home() / '.local/state/steward/state.sqlite'
    return home() if database == production else database.parent / 'memory'


def period(layer, key):
    if layer == 'day':
        start = date.fromisoformat(key)
        end = start + timedelta(days=1)
        jobs.require(start.isoformat() == key, 'invalid day key')
    elif layer == 'week':
        jobs.require(bool(re.fullmatch(r'\d{4}-W\d{2}', key)), 'invalid week key')
        start = date.fromisocalendar(int(key[:4]), int(key[6:]), 1)
        end = start + timedelta(days=7)
    else:
        jobs.require(bool(re.fullmatch(r'\d{4}-\d{2}', key)), 'invalid month key')
        start = date.fromisoformat(key + '-01')
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def days(start, end):
    while start < end:
        yield start.isoformat()
        start += timedelta(days=1)


def latest(db):
    return [dict(r) for r in db.execute('SELECT m.* FROM memories m WHERE version=(SELECT max(version) FROM memories WHERE layer=m.layer AND key=m.key) ORDER BY key,layer')]


def filed(db, root=None):
    return {r[0] for r in db.execute('SELECT day FROM journal_files') if (storage(db, root) / 'journal' / (r[0] + '.json')).is_file()}


def journal(db, since=None, until=None, day=None, root=None, full=False):
    if day:
        start, end = period('day', day)
        path = storage(db, root) / 'journal' / (day + '.json')
        if path.exists() and not full:
            return json.loads(path.read_text())
        since = datetime.combine(start, time(), local_zone()).isoformat()
        until = datetime.combine(end, time(), local_zone()).isoformat()
    jobs.require(since is not None and until is not None, 'journal requires a day or both bounds')
    lo, hi = jobs.instant(since), jobs.instant(until)
    jobs.require(lo < hi, 'invalid journal window')
    result = dict(since=since, until=until, truncated=False, changes=[], runs=[], messages=[], interrupts=[], inbox=[], calendar_b=[], wakes=[])
    def add(section, row):
        result[section].append(row)
        if len(json.dumps(result).encode()) > 256 * 1024:
            result[section].pop()
            result['truncated'] = True
            return False
        return True
    def inside(value):
        return value is not None and lo <= jobs.instant(value) < hi
    changes = {r['id']: dict(r) for r in db.execute('SELECT * FROM changes')}
    for row in changes.values():
        if inside(row['at']):
            subjects = []
            reasons = []
            for table, key in [('jobs','name'), ('runs','run_id'), ('interests','name'), ('demands','name'), ('blocks','event_key'), ('memories','key')]:
                for r in db.execute(f'SELECT * FROM {table} WHERE change_id=?', (row['id'],)):
                    subjects.append(r[key])
                    if table == 'runs':
                        reasons.append(r['reason'])
            add('changes', dict(row, subject=subjects, reason=reasons or [row['summary']]))
    for r in jobs.current(db, 'runs'):
        if any(inside(r[k]) for k in ('planned_for','started_at','ended_at')) or any(inside(changes[v[0]]['at']) for v in db.execute('SELECT change_id FROM runs WHERE run_id=?', (r['run_id'],))):
            count = None
            if r['manifest']:
                try:
                    manifest_path = Path(r['manifest'])
                    manifest = json.loads(manifest_path.read_text())
                    records = manifest_path.with_name('records.jsonl')
                    count = sum(1 for line in records.read_text().splitlines() if line.strip()) if records.exists() else 0
                except (OSError, ValueError, TypeError):
                    pass
            add('runs', {**{k:r[k] for k in ('run_id','job','state','planned_for','started_at','ended_at')}, 'minutes':r['cost_minutes'], 'tokens':r['cost_tokens'], 'records_count':count})
    for r in db.execute("SELECT * FROM outbox WHERE state='sent'"):
        if inside(r['sent_at']):
            keys = [k[0] for k in db.execute('SELECT key FROM outbox_keys WHERE outbox_id=? ORDER BY key', (r['id'],))]
            add('messages', {**{k:r[k] for k in ('topic','kind','message_id','sent_at')}, 'told_key':keys[0] if keys else None, 'told_keys':keys, 'text':r['text'] if full else r['text'][:300]})
    from .notify import interrupt_records
    for r in interrupt_records(db):
        if inside(r['at']):
            add('interrupts', r)
    for r in db.execute('SELECT * FROM inbox'):
        if inside(r['at']):
            add('inbox', dict(r))
    for r in db.execute('SELECT * FROM blocks'):
        if inside(changes[r['change_id']]['at']):
            add('calendar_b', dict(r))
    for r in db.execute('SELECT * FROM wakes'):
        if inside(r['started_at']):
            add('wakes', dict(r))
    return result


def wake_journal(db, wake_id):
    wake = db.execute('SELECT * FROM wakes WHERE id=?', (wake_id,)).fetchone()
    jobs.require(wake is not None, 'unknown wake')
    cursor = wake['change_cursor']
    previous = db.execute('SELECT change_cursor FROM wakes WHERE id<? ORDER BY id DESC LIMIT 1', (wake_id,)).fetchone()
    lo = previous[0] if previous else 0
    result = dict(wake=wake_id, cursor=cursor, previous_cursor=lo)
    for verb in ('release', 'mind'):
        row = db.execute('SELECT * FROM changes WHERE verb=? AND rowid<=? ORDER BY rowid DESC LIMIT 1', (verb, cursor)).fetchone()
        result[verb] = dict(row) if row else None
    synthesis = db.execute("SELECT m.* FROM memories m JOIN changes c ON c.id=m.change_id WHERE m.layer='synthesis' AND m.key='current' AND c.rowid<=? ORDER BY c.rowid DESC LIMIT 1", (cursor,)).fetchone()
    result['synthesis'] = dict(synthesis) if synthesis else None
    result['unknown'] = {key: 'not recorded at or before cursor' for key in ('release', 'mind', 'synthesis') if result[key] is None}
    result['changes'] = [dict(r) for r in db.execute('SELECT * FROM changes WHERE rowid>? AND rowid<=? ORDER BY rowid', (lo, cursor))]
    return result


def file_days(db, now=None, root=None):
    now = now or jobs.now_utc()
    directory = storage(db, root) / 'journal'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    done = []
    for key in days(FIRST, now.astimezone(local_zone()).date()):
        path = directory / (key + '.json')
        if not path.exists():
            atomic_file(path, json.dumps(journal(db, day=key, root=root)).encode())
        with db:
            db.execute('INSERT OR IGNORE INTO journal_files VALUES (?,?)', (key, now.isoformat()))
        done.append(key)
    return done


def due(db, now=None, root=None):
    today = (now or jobs.now_utc()).astimezone(local_zone()).date()
    records = filed(db, root)
    have = {(r['layer'],r['key']) for r in latest(db) if r['content'] is not None}
    if not records:
        return []
    first = min(records)
    candidates = {('day', k) for k in records}
    for k in records:
        d = date.fromisoformat(k)
        candidates.add(('week', f'{d.isocalendar().year}-W{d.isocalendar().week:02}'))
        candidates.add(('month', k[:7]))
    result = []
    for layer, key in candidates:
        start, end = period(layer, key)
        if start.isoformat() >= first and end <= today and set(days(start,end)) <= records and (layer,key) not in have:
            item = dict(layer=layer,key=key)
            if layer == 'week':
                item['day_memories'] = list(days(start,end))
            result.append(item)
    return sorted(result, key=lambda r: (period(r['layer'],r['key'])[1], list(CAPS).index(r['layer'])))


def mirror(db, root=None):
    for r in latest(db):
        path = storage(db, root) / r['layer'] / (r['key'] + '.md')
        if r['content'] is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not path.exists() or path.read_text() != r['content']:
                atomic_file(path, r['content'].encode())


def write(db, layer, key, content, actor='owner', now=None, root=None):
    now = now or jobs.now_utc()
    jobs.require(layer in CAPS and isinstance(content,str) and content.strip() and len(content.encode()) <= CAPS[layer], 'invalid memory or byte cap exceeded')
    if layer in ('lessons', 'synthesis'):
        jobs.require(key == {'lessons': 'lessons', 'synthesis': 'current'}[layer], 'invalid ' + layer + ' key')
    else:
        start,end = period(layer,key)
        jobs.require(end <= now.astimezone(local_zone()).date(), 'period incomplete')
        jobs.require(set(days(start,end)) <= filed(db,root), 'period not filed')
    with db:
        cid = jobs.change(db, 'memory write', now, actor)
        label = layer
        if layer in ('day', 'week', 'month'):
            label += ' memory ' + (start.strftime('%a %d %b %Y') if layer == 'day' else
                                   start.strftime('%b %Y') if layer == 'month' else key)
        db.execute('UPDATE changes SET summary=? WHERE id=?', ('Memory write: ' + label, cid))
        version = db.execute('SELECT coalesce(max(version),0)+1 FROM memories WHERE layer=? AND key=?', (layer,key)).fetchone()[0]
        db.execute('INSERT INTO memories VALUES (?,?,?,?,?)', (layer,key,version,cid,content))
    mirror(db,root)
    return dict(change_id=cid,layer=layer,key=key,version=version)


def undo(db, identity, actor='owner', root=None):
    r = db.execute('SELECT * FROM memories WHERE change_id=?', (identity,)).fetchone()
    jobs.require(r is not None, 'unknown memory change')
    version = db.execute('SELECT max(version) FROM memories WHERE layer=? AND key=?', (r['layer'],r['key'])).fetchone()[0]
    jobs.require(version == r['version'], 'a later change must be undone first')
    prior = db.execute('SELECT content FROM memories WHERE layer=? AND key=? AND version=?', (r['layer'],r['key'],version-1)).fetchone()
    with db:
        cid = jobs.change(db,'undo',jobs.now_utc(),actor,identity)
        db.execute('INSERT INTO memories VALUES (?,?,?,?,?)',(r['layer'],r['key'],version+1,cid,prior[0] if prior else None))
    mirror(db,root)
    return dict(change_id=cid,undo_of=identity)


def show(db, layer=None, key=None, week=None, month=None):
    bounds = period('week' if week else 'month', week or month) if week or month else None
    result = []
    for r in latest(db):
        if r['content'] is None or (layer and r['layer'] != layer) or (key and r['key'] != key):
            continue
        if bounds:
            if r['layer'] in ('lessons', 'synthesis'):
                continue
            start,end = period(r['layer'],r['key'])
            if not (start < bounds[1] and end > bounds[0]):
                continue
        result.append(r)
    return result


def recall(db, now):
    today = now.astimezone(local_zone()).date()
    earliest = today - timedelta(days=7)
    week_end = earliest - timedelta(days=earliest.weekday())
    weeks = [week_end-timedelta(weeks=i) for i in range(1,5)]
    month_end = min(weeks).replace(day=1)
    months=[]
    for _ in range(6):
        month_end=(month_end-timedelta(days=1)).replace(day=1)
        months.append(month_end.strftime('%Y-%m'))
    keys = [('lessons','lessons')] + [('day',k) for k in days(earliest,today)] + [('week',f'{d.isocalendar().year}-W{d.isocalendar().week:02}') for d in reversed(weeks)] + [('month',k) for k in reversed(months)]
    available={(r['layer'],r['key']):r['content'] for r in latest(db) if r['content'] is not None}
    return '\n\n'.join(f'### {layer} {key}\n{available[layer,key]}' for layer,key in keys if (layer,key) in available)


def index(db):
    """What is on file, one line per layer: the wake recalls the rest with memory show."""
    held = {}
    for r in latest(db):
        if r['content'] is not None and r['layer'] not in ('synthesis',):
            held.setdefault(r['layer'], []).append(r['key'])
    lines = []
    for layer in ('day', 'week', 'month'):
        keys = sorted(held.get(layer, []))
        if keys:
            lines.append(f'- {layer}: {len(keys)} on file, {keys[0]} … {keys[-1]}' if len(keys) > 3 else f'- {layer}: ' + ', '.join(keys))
    if 'lessons' in held:
        lines.append('- lessons: on file')
    return '\n'.join(lines) or '- nothing on file yet'


# An oversized decision page is a visible failure, never permission to omit inputs.
BRIEFING_BYTES = 96 * 1024


def system_prompt(prompt, source=None):
    """Rarely changing text precedes the variable page in the harness cache."""
    return '\n\n'.join([mind.read('soul.md', source), mind.read('charter.md', source), prompt])


def briefing(db, now, state, receipt=None):
    carried = next((r['content'] for r in latest(db) if r['layer'] == 'synthesis' and r['content'] is not None), None)
    memory_text = ('## What I carry\n' + (carried or '(no synthesis yet: consolidation writes the first one)') +
                   '\n\n## Memories on file\n' + index(db) +
                   '\nRecall one with `steward memory show LAYER KEY`; the fading view with `steward memory recall`.')
    from . import briefing as page
    receipt = receipt if receipt is not None else {}
    receipt.update(complete=False)
    from .timing import moments
    state = dict(state, runs=jobs.current(db, 'runs'), moments=moments(db))
    if 'day_close' in state.get('rituals', []):
        state['day_close'] = page.day_close(db, now)
    text = memory_text + '\n\n' + page.render(state, now, receipt)
    receipt['bytes'] = len(text.encode())
    if receipt['bytes'] > BRIEFING_BYTES:
        receipt['error'] = f"briefing page exceeds {BRIEFING_BYTES} byte limit ({receipt['bytes']} bytes)"
        raise StateError(receipt['error'])
    receipt.update(complete=True)
    return text


def provocations(db, now=None, root=None, source=None):
    text=mind.read('soul.md', source).split('Provocations (code draws one per week memory):',1)[1].split('\n## ',1)[0]
    options=[(int(n),t) for n,t in re.findall(r'^(\d+)\. (.+)$',text,re.M)]
    jobs.require(bool(options), 'no provocations in soul')
    for item in due(db,now,root):
        if item['layer']=='week' and not db.execute('SELECT 1 FROM provocations WHERE week=?',(item['key'],)).fetchone():
            number,text=secrets.choice(options)
            with db:
                db.execute('INSERT INTO provocations VALUES (?,?,?,?)',(item['key'],number,text,(now or jobs.now_utc()).isoformat()))
    return [dict(r) for r in db.execute('SELECT * FROM provocations ORDER BY week')]


def experiments(db):
    changes=[dict(r) for r in db.execute('SELECT * FROM changes ORDER BY rowid')]
    undone={r['undo_of'] for r in changes if r['undo_of']}
    result=[]
    for r in changes:
        reasons=[r['summary']]+[v[0] for v in db.execute('SELECT reason FROM runs WHERE change_id=?',(r['id'],))]
        reason=next((s for s in reasons if s.startswith('experiment:')),None)
        closed=any(s['summary'].startswith('experiment closed:') and r['id'] in s['summary'] for s in changes)
        closed=closed or any(v[0].startswith('experiment closed:') and r['id'] in v[0] for v in db.execute('SELECT reason FROM runs'))
        if reason and r['id'] not in undone and not closed:
            end=re.search(r'\bends (\d{4}-\d{2}-\d{2})\b',reason)
            result.append(dict(r,reason=reason,ends=end[1] if end else None))
    return result

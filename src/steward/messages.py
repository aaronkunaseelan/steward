"""Owner messages, moments and delivery, independent of model availability."""
from .settings import local_zone
from .memory import local_day
import json
import re
import subprocess
from datetime import timedelta
from . import contracts, jobs, senses, telegram_files
from .notify import notify, flush, binding, updates, telegram_sender
from .state import StateError


def inbox(db, telegram=None):
    if telegram is None:
        try:
            telegram = binding()
        except (OSError, ValueError):
            telegram = {}
    topics = telegram.get('topics', {})
    rows = []
    for row in db.execute('SELECT * FROM inbox WHERE handled=0 ORDER BY update_id'):
        item = dict(row)
        item['attachments'] = json.loads(item['attachments'])
        item['topic'] = next((k for k, v in topics.items() if v == item['thread_id']), str(item['thread_id']))
        lines = [item['text']] if item['text'] else []
        for ref in item['attachments']:
            result = ref.get('result') or dict(state='pending', reason='not_processed')
            text = result.get('text')
            lines.append(f"[{ref['kind']}: {text}]" if text else
                         f"[{ref['kind']}: not read ({result['state']}/{result['reason']})]")
        item['text'] = '\n'.join(lines)
        rows.append(item)
    return rows


def outstanding(db):
    """Read-only watch view; ids are the same update ids accepted by inbox done."""
    report = dict(schema='steward.inbox.v1', outstanding=[
        dict(id=item['update_id'], at=item['at']) for item in inbox(db, {})])
    errors = contracts.issues(contracts.load('steward.inbox.v1'), report)
    if errors:
        raise contracts.ContractError('steward.inbox.v1: ' + '; '.join(errors[:3]))
    for item in report['outstanding']:
        jobs.instant(item['at'])  # Validate ISO time and required offset.
    return report


def extract(ref, downloader, runner):
    try:
        data = telegram_files.bounded(ref, downloader)
        completed = runner(['qmd', 'extract', '-', '--json'],
                           input=data, capture_output=True, timeout=420)
        try:
            payload = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return dict(state='unknown', reason='qmd_exit' if completed.returncode else 'qmd_json')
        if contracts.issues(contracts.load('qmd.extract.v1', vendor=True), payload):
            return dict(state='unknown', reason='qmd_contract')
        if payload['error'] is not None or completed.returncode == 4:
            return dict(state='failed', reason=payload['error'] or 'qmd_exit_4',
                        kind=payload['kind'], text=None)
        if completed.returncode:
            return dict(state='unknown', reason='qmd_exit')
        return dict(state='done', reason=None, kind=payload['kind'],
                    text=payload['text'][:4000] if payload['text'] is not None else None)
    except subprocess.TimeoutExpired:
        return dict(state='unknown', reason='qmd_timeout')
    except telegram_files.Failed as exc:
        code = str(exc) if str(exc) in ('too_large', 'timeout', 'download') else 'download'
        return dict(state='too_large' if code == 'too_large' else 'unknown', reason=code)
    except Exception:
        return dict(state='unknown', reason='attachment_error')


def read_attachments(root, downloader=telegram_files.download, runner=subprocess.run):
    # Never hold the writer lock over network or qmd calls. Wake lock owns this pass.
    from .state import writer
    with writer(root) as store:
        rows = [dict(r) for r in store.db.execute('SELECT * FROM inbox WHERE handled=0 ORDER BY update_id')]
    for row in rows:
        refs = json.loads(row['attachments'])
        for ref in refs:
            if ref.get('result') is not None:
                continue
            ref['result'] = extract(ref, downloader, runner)
            with writer(root, wait=60) as store:
                with store.db:
                    store.db.execute('UPDATE inbox SET attachments=? WHERE update_id=?',
                                     (json.dumps(refs), row['update_id']))


def done(db, ids, now, actor):
    jobs.require(ids and all(db.execute('SELECT 1 FROM inbox WHERE update_id=?', (i,)).fetchone() for i in ids), 'unknown inbox id')
    with db:
        cid = jobs.change(db, 'inbox done', now, actor)
        db.executemany('UPDATE inbox SET handled=1 WHERE update_id=?', [(i,) for i in ids])
    return dict(change_id=cid, handled=ids)


POLL_MAX_AGE = timedelta(minutes=60)  # Three 20-minute tick intervals.


def poll_status(db, now):
    row = db.execute("SELECT value FROM message_meta WHERE key='inbound_poll'").fetchone()
    data = json.loads(row[0]) if row else dict(last_poll_ok_at=None, last_error=None)
    ok = jobs.instant(data['last_poll_ok_at']) if data['last_poll_ok_at'] else None
    reason = ('Telegram inbound poll has never succeeded' if ok is None else
              'Telegram inbound poll last succeeded over 60 min ago' if now - ok > POLL_MAX_AGE else
              'Telegram inbound poll failed: ' + data['last_error']['code'] if data['last_error'] else None)
    return dict(data, status='unknown' if reason else 'ok', reason=reason)


def poll(db, now, settings, sender=telegram_sender, fetcher=updates, telegram=None):
    try:
        telegram = telegram or binding()
        row = db.execute("SELECT value FROM message_meta WHERE key='offset'").fetchone()
        incoming = fetcher(int(row[0]) if row else 0)
    except Exception as exc:
        # Provider text may contain the token: retain only the exception class.
        data = poll_status(db, now)
        health = dict(last_poll_ok_at=data['last_poll_ok_at'],
                      last_error=dict(code=type(exc).__name__, at=now.isoformat()))
        incoming = None
    else:
        health = dict(last_poll_ok_at=now.isoformat(), last_error=None)
    with db:
        db.execute("INSERT INTO message_meta VALUES ('inbound_poll',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (json.dumps(health),))
    if incoming is None:
        return False
    wake = False
    for update in incoming:
        uid = update['update_id']
        with db:
            db.execute("INSERT INTO message_meta VALUES ('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(uid + 1),))
            m = update.get('message', {})
            if m.get('chat', {}).get('id') != telegram['chat_id'] or m.get('from', {}).get('id') != telegram['owner_user_id']:
                continue
            words = m.get('text')
            if not isinstance(words, str):
                words = m.get('caption')
            text = words.strip() if isinstance(words, str) else ''
            media = [kind for kind in ('photo', 'document', 'audio', 'voice', 'video', 'video_note', 'animation', 'sticker') if kind in m]
            if not text and not media:
                continue
            thread = m.get('message_thread_id')
            reply = m.get('reply_to_message', {}).get('message_id')
            is_undo = text.lower() == 'undo' or text.lower().startswith('undo ')
            if db.execute('SELECT 1 FROM inbox WHERE update_id=?', (uid,)).fetchone():
                continue
            refs = []
            stored_text = text
            for kind in media:
                if kind == 'sticker':
                    stored_text = '\n'.join(filter(None, (stored_text, '[sticker]')))
                    continue
                file = (max(m[kind], key=lambda p: (p.get('width', 0) * p.get('height', 0), p.get('file_size', 0)))
                        if kind == 'photo' else m[kind])
                refs.append(dict(kind=kind, **{key: file.get(key) for key in
                            ('file_id', 'file_unique_id', 'mime_type', 'file_name', 'file_size')}))
            db.execute('INSERT INTO inbox (update_id,message_id,thread_id,at,text,reply_to,handled,attachments) VALUES (?,?,?,?,?,?,?,?)',
                       (uid, m['message_id'], thread, now.isoformat(), stored_text, reply, int(is_undo), json.dumps(refs)))
        if is_undo:
            try:
                if text.lower() == 'undo':
                    candidates = [r[0] for r in db.execute('SELECT DISTINCT change_id FROM outbox WHERE message_id=?', (reply,))]
                    linked = [r[0] for r in db.execute('SELECT DISTINCT mc.change_id FROM message_changes mc JOIN outbox o ON o.id=mc.outbox_id WHERE o.message_id=?', (reply,))]
                    candidates = linked or candidates
                else:
                    prefix = text.split(maxsplit=1)[1]
                    jobs.require(re.fullmatch(r'[0-9a-fA-F]{8,32}', prefix), 'undo needs a change prefix of at least 8 hex characters')
                    candidates = [r[0] for r in db.execute('SELECT id FROM changes WHERE id LIKE ?', (prefix + '%',))]
                jobs.require(len(candidates) == 1, 'undo target unknown or ambiguous; use a change id')
                original = candidates[0]
                from . import calendar_b
                is_draft = db.execute('SELECT 1 FROM drafts WHERE change_id=?', (original,)).fetchone()
                if is_draft:
                    from . import drafts
                    restored = drafts.undo(db, original, actor='owner', now=now)
                elif db.execute('SELECT 1 FROM blocks WHERE change_id=? UNION SELECT 1 FROM demands WHERE change_id=?', (original, original)).fetchone():
                    restored = calendar_b.undo(db, settings, original, now, 'owner')
                else:
                    restored = jobs.undo(db, original, now, 'owner')
                subjects = [r[0] for r in db.execute('SELECT name FROM jobs WHERE change_id=? UNION SELECT job FROM runs WHERE change_id=?', (restored['change_id'], restored['change_id']))]
                answer = ('Deleted Gmail draft' if is_draft else
                          'Restored ' + (', '.join(subjects) or 'calendar change') + ' · undo ' + restored['change_id'][:8])
            except StateError as exc:
                answer = 'Undo refused: ' + str(exc)
            except (ValueError, OSError, KeyError, TypeError):
                answer = 'Undo refused: constraints, an owner edit, or an unknown calendar prevented safe restoration.'
            topic = next((k for k, v in telegram['topics'].items() if v == thread), 'Talk')
            notify(db, topic, answer, sender=sender, now=now, actor='tick', settings=settings, thread_id=thread, deliver='immediate')
        else:
            wake = True
    return wake


def queued(db):
    return db.execute("SELECT count(*) FROM outbox WHERE topic='Finds' AND state='pending' AND delivery='digest'").fetchone()[0]


def clipped(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def fallback(db, now):
    observations = senses.snapshot(db, now)
    cal, gsd = observations['calendars'], observations['gsd']
    unknown = []
    for label, reading in (('calendar', cal), ('your list', gsd)):
        if reading['verdict'] != 'fresh':
            unknown.append(clipped(f"{label} {reading['verdict']}: {reading['reason']}", 180))
    events = []
    for e in cal.get('data', {}).get('events', []):
        start = e['start'].get('dateTime', e['start'].get('date', ''))
        when = jobs.instant(start).astimezone(local_zone()) if 'T' in start else None
        day = when.date().isoformat() if when else start
        if day == str(local_day(now)) and e.get('status') != 'cancelled':
            events.append((when.timestamp() if when else float('-inf'), (when.strftime('%H:%M') if when else 'All day') +
                           ' ' + (e.get('summary') or 'Busy')))
    groups = [('Today', [v for _, v in sorted(events)[:6]] or
               ['No events observed' if cal['verdict'] == 'fresh' else 'Not currently known'], cal)]
    for label, key, empty in (('Your list', 'waiting_on_you', 'No open items of yours'),
                              ('Waiting', 'waiting_on_others', 'No items waiting on others')):
        priorities = gsd.get('data', {}).get(key)
        items = ([str(i.get('title') or i['id']) for i in priorities[:6]] or [empty]) if priorities is not None else ['Not currently known']
        groups.append((label, items, gsd))
    # Reserve the final unknown group before shortening items; never clip markup or a warning.
    header = '**' + now.astimezone(local_zone()).strftime('%a %d %b') + ' · Current state (fallback)**'
    ending = "**Can't see**\n" + ('\n'.join(unknown) if unknown else 'Nothing missing from these readings')
    from .notify import TODAY_CHARACTERS
    overhead = len(header) + len(ending) + 12 + sum(len(label) + 7 +
               (24 if reading['verdict'] != 'fresh' else 0) for label, _, reading in groups)
    budget = max(12, (TODAY_CHARACTERS - overhead) // sum(len(items) for _, items, _ in groups))
    sections = [header]
    for label, items, reading in groups:
        qualifier = ['Last seen, not current'] if reading['verdict'] != 'fresh' and items != ['Not currently known'] else []
        sections.append('\n'.join(['**' + label + '**', *qualifier, *(clipped(i, budget) for i in items)]))
    return '\n\n'.join([*sections, ending])


def calendar_changes(db, identity):
    """Use stored before/after fields, including edits recorded before the new summary shape."""
    from .calendar_b import change_summary
    entries = []
    for row in db.execute("SELECT b.* FROM blocks b WHERE change_id=? AND state='intended' AND version=(SELECT min(version) FROM blocks WHERE event_key=b.event_key AND change_id=b.change_id AND state='intended') ORDER BY rowid", (identity,)):
        if re.search(r'(^|[-_:])canary($|[-_:])', row['demand'], re.I):
            continue
        final = db.execute('SELECT state FROM blocks WHERE event_key=? AND change_id=? ORDER BY version DESC LIMIT 1', (row['event_key'], identity)).fetchone()
        if final['state'] not in ('applied', 'deleted'):
            return None  # An incomplete apply is not an accomplished calendar change.
        after = json.loads(row['payload'])
        prior = db.execute("SELECT payload FROM blocks WHERE event_key=? AND version<? AND state='applied' ORDER BY version DESC LIMIT 1", (row['event_key'], row['version'])).fetchone()
        before = json.loads(prior[0]) if prior else None
        action = ('drop' if after.get('delete') else 'keep' if before and
                  (before['start'], before['end']) == (after['start'], after['end']) else 'place')
        entries.append(change_summary(before, after, action, after['reason']))
    return '\n\n'.join(entries)


def visible_change(db, row):
    verb, identity = row['verb'], row['id']
    if row['actor'] not in ('owner', 'model'):
        return ''
    if verb == 'block choose':
        return calendar_changes(db, identity)
    if verb in ('draft', 'undo draft'):
        return row['summary']
    if verb == 'moment set' or (verb == 'undo' and db.execute('SELECT 1 FROM moments WHERE change_id=?', (identity,)).fetchone()):
        return '\n\n'.join(r['start'] + '–' + r['deadline'] + ' ' +
            r['name'].replace('_', ' ') + ' · updated' for r in
            db.execute('SELECT * FROM moments WHERE change_id=?', (identity,)))
    if verb.startswith(('job ', 'run ')) or verb == 'undo':
        requested = db.execute('SELECT 1 FROM message_meta WHERE key=?', ('owner_request:' + identity,)).fetchone()
        if row['actor'] != 'owner' and not (row['actor'] == 'model' and requested):
            return ''
        subjects = [x[0] for x in db.execute('SELECT name FROM jobs WHERE change_id=? UNION SELECT job FROM runs WHERE change_id=?', (identity, identity))]
        if not subjects or any(re.search(r'(^|[-_:])canary($|[-_:])', s, re.I) for s in subjects):
            return ''
        reasons = list(dict.fromkeys([r[0] for r in db.execute('SELECT reason FROM runs WHERE change_id=?', (identity,))] +
                      [json.loads(r[0])['justification'] for r in db.execute('SELECT spec_json FROM jobs WHERE change_id=?', (identity,))]))
        return verb.capitalize() + ' ' + ', '.join(subjects) + ('\n' + '; '.join(reasons) if reasons else '')
    return ''


def changes_text(rows):
    header = '**Your calendar**' if all(r['verb'] == 'block choose' for r, _ in rows) else '**Changes**'
    multiple = len(rows) > 1
    entries = [('You: ' if r['actor'] == 'owner' else '') + text +
               ('\nUndo: ' + r['id'][:8] if multiple else '') for r, text in rows]
    hint = ('Reply "undo ID" with the change’s Undo id to undo that change.' if multiple else
            'Reply "undo" to undo it.')
    return '\n\n'.join([header, *entries, hint])


def changes(db, now, sender, settings, key=None):
    # Hidden bookkeeping gets its own reported marker, never an undo link on a message.
    rows = db.execute("SELECT rowid,* FROM changes WHERE NOT EXISTS (SELECT 1 FROM message_changes m WHERE m.change_id=changes.id) AND NOT EXISTS (SELECT 1 FROM message_meta m WHERE m.key='changes_reported:' || changes.id) ORDER BY rowid").fetchall()
    shown = []
    with db:
        for row in rows:
            text = visible_change(db, row)
            if text is None:
                continue
            if text:
                shown.append((row, clipped(text, 3300)))
            else:
                db.execute('INSERT INTO message_meta VALUES (?,?)', ('changes_reported:' + row['id'], 'true'))
    batch = []
    batches = []
    for entry in shown:
        if batch and len(changes_text([*batch, entry])) > 3900:
            batches.append(batch)
            batch = []
        batch.append(entry)
    if batch:
        batches.append(batch)
    for i, batch in enumerate(batches):
        notify(db, 'Changes', changes_text(batch),
               key if len(batches) == 1 else f'{key}:{i}' if key else None,
               sender, now, 'tick', settings=settings, change_id=batch[0][0]['id'],
               related_changes=[r['id'] for r, _ in batch])


def alerts(db, now, sender, settings):
    health = db.execute("SELECT * FROM senses WHERE name='health' ORDER BY id DESC LIMIT 1").fetchone()
    if health and health['status'] == 'ok':
        auth = json.loads(health['payload'])['data']['auth']
        row = db.execute("SELECT value FROM message_meta WHERE key='garmin_auth'").fetchone()
        prior = json.loads(row[0]) if row else {}
        # Persist the episode key: bounded sense history and read outages must not
        # repeat a sign-in request. A later known auth state starts a new episode.
        if prior.get('auth') != auth:
            prior = dict(auth=auth, key=f"alert:garmin:signed_out:{health['id']}")
            with db:
                db.execute("INSERT INTO message_meta VALUES ('garmin_auth',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(prior),))
        if auth == 'signed_out':
            notify(db, 'Alerts',
                   'Garmin needs you to sign in again: run `~/tools/garmin/bin/garmin-login` on OCI',
                   prior['key'], sender, now, 'tick', settings=settings)
    problems = []
    machine = senses.snapshot(db, now)['machine']
    if machine['status'] == 'ok' and machine.get('data', {}).get('violation'):
        problems.append(('violation', machine['data']['violation'], machine['at'],
                         'Disable gsd-step.timer; Steward owns the run schedule.'))
    for name in ('calendars', 'gsd', 'sol'):
        rows = db.execute('SELECT * FROM senses WHERE name=? ORDER BY id DESC', (name,)).fetchall()
        unknown = []
        for r in rows:
            if r['status'] != 'unknown':
                break
            unknown.append(r)
        if unknown and now - jobs.instant(unknown[-1]['at']) > timedelta(hours=2):
            problems.append(('sense', name, unknown[-1]['at'], 'Check credentials and the read CLI.'))
    sol = senses.snapshot(db, now)['sol']
    if sol.get('data', {}).get('rate_limit_reached_type') is not None:
        problems.append(('limit', 'Sol', sol['at'], 'Wait for the provider reset or check the subscription.'))
    runs = jobs.current(db, 'runs')
    for name in {r['job'] for r in runs}:
        last = sorted([r for r in runs if r['job'] == name and r['state'] in ('failed','succeeded')], key=lambda r:r['ended_at'] or '', reverse=True)[:3]
        if len(last) == 3 and all(r['state'] == 'failed' for r in last):
            problems.append(('job', name, last[-1]['ended_at'], 'Inspect the job command and credentials; pause it if needed.'))
    wakes = db.execute("SELECT * FROM wakes WHERE status IN ('completed','failed') ORDER BY id DESC LIMIT 2").fetchall()
    if len(wakes) == 2 and all(r['status'] == 'failed' for r in wakes):
        problems.append(('wake', 'harness', wakes[-1]['started_at'], 'Check tty/Codex authentication and harness availability.'))
    for kind, subject, since, action in problems:
        key = f'alert:{kind}:{subject}:{local_day(now)}'
        notify(db, 'Alerts', f'{subject}: {kind} problem since {since}. {action}', key, sender, now, 'tick', settings=settings)

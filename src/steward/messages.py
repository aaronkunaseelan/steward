"""Owner messages, rituals and delivery, independent of model availability."""
from .settings import local_zone
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
                    restored = drafts.undo(db, settings, original, actor='owner', now=now)
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
            notify(db, topic, 'change', answer, sender=sender, now=now, actor='tick', settings=settings, thread_id=thread, deliver='immediate')
        else:
            wake = True
    return wake


def queued(db):
    return db.execute("SELECT count(*) FROM outbox WHERE topic='Finds' AND state='pending' AND delivery='digest'").fetchone()[0]


def fallback(db, now):
    from .timing import owner_time
    observations = senses.snapshot(db, now)
    lines = ['Current state (fallback)', owner_time(now, '%A %d %b, %H:%M'), 'Today']
    cal = observations['calendars']
    events = []
    for e in cal.get('data', {}).get('events', []):
        start = e['start'].get('dateTime', e['start'].get('date', ''))
        if start[:10] == str(now.astimezone(local_zone()).date()):
            clock = owner_time(jobs.instant(start), '%H:%M') if 'T' in start else 'All day'
            events.append((start, clock + ' ' + (e.get('summary') or 'Busy')))
    if cal['verdict'] != 'fresh':
        lines += [f"Calendars {cal['verdict']}: {cal['reason']}"]
    lines += [v[:160] for _, v in sorted(events)[:6]] or ['No events observed']
    lines += ['Your items']
    gsd = observations['gsd']
    if gsd['verdict'] != 'fresh':
        lines += [f"gsd {gsd['verdict']}: {gsd['reason']}"]
    for key, empty in (('waiting_on_you', 'No open items of yours'), ('waiting_on_others', 'No items waiting on others')):
        if key == 'waiting_on_others':
            lines += ['Waiting on others']
        priorities = gsd.get('data', {}).get(key)
        lines += ([str(i.get('title') or i['id'])[:140] for i in priorities] or [empty]) if priorities is not None else ['Unknown: refresh gsd sense']
    lines += ['Compute today']
    runs = [r for r in jobs.current(db, 'runs') if r['state'] == 'planned' and jobs.instant(r['planned_for']).astimezone(local_zone()).date() == now.astimezone(local_zone()).date()]
    lines += [(owner_time(jobs.instant(r['planned_for']), '%H:%M') + ' ' + r['job'] + ': ' + r['reason'])[:160] for r in sorted(runs, key=lambda r:r['planned_for'])[:5]] or ['No planned jobs']
    lines += ['Finds waiting', str(queued(db)) + ' queued', 'Next actions', 'Review Today; send instructions in Talk.']
    from .notify import TODAY_CHARACTERS
    return '\n'.join(lines)[:TODAY_CHARACTERS]


def changes(db, now, sender, settings, key=None):
    # Wake-start cursors omit edits between wakes; linkage records what was reported.
    # Migration 17 links pre-release edits to a suppressed outbox row.
    rows = db.execute("SELECT rowid,* FROM changes WHERE actor NOT IN ('tick','migration') AND verb NOT IN ('notify','inbox done','tick skip') AND NOT EXISTS (SELECT 1 FROM tick_skips s WHERE s.change_id=changes.id) AND NOT EXISTS (SELECT 1 FROM message_changes m WHERE m.change_id=changes.id) ORDER BY rowid").fetchall()
    if not rows:
        return
    counts = {}
    for r in rows:
        subjects = [x[0] for x in db.execute('SELECT name FROM jobs WHERE change_id=? UNION SELECT job FROM runs WHERE change_id=?', (r['id'], r['id']))]
        reasons = ['Retry after a failed run' if x['retry_of'] else x['reason']
                   for x in db.execute('SELECT reason,retry_of FROM runs WHERE change_id=?', (r['id'],))]
        reasons += [json.loads(x[0])['justification'] for x in db.execute('SELECT spec_json FROM jobs WHERE change_id=?', (r['id'],))]
        # Plain words: the verb on its subjects and why, or the change's own summary.
        line = ' '.join([r['verb']] + [', '.join(subjects)] * bool(subjects)) + ': ' + '; '.join(reasons) if reasons else r['summary']
        who = {'model': 'Steward', 'owner': 'You', 'supervisor': 'Supervisor'}.get(r['actor'], r['actor'])
        line = who + ': ' + line[:1].upper() + line[1:]
        counts[line] = counts.get(line, 0) + 1
    lines = [line + (f' (×{count})' if count > 1 else '') for line, count in counts.items()]
    # Reply-to-message undo uses message_changes; internal ids need not reach the owner.
    budget = max(20, 3900 // len(lines))
    text = '\n'.join(line if len(line) <= budget else line[:budget-1] + '…' for line in lines)
    notify(db, 'Changes', 'change', text, key, sender, now, 'tick', settings=settings,
           change_id=rows[0]['id'], related_changes=[r['id'] for r in rows])


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
            notify(db, 'Alerts', 'alert',
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
        key = f'alert:{kind}:{subject}:{now.astimezone(local_zone()).date()}'
        notify(db, 'Alerts', 'alert', f'{subject}: {kind} problem since {since}. {action}', key, sender, now, 'tick', settings=settings)

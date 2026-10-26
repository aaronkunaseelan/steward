"""Record notification intent before sending through the Bot API."""
from datetime import datetime, timedelta
import html
import json
import os
import re
import urllib.parse
import tomllib
import urllib.request
import uuid

from .jobs import change, now_utc, require
from .memory import local_day
from .runtime import config_dir
from .timing import moments, moment_at

TODAY_CHARACTERS = 1200

TOPICS = ('Today', 'Changes', 'Finds', 'Alerts', 'Talk')


def binding():
    return tomllib.loads((config_dir() / 'telegram.toml').read_text())


def bot_token():
    token = os.environ.get('STEWARD_TELEGRAM_BOT_TOKEN')
    require(bool(token), 'Telegram token unavailable')
    return token


def api(method, payload):
    token = bot_token()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    require(result.get('ok') is True, 'Telegram send failed')
    return result['result']



def telegram_sender(topic, text, **options):
    settings = binding()
    thread = options.pop('thread_id', None) or settings['topics'][topic]
    options.setdefault('disable_notification', True)
    return api('sendMessage', dict(chat_id=settings['chat_id'], message_thread_id=thread,
               text=text, link_preview_options={'is_disabled': True}, **options))['message_id']


def updates(offset):
    return api('getUpdates', dict(offset=offset, timeout=0, allowed_updates=['message']))


def interrupt_data(row):
    # Ordinary delivery values stay unchanged; explicit interrupts carry their audit here.
    return json.loads(row['delivery']) if row['delivery'].startswith('{') else None


def interrupt_records(db):
    records = []
    for row in db.execute("SELECT * FROM outbox WHERE delivery LIKE '{%' ORDER BY rowid"):
        data = interrupt_data(row)
        if data['at'] is not None:
            records.append(dict(data, outbox_id=row['id'], topic=row['topic'],
                                message_id=row['message_id'], state=row['state']))
    return records


def interrupt_status(db, now, cap=3):
    from .settings import local_zone
    require(type(cap) is int and cap >= 0, 'interrupt_cap must be a non-negative integer')
    day = local_day(now)
    records = [r for r in interrupt_records(db)
               if local_day(datetime.fromisoformat(r['at'])) == day]
    return dict(day=day.isoformat(), cap=cap, used=sum(not r['over_cap'] for r in records),
                over_cap=sum(r['over_cap'] for r in records), records=records)


LINK = re.compile(r'\[([^\]\n]+)\]\((https?://(?:[^\s<>()]|\([^\s<>()]*\))+)\)')
BARE = re.compile(r'(?i)(?:https?://|www\.)')
BOLD = re.compile(r'\*\*((?:(?!\*\*).)+)\*\*', re.S)


def plain_text(text, start, stop, bold):
    """Only complete bold pairs within this link-free slice are markup."""
    pieces, visible, end = [], [], start
    for match in bold:
        if not start <= match.start() < match.end() <= stop:
            continue
        pieces.extend((html.escape(text[end:match.start()]),
                       '<b>' + html.escape(match[1]) + '</b>'))
        visible.extend((text[end:match.start()], match[1]))
        end = match.end()
    pieces.append(html.escape(text[end:stop]))
    visible.append(text[end:stop])
    return ''.join(pieces), ''.join(visible)


def render(db, text, now, expired_pages=False):
    """Escape everything except bold and our validated Telegram anchors."""
    pieces, visible, end = [], [], 0
    bold = list(BOLD.finditer(text))
    for match in LINK.finditer(text):
        plain = text[end:match.start()]
        label, url = match.groups()
        require(not BARE.search(plain) and not BARE.search(label), 'bare URL refused; use [label](https://...)')
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password, 'invalid hyperlink URL')
        from .settings import pages_host
        if parsed.hostname.lower() == urllib.parse.urlsplit(pages_host()).hostname:
            from .pages import valid_slug
            slug = parsed.path[1:]
            require(valid_slug(slug) and not parsed.query and not parsed.fragment and parsed.scheme == 'https' and parsed.port in (None, 443), 'invalid Steward page link')
            row = db.execute('SELECT state,expires_at FROM pages WHERE slug=?', (slug,)).fetchone()
            from .jobs import instant
            live = row is not None and row['state'] == 'live' and instant(row['expires_at']) > now
            if not live and expired_pages:
                fallback = label + ' (page expired)'
                formatted, words = plain_text(text, end, match.start(), bold)
                pieces.extend((formatted, html.escape(fallback)))
                visible.extend((words, fallback))
                end = match.end()
                continue
            require(live, 'Steward page link is not live or has expired')
        formatted, words = plain_text(text, end, match.start(), bold)
        pieces.extend((formatted, '<a href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>'))
        visible.extend((words, label))
        end = match.end()
    tail = text[end:]
    require(not BARE.search(tail), 'bare URL refused; use [label](https://...)')
    formatted, words = plain_text(text, end, len(text), bold)
    pieces.append(formatted)
    visible.append(words)
    return ''.join(pieces), ''.join(visible)


def canonical_key(key):
    """URL identity: lowercase host, no fragment or tracking query; opaque keys stay opaque."""
    parsed = urllib.parse.urlsplit(key)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return key
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'dclid', 'msclkid', 'mc_cid', 'mc_eid')]
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if parsed.port is not None:
        host += ':' + str(parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, urllib.parse.urlencode(query), ''))


def notification_status(db):
    """Bounded failure codes only, never message bodies or exception strings."""
    from .pages import read_meta
    return {state: [dict(outbox_id=r['id'], topic=r['topic'],
                         **read_meta(db, 'outbox_error:' + r['id'], {'code': 'unknown', 'at': None}))
                    for r in db.execute("SELECT id,topic FROM outbox WHERE state=? AND "
                        "(state='failed' OR EXISTS (SELECT 1 FROM message_meta m WHERE m.key='outbox_error:' || outbox.id)) "
                        "ORDER BY rowid DESC LIMIT 20", (state,))]
            for state in ('failed', 'pending')}


def send_rows(db, rows, sender, now, settings):
    # Validate separately so one bad digest row cannot strand unrelated messages.
    from .pages import meta
    ready, rendered, lengths = [], [], []
    for row in rows:
        code = None
        try:
            text, visible = render(db, row['text'], now, expired_pages=True)
            if not 0 < len(visible) <= 4000:  # transport limit; Today's limit applies once, when queued
                code = 'message_too_long'
        except (ValueError, TypeError):
            code = 'render_invalid'
        if code:
            with db:
                db.execute("UPDATE outbox SET state='failed' WHERE id=?", (row['id'],))
                meta(db, 'outbox_error:' + row['id'], dict(code=code, at=now.isoformat()))
        else:
            ready.append(row)
            rendered.append(text)
            lengths.append(len(visible))
    if not ready:
        return False
    if sum(lengths) + 2 * (len(ready) - 1) > 4000:
        # Expiry suffixes can make a previously fitting digest overflow. Split it.
        results = [send_rows(db, [row], sender, now, settings) for row in ready]
        return all(results) and len(ready) == len(rows)
    complete = len(ready) == len(rows)
    rows, text = ready, '\n\n'.join(rendered)
    interrupt = False
    if len(rows) == 1:
        # Re-read so retrying even a stale row cannot spend a second interrupt.
        data = interrupt_data(db.execute('SELECT * FROM outbox WHERE id=?', (rows[0]['id'],)).fetchone())
        if data is not None and data['at'] is None:
            budget = interrupt_status(db, now, settings.get('interrupt_cap', 3))
            interrupt = budget['used'] < budget['cap']
            data.update(at=now.isoformat(), over_cap=not interrupt)
            # Commit the budget before the outward call. An ambiguous failure may have
            # buzzed: it consumes the slot and all retries of this message are silent.
            with db:
                db.execute('UPDATE outbox SET delivery=? WHERE id=?', (json.dumps(data), rows[0]['id']))
    # Persist ambiguity before the call: even a process interruption may have delivered.
    with db:
        for row in rows:
            meta(db, 'outbox_error:' + row['id'], dict(code='send_outcome_unknown', at=now.isoformat()))
    try:
        options = dict(disable_notification=not interrupt, parse_mode='HTML')
        if rows[0]['thread_id'] is not None:
            options['thread_id'] = rows[0]['thread_id']
        elif sender is telegram_sender:
            options['thread_id'] = binding()['topics'][rows[0]['topic']]
        from .pages import read_meta
        reply_to = read_meta(db, 'outbox_reply:' + rows[0]['id'])
        if reply_to is not None:
            options['reply_parameters'] = dict(message_id=reply_to, allow_sending_without_reply=True)
        mid = sender(rows[0]['topic'], text, **options)
        require(type(mid) is int, 'invalid message id')
    except Exception:
        return False
    with db:
        for row in rows:
            data = interrupt_data(db.execute('SELECT * FROM outbox WHERE id=?', (row['id'],)).fetchone())
            if data is not None:
                data['buzz'] = interrupt
                db.execute('UPDATE outbox SET delivery=? WHERE id=?', (json.dumps(data), row['id']))
            db.execute("UPDATE outbox SET state='sent',message_id=?,sent_at=?,thread_id=? WHERE id=?",
                       (mid, now.isoformat(), options.get('thread_id'), row['id']))
            db.execute("DELETE FROM message_meta WHERE key=?", ('outbox_error:' + row['id'],))
            keys = {r[0] for r in db.execute('SELECT key FROM outbox_keys WHERE outbox_id=?', (row['id'],))}
            db.executemany('INSERT OR IGNORE INTO told VALUES (?,?,?,?,?,?)',
                           [(key, now.isoformat(), row['job'], row['run_id'], row['topic'], mid) for key in keys])
    return complete


def flush(db, sender=telegram_sender, now=None, settings=None):
    from .settings import local_zone
    now = now or now_utc()
    settings = settings or {}
    batch = []
    for row in db.execute("SELECT o.*, c.at AS queued_at FROM outbox o JOIN changes c ON c.id=o.change_id WHERE o.state='pending' ORDER BY o.rowid").fetchall():
        if row['delivery'] == 'digest':
            # One evening drop: a Find joins the first day close at or after it was queued.
            queued = datetime.fromisoformat(row['queued_at']).astimezone(local_zone())
            moment = next(m for m in moments(db) if m['releases'] == 'digest')
            drop = moment_at(moment, queued, 'start')
            if drop < queued:
                drop = moment_at(moment, queued + timedelta(days=1), 'start')
            if now < drop:
                continue
            if batch and sum(len(LINK.sub(lambda m: m[1], r['text'])) + 2 for r in batch) + len(LINK.sub(lambda m: m[1], row['text'])) > 4000:
                send_rows(db, batch, sender, now, settings)
                batch = []
            batch.append(row)
        else:
            send_rows(db, [row], sender, now, settings)
    if batch:
        send_rows(db, batch, sender, now, settings)


def notify(db, topic, text, told_key=None, sender=telegram_sender, now=None,
           actor='owner', deliver=None, settings=None, thread_id=None, change_id=None, related_changes=(),
           interrupt=None, moment=None, reply_to=None):
    now = now or now_utc()
    if reply_to is not None:
        require(type(reply_to) is int and reply_to > 0, 'invalid reply message id')
        target = db.execute("SELECT topic,thread_id FROM outbox WHERE state='sent' AND message_id=? ORDER BY rowid DESC LIMIT 1",
                            (reply_to,)).fetchone()
        require(target is not None, 'reply target is not a message Steward sent')
        require(topic is None or topic == target['topic'], '--topic disagrees with reply target')
        require(thread_id is None or thread_id == target['thread_id'], 'thread disagrees with reply target')
        topic, thread_id = target['topic'], target['thread_id']
    require(topic in TOPICS, 'invalid notification route')
    if moment is not None:
        from .timing import moment_key
        require(topic == 'Today', 'moments must be sent to Today')
        require(told_key is None, 'use moment or told key, not both')
        told_key = moment_key(db, moment, now)
    require(isinstance(text, str), 'invalid message text')
    _, visible = render(db, text, now)
    require(0 < len(visible) <= 4000, 'invalid message length')
    require(topic != 'Today' or len(visible) <= TODAY_CHARACTERS,
            f'Today must be at most {TODAY_CHARACTERS} characters; put the detail on a page')
    keys = [] if told_key is None else [told_key] if isinstance(told_key, str) else told_key
    require(isinstance(keys, (list, tuple)) and all(isinstance(key, str) and key.strip() for key in keys), 'invalid told key')
    keys = list(dict.fromkeys(canonical_key(key) for key in keys))
    if interrupt is not None:
        require(isinstance(interrupt, str) and bool(interrupt.strip()), 'interrupt requires a reason')
        require(actor != 'tick', 'code-written messages cannot interrupt')
        require(deliver in (None, 'immediate'), 'interrupt requires immediate delivery')
        interrupt_status(db, now, (settings or {}).get('interrupt_cap', 3))
    deliver = 'immediate' if interrupt is not None else deliver or os.environ.get('STEWARD_DELIVER', 'digest')
    require(deliver in ('digest', 'immediate', 'silent'), 'invalid delivery')
    delivery = deliver if topic == 'Finds' and reply_to is None else 'immediate'
    if interrupt is not None:
        delivery = json.dumps(dict(reason=interrupt.strip(), at=None, over_cap=None))
    if actor == 'maintenance':
        delivery = 'silent'
    with db:
        cid = change_id or change(db, 'notify', now, actor)
        from .capabilities import told_check
        if any(told_check(db, keys).values()):
            return {'change_id': cid, 'status': 'already_told'}
        identity = uuid.uuid4().hex
        db.execute("INSERT INTO outbox(id,change_id,topic,text,told_key,state,thread_id,delivery) VALUES (?,?,?,?,?,?,?,?)", (identity, cid, topic, text, None, 'suppressed' if delivery == 'silent' else 'pending', thread_id, delivery))
        db.executemany('INSERT INTO outbox_keys VALUES (?,?)', [(identity, key) for key in keys])
        db.execute('UPDATE outbox SET job=?,run_id=? WHERE id=?', (os.environ.get('STEWARD_JOB'), os.environ.get('STEWARD_RUN_ID'), identity))
        db.executemany('INSERT INTO message_changes VALUES (?,?)', [(identity, c) for c in related_changes])
        from .pages import meta
        if reply_to is not None:
            meta(db, 'outbox_reply:' + identity, reply_to)
    # Credentialless jobs queue; the tick's flush must still expose delivery failures.
    if (delivery == 'immediate' or interrupt is not None) and (
            sender is not telegram_sender or os.environ.get('STEWARD_TELEGRAM_BOT_TOKEN')):
        send_rows(db, [db.execute('SELECT * FROM outbox WHERE id=?', (identity,)).fetchone()], sender, now, settings or {})
    row = db.execute('SELECT * FROM outbox WHERE id=?', (identity,)).fetchone()
    return dict(change_id=cid, outbox_id=identity, status=row['state'], message_id=row['message_id'])

"""Gmail drafts only. There is deliberately no mail-send operation."""
import json
import re
import subprocess
from urllib.parse import quote, urlsplit

from . import jobs
from .notify import notify, telegram_sender
from .state import StateError


def message_id(value, account):
    if value.startswith('gmail:'):
        parts = value.split(':')
        jobs.require(len(parts) == 3 and parts[1].lower() == account.lower(),
                     'reply account mismatch')
        value = parts[2]
    jobs.require(bool(re.fullmatch(r'[a-zA-Z0-9_-]+', value)), 'invalid Gmail message id')
    return value


def command(action, account):
    # Exact command gating plus the permanent no-send wall, including for undo.
    jobs.require(action in ('create', 'delete'), 'unsupported draft action')
    return ['gog', '--enable-commands-exact=gmail.drafts.' + action, '--gmail-no-send',
            '-j', 'gmail', 'drafts', action, '--account', account]


def run(argv, body=None):
    try:
        result = subprocess.run(argv, input=body, text=True, capture_output=True,
                                timeout=60, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never return stderr: provider errors may contain owner mail or credentials.
        raise StateError('Gmail draft outcome unknown; inspect Drafts before retrying') from None


def create(db, settings, body, reply_to=None, reply_all=False, to=None, subject=None,
           actor='owner', runner=run, sender=telegram_sender, now=None):
    now = now or jobs.now_utc()
    from .settings import required
    account = required('gmail_account', settings)
    jobs.require(isinstance(account, str) and '@' in account, 'invalid setting: gmail_account')
    jobs.require(bool(reply_to) != bool(to), 'choose reply-to or to')
    jobs.require(not reply_all or bool(reply_to), 'reply-all requires reply-to')
    jobs.require(isinstance(body, str) and body.strip(), 'draft body required')
    jobs.require(reply_to or (isinstance(subject, str) and subject.strip()), 'new draft subject required')
    argv = command('create', account)
    if reply_to:
        argv += ['--reply-to-message-id', message_id(reply_to, account), '--auto-from-addressed-alias']
        if reply_all:
            argv += ['--reply-all']
    else:
        jobs.require(isinstance(to, list) and all(isinstance(a, str) and '@' in a
                     and not any(c in a for c in '\r\n') for a in to), 'invalid recipients')
        argv += ['--to', ','.join(to)]
    if subject is not None:
        jobs.require('\n' not in subject and '\r' not in subject, 'invalid subject')
        argv += ['--subject', subject]
    argv += ['--body-file', '-']
    with db:
        cid = jobs.change(db, 'draft', now, actor)
        db.execute('INSERT INTO drafts(change_id,account,state) VALUES (?,?,?)', (cid, account, 'unknown'))
        db.execute('UPDATE changes SET summary=? WHERE id=?', ('Gmail draft outcome unknown; inspect Drafts before retrying', cid))
    result = runner(argv, body)
    draft = result.get('draft', result)
    identity = draft.get('draftId') or draft.get('id')
    jobs.require(isinstance(identity, str) and bool(re.fullmatch(r'[a-zA-Z0-9_-]+', identity)),
                 'Gmail draft outcome unknown: missing draft id')
    thread = draft.get('message', {}).get('threadId') or result.get('threadId')
    url = result.get('url') or draft.get('url') or result.get('threadUrl')
    if not url:
        target = '#all/' + quote(thread, safe='') if isinstance(thread, str) and thread else '#drafts'
        url = 'https://mail.google.com/mail/u/' + quote(account, safe='') + '/' + target
    parsed = urlsplit(url)
    jobs.require(parsed.scheme == 'https' and parsed.netloc == 'mail.google.com'
                 and not any(c in url for c in '\r\n()'), 'Gmail draft outcome unknown: invalid URL')
    # No subject, recipients or body in outward notifications or the change log.
    summary = ('Created reply draft' if reply_to else 'Created new email draft') + ' · [Open draft](' + url + ')'
    with db:
        db.execute("UPDATE drafts SET draft_id=?,url=?,state='created' WHERE change_id=?", (identity, url, cid))
        db.execute('UPDATE changes SET summary=? WHERE id=?', (summary, cid))
    from .messages import changes_text
    row = db.execute('SELECT * FROM changes WHERE id=?', (cid,)).fetchone()
    notify(db, 'Changes', changes_text([(row, summary)]), 'draft:' + cid,
           sender, now, actor, settings=settings, change_id=cid, related_changes=(cid,))
    return dict(change_id=cid, draft_id=identity, url=url, status='created')


def undo(db, identity, actor='owner', runner=run, now=None):
    now = now or jobs.now_utc()
    row = db.execute('SELECT * FROM drafts WHERE change_id=?', (identity,)).fetchone()
    jobs.require(row is not None and row['state'] == 'created', 'draft is not available for undo')
    with db:
        cid = jobs.change(db, 'undo draft', now, actor, identity)
        db.execute('UPDATE changes SET summary=? WHERE id=?', ('Delete Gmail draft (outcome pending)', cid))
    runner(command('delete', row['account']) + [row['draft_id'], '--force', '--no-input'])
    with db:
        db.execute("UPDATE drafts SET state='deleted' WHERE change_id=?", (identity,))
        db.execute('UPDATE changes SET summary=? WHERE id=?', ('Deleted Gmail draft', cid))
    return dict(change_id=cid, undo_of=identity, status='deleted')

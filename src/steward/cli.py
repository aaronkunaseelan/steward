"""Steward: validated job, run, notification and state verbs."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from . import __version__
from . import jobs, runtime, memory
from .notify import TOPICS, notify
from .state import StateError, WriterBusy, restore, writer


def in_unit(unit):
    # INVOCATION_ID is inherited from any service (e.g. an agent daemon); the cgroup names the unit.
    try:
        return Path('/proc/self/cgroup').read_text().strip().endswith('/' + unit)
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'steward {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    contract_parser = commands.add_parser('contract')
    contract_parser.add_argument('id', choices=['steward.job.v1', 'steward.job.v2', 'steward.record.v1', 'steward.transcript.v1', 'steward.run-summary.v1', 'steward.inbox.v1'])
    briefing_parser = commands.add_parser('briefing', help='pull archived wake sections')
    briefing_parser.add_argument('sections', nargs='*')
    briefing_parser.add_argument('--wake', type=int)
    briefing_parser.add_argument('--list', action='store_true')
    for name in ('external-change', 'moment', 'draft', 'page', 'interest', 'told', 'block', 'job', 'run', 'run-exec', 'notify', 'inbox', 'undo', 'state', 'intent', 'transcript', 'plan', 'tick', 'wake', 'init-state', 'backup', 'restore', 'journal', 'memory'):
        p = commands.add_parser(name)
        if name != 'transcript':
            p.add_argument('--state', type=Path, default=Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'steward')
            p.add_argument('--config', type=Path)
        if name in ('intent', 'transcript', 'inbox'):
            p.add_argument('--json', action='store_true', help='print the read-only view for mech-d Page 2')
        if name == 'external-change':
            p.add_argument('--subject', required=True)
            p.add_argument('--outcome', required=True)
        if name == 'moment':
            p.add_argument('action', choices=['set'])
            p.add_argument('name')
            p.add_argument('--start', required=True)
            p.add_argument('--by', dest='deadline', required=True)
        if name == 'transcript':
            p.add_argument('id')
        if name == 'told':
            p.add_argument('action', choices=['check'])
            p.add_argument('keys', nargs='+')
        if name == 'page':
            sub = p.add_subparsers(dest='action', required=True)
            for action in ('put', 'get', 'rm'):
                q = sub.add_parser(action)
                q.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                q.add_argument('--config', type=Path, default=argparse.SUPPRESS)
                if action == 'put':
                    q.add_argument('--title', required=True)
                    q.add_argument('--slug')
                    q.add_argument('--html', action='store_true')
                    q.add_argument('--items', help='comma-separated gsd ids; code writes the prep header')
                    q.add_argument('--block', help='demand whose applied block today supplies time and why')
                    q.add_argument('--public', action='store_true', help='allow access without a password (default: protected)')
                    q.add_argument('file')
                else:
                    q.add_argument('slug')
        if name == 'interest':
            sub = p.add_subparsers(dest='action', required=True)
            for action in ('set', 'rm'):
                q = sub.add_parser(action)
                q.add_argument('name')
                q.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                if action == 'set':
                    q.add_argument('--spec', type=Path)
        if name == 'block':
            sub = p.add_subparsers(dest='block_action', required=True)
            for action in ('demand', 'plan', 'choose', 'apply'):
                q = sub.add_parser(action)
                q.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                q.add_argument('--config', type=Path, default=argparse.SUPPRESS)
                if action == 'demand':
                    ds = q.add_subparsers(dest='action', required=True)
                    for verb in ('add', 'edit', 'rm'):
                        d = ds.add_parser(verb)
                        d.add_argument('name')
                        d.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                        d.add_argument('--config', type=Path, default=argparse.SUPPRESS)
                        if verb != 'rm':
                            d.add_argument('--spec', type=Path)
                elif action == 'choose':
                    q.add_argument('--spec', type=Path)
                elif action == 'apply':
                    q.add_argument('change_id')
        if name in ('job', 'run'):
            sub = p.add_subparsers(dest='action', required=True)
            for action in (('add', 'edit', 'pause', 'resume', 'rm') if name == 'job' else ('now', 'plan', 'cancel')):
                q = sub.add_parser(action)
                # Accept the state/config flags before or after the nested verb.
                q.add_argument('--owner-message', type=int, help='inbox update id requesting this job or run change')
                q.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                q.add_argument('--config', type=Path, default=argparse.SUPPRESS)
                q.add_argument('name')
                if name == 'job' and action in ('add', 'edit'):
                    q.add_argument('--spec', type=Path)
                if name == 'run':
                    q.add_argument('--reason', required=action != 'now', default='explicit run now')
                    if action == 'plan':
                        q.add_argument('--at', required=True)
        if name in ('tick', 'run-exec', 'intent', 'transcript'):
            p.add_argument('--data', type=Path)
        if name == 'tick':
            p.add_argument('action', nargs='?', choices=['skip'])
            p.add_argument('--from', dest='from_at')
            p.add_argument('--to', dest='to_at')
            p.add_argument('--reason')
        if name == 'run-exec':
            p.add_argument('run_id')
        if name == 'inbox':
            p.add_argument('action', choices=['done'], nargs='?')
            p.add_argument('ids', nargs='*', type=int)
            p.add_argument('--outstanding', action='store_true', help='read unhandled owner message ids and arrival times')
        if name == 'notify':
            p.add_argument('--topic', choices=TOPICS)
            p.add_argument('--reply-to', type=int, metavar='MESSAGE_ID')
            identity = p.add_mutually_exclusive_group()
            identity.add_argument('--told-key', action='append')
            identity.add_argument('--moment', metavar='NAME', help='address an owner moment in Today; code builds its daily key')
            p.add_argument('--deliver', choices=['digest', 'immediate', 'silent'])
            p.add_argument('--interrupt', metavar='REASON', help='request sound: why the owner must act before the next moment')
            p.add_argument('text')
        if name == 'draft':
            target = p.add_mutually_exclusive_group(required=True)
            target.add_argument('--reply-to')
            target.add_argument('--to', nargs='+')
            p.add_argument('--reply-all', action='store_true')
            p.add_argument('--body-file', required=True)
            p.add_argument('--subject')
        if name == 'undo':
            p.add_argument('change_id')
            p.add_argument('--owner-message', type=int, help='inbox update id requesting this undo')
        if name in ('backup', 'restore'):
            p.add_argument('location', type=Path)
        if name == 'journal':
            view = p.add_mutually_exclusive_group()
            view.add_argument('--wake', type=int)
            view.add_argument('--outcomes', action='store_true')
            p.add_argument('--week')
            p.add_argument('--json', action='store_true')
            p.add_argument('--since')
            p.add_argument('--until')
            p.add_argument('--day')
            p.add_argument('--full', action='store_true', help='return unclipped message and inbox text')
        if name == 'memory':
            sub = p.add_subparsers(dest='action', required=True)
            for action in ('write', 'show', 'due', 'recall'):
                q = sub.add_parser(action)
                q.add_argument('--state', type=Path, default=argparse.SUPPRESS)
                if action not in ('due', 'recall'):
                    q.add_argument('layer', choices=list(memory.CAPS), nargs=None if action == 'write' else '?')
                    q.add_argument('key', nargs='?')
                if action == 'write':
                    q.add_argument('--file', type=Path)
                if action == 'show':
                    q.add_argument('--week')
                    q.add_argument('--month')
    args = parser.parse_args(argv)
    try:
        if args.command == 'briefing':
            from .briefing import pull
            print(pull(runtime.data_root(), args.wake, args.sections, args.list), end='')
            return 0
        if args.command == 'contract':
            from .contracts import DIRECTORY
            suffix = '.md' if args.id in ('steward.job.v1', 'steward.job.v2') else '.json'
            print((DIRECTORY / (args.id + suffix)).read_text(), end='')
            return 0
        if args.command == 'transcript':
            from .transcripts import compact, document
            print(compact(document(runtime.data_root(args.data), args.id)))
            return 0
        if args.command == 'intent':
            from .intent import document
            settings = runtime.config(args.config)
            report = document(args.state, settings, data=args.data)
            print(json.dumps(report, sort_keys=True, allow_nan=False))
            return 0
        settings = runtime.config(args.config)
        from . import mind
        with mind.use(settings):
            actor = os.environ.get('STEWARD_ACTOR')
            if args.command == 'page' and args.action == 'get':
                from . import pages
                sys.stdout.buffer.write(pages.get(args.slug))
                return 0
            elif args.command == 'restore':
                jobs.require(actor in ('owner', 'model', 'maintenance'),
                             'write requires STEWARD_ACTOR=owner, model or maintenance')
                report = restore(args.location, args.state)
            elif args.command in ('tick', 'wake') and getattr(args, 'action', None) is None and not in_unit(f'steward-{args.command}.service'):
                # Senses and sends need the unit's EnvironmentFiles (keyring, bot token). Outside
                # the unit the calendars read fails, and the read after it looks like a change.
                raise StateError(f'run inside the unit: systemctl --user start steward-{args.command}.service')
            elif args.command == 'tick' and args.action is None:
                report = runtime.tick(args.state, args.data, settings=settings)
            elif args.command == 'wake':
                report = runtime.wake(args.state, settings=settings)
            elif args.command == 'run-exec':
                report = runtime.execute(args.state, args.run_id, args.data)
            elif args.command == 'inbox' and not (args.outstanding and args.action is None and not args.ids) and not (not args.outstanding and args.action == 'done' and args.ids):
                raise StateError('use inbox --outstanding or inbox done ID…')
            elif (args.command == 'inbox' and args.outstanding) or args.command in ('state', 'plan', 'told', 'journal') or (args.command == 'memory' and args.action != 'write') or (args.command == 'block' and args.block_action == 'plan'):
                # A read-only connection: state does not migrate or repair files.
                path = args.state / 'state.sqlite'
                db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                db.row_factory = sqlite3.Row
                try:
                    from .planner import choices
                    if args.command == 'inbox':
                        from .messages import outstanding
                        report = outstanding(db)
                    elif args.command == 'journal':
                        jobs.require(args.week is None or args.outcomes, '--week requires --outcomes')
                        if args.outcomes:
                            from .outcomes import weekly
                            report = weekly(db, args.week)
                        else:
                            report = memory.wake_journal(db, args.wake) if args.wake is not None else memory.journal(db, args.since, args.until, args.day, full=args.full)
                    elif args.command == 'memory':
                        report = memory.due(db) if args.action == 'due' else {'recall': memory.recall(db, jobs.now_utc())} if args.action == 'recall' else memory.show(db, args.layer, args.key, args.week, args.month)
                    elif args.command == 'told':
                        from .capabilities import told_check
                        report = told_check(db, args.keys)
                    elif args.command == 'block':
                        from .calendar_b import plan
                        report = plan(db, settings)
                    else:
                        report = choices(db, jobs.now_utc()) if args.command == 'plan' else dict(jobs.snapshot(db, interrupt_cap=settings.get('interrupt_cap', 3)), mind=mind.history(settings))
                finally:
                    db.close()
            else:
                jobs.require(actor in ('owner', 'model', 'maintenance'),
                             'write requires STEWARD_ACTOR=owner, model or maintenance')
                with writer(args.state, initialize=args.command == 'init-state', wait=0 if args.command == 'wake' else 60) as store:
                    db = store.db
                    if args.command == 'init-state':
                        report = {'status': 'initialized', 'schema': db.execute('PRAGMA user_version').fetchone()[0]}
                    elif args.command == 'external-change':
                        from .external_changes import record
                        report = record(db, args.subject, args.outcome, actor=actor)
                    elif args.command == 'moment':
                        from .timing import set_moment
                        report = set_moment(db, args.name, args.start, args.deadline, actor=actor)
                    elif args.command == 'tick':
                        from .tick_skips import skip
                        report = skip(db, args.from_at, args.to_at, args.reason, actor=actor)
                    elif args.command == 'memory':
                        report = memory.write(db, args.layer, args.key or ('lessons' if args.layer == 'lessons' else ''), args.file.read_text() if args.file else sys.stdin.read(), actor)
                    elif args.command == 'block':
                        from . import calendar_b
                        if args.block_action == 'demand':
                            spec = None if args.action == 'rm' else (json.loads(args.spec.read_text()) if args.spec else json.load(sys.stdin))
                            report = calendar_b.demand(db, args.action, args.name, spec, actor=actor)
                        elif args.block_action == 'choose':
                            proposal = json.loads(args.spec.read_text()) if args.spec else json.load(sys.stdin)
                            report = calendar_b.choose(db, settings, proposal, actor=actor)
                        else:
                            report = calendar_b.resume(db, settings, args.change_id)
                    elif args.command == 'draft':
                        from . import drafts
                        from .capabilities import require_draft_permit
                        require_draft_permit(db)
                        body = sys.stdin.read() if args.body_file == '-' else Path(args.body_file).read_text()
                        report = drafts.create(db, settings, body, args.reply_to, args.reply_all,
                                               args.to, args.subject, actor=actor)
                    elif args.command == 'page':
                        from . import pages
                        if args.action == 'put':
                            if args.file == '-':
                                data = sys.stdin.buffer.read(pages.LIMIT + 1)
                            else:
                                with Path(args.file).open('rb') as stream:
                                    data = stream.read(pages.LIMIT + 1)
                            report = pages.put(db, args.title, data, args.file, args.slug, args.html, actor=actor, public=args.public,
                                               items=args.items.split(',') if args.items is not None else None, block=args.block)
                        else:
                            report = pages.remove(db, args.slug, actor=actor)
                    elif args.command == 'interest':
                        from .capabilities import interest
                        spec = None if args.action == 'rm' else (json.loads(args.spec.read_text()) if args.spec else json.load(sys.stdin))
                        report = interest(db, args.action, args.name, spec, actor)
                    elif args.command == 'job':
                        spec = None
                        if args.action in ('add', 'edit'):
                            spec = json.loads(args.spec.read_text()) if args.spec else json.load(sys.stdin)
                        report = jobs.job_verb(db, args.action, args.name, spec, actor=actor, owner_message=args.owner_message)
                    elif args.command == 'run':
                        report = jobs.run_verb(db, args.action, args.name, args.reason, getattr(args, 'at', None), actor=actor, owner_message=args.owner_message)
                    elif args.command == 'undo':
                        from . import calendar_b
                        if db.execute('SELECT 1 FROM drafts WHERE change_id=?', (args.change_id,)).fetchone():
                            from . import drafts
                            report = drafts.undo(db, args.change_id, actor=actor)
                        elif db.execute('SELECT 1 FROM memories WHERE change_id=?', (args.change_id,)).fetchone():
                            report = memory.undo(db, args.change_id, actor)
                        elif db.execute('SELECT 1 FROM blocks WHERE change_id=? UNION SELECT 1 FROM demands WHERE change_id=?', (args.change_id, args.change_id)).fetchone():
                            report = calendar_b.undo(db, settings, args.change_id, actor=actor)
                        else:
                            report = jobs.undo(db, args.change_id, actor=actor, owner_message=args.owner_message)
                    elif args.command == 'inbox':
                        from .messages import done
                        report = done(db, args.ids, jobs.now_utc(), actor)
                    elif args.command == 'notify':
                        report = notify(db, args.topic, args.text, args.told_key, actor=actor, deliver=args.deliver, settings=settings, interrupt=args.interrupt, moment=args.moment, reply_to=args.reply_to)
                    elif args.command == 'backup':
                        report = store.backup(args.location)
    except WriterBusy:
        print(json.dumps({'status': 'writer_busy'}))
        return 3
    except StateError as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}), file=sys.stderr if args.command in ('intent', 'transcript', 'tick') else sys.stdout)
        return 2
    except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error, subprocess.SubprocessError) as exc:
        from .transcripts import redact
        # Subprocess strings include argv; OS/codec/key errors can include private inputs.
        if isinstance(exc, subprocess.SubprocessError):
            reason = 'external command failed'
        elif isinstance(exc, OSError):
            reason = exc.strerror or 'OS operation failed'
        elif isinstance(exc, UnicodeError):
            reason = 'invalid text encoding'
        elif isinstance(exc, KeyError):
            reason = 'missing required key'
        else:
            reason = str(exc)
        reason = redact(reason).splitlines()[0] if reason else 'operation failed'
        print(json.dumps({'status': 'rejected', 'error': f'{type(exc).__name__}: {reason[:200]}'}), file=sys.stderr if args.command in ('intent', 'transcript', 'tick') else sys.stdout)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0

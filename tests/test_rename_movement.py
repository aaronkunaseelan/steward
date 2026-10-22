from fixture_mind import use_settings
"""One-off vocabulary migration: all versions, no message-history changes."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
import unittest

from steward.state import writer
from steward import calendar_b, senses
from datetime import datetime, timezone

OLD = 'train' 'ing'  # Legacy input, deliberately absent from Steward's runtime vocabulary.
SCRIPT = Path(__file__).resolve().parents[1] / 'bin' / 'rename-movement'


class RenameMovementTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory(prefix='s16-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with writer(self.root, initialize=True) as store:
            db = store.db
            for version in (1, 2):
                db.execute('INSERT INTO demands VALUES(?,?,?,?,?)',
                           (OLD, version, 'c', json.dumps(dict(kind=OLD, title=OLD.title(),
                            justification='Why ' + OLD, training_readiness=80,
                            producer='strength_' + OLD)), 'active'))
                db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?)',
                           (OLD + ':week:0', version, 'c', OLD, 'applied',
                            json.dumps(dict(title=OLD.title(), event_key=OLD + ':week:0',
                                start='2026-10-21T09:00:00+03:00', end='2026-10-21T11:00:00+03:00',
                                updated='2026-10-19T00:00:00Z', event_id='same-id', transparency='transparent'))))
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',
                           ('day', '2026-10-19', version, 'c', OLD.title() + ' and ' + OLD.upper()))
                db.execute('INSERT INTO interests VALUES(?,?,?,?,?)',
                           (OLD, version, 'c', json.dumps(dict(text=OLD)), 'active'))
            db.execute('INSERT INTO changes VALUES(?,?,?,?,?,?)',
                       ('c', 'now', 'owner', 'add', 'Add ' + OLD, None))
            db.execute('INSERT INTO told VALUES(?,?,?,?,?,?)', (OLD, 'now', None, None, 'Today', 1))
            db.execute('INSERT INTO outbox(id,change_id,topic,kind,text,state) VALUES(?,?,?,?,?,?)',
                       ('o', 'c', 'Today', 'result', OLD, 'sent'))
            db.commit()
            db.execute('INSERT INTO senses(name,at,status,payload,change_id,event) VALUES(?,?,?,?,?,?)',
                       ('calendars', '2026-10-20T15:00:00Z', 'ok', '{}', 'c', 0))
            db.commit()
        self.path = self.root / 'state.sqlite'
        self.event = dict(id='same-id', summary=OLD.title(), updated='2026-10-19T00:00:00Z',
                          start={'dateTime': '2026-10-21T09:00:00+03:00'},
                          end={'dateTime': '2026-10-21T11:00:00+03:00'}, transparency='transparent',
                          extendedProperties=dict(private=dict(event_key=OLD + ':week:0', steward='1', other='keep')))
        self.calls = []

    def run_script(self, dry=False, runner=None, idle=None):
        module = runpy.run_path(str(SCRIPT))
        def gog(argv):
            self.calls.append(argv)
            if 'events' in argv:
                return json.dumps(dict(events=[self.event]))
            self.event['summary'] = argv[argv.index('--summary') + 1]
            self.event['extendedProperties']['private']['event_key'] = next(x.split('=', 1)[1] for x in argv if x.startswith('event_key='))
            self.event['updated'] = '2026-10-20T15:30:00Z'
            return json.dumps({'event': self.event})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = module['migrate'](self.path, 'schedule', dry_run=dry, runner=runner or gog,
                                       check_idle=idle or (lambda: None))
        return result, output.getvalue()

    def dump(self):
        with sqlite3.connect(self.path) as db:
            return '\n'.join(db.iterdump())

    def test_dry_run_read_only_and_all_versions(self):
        before = self.dump()
        result, output = self.run_script(dry=True)
        self.assertEqual(before, self.dump())
        self.assertEqual(result['rows'], dict(demands=2, blocks=2, memories=2, interests=2, changes=1))
        self.assertEqual(result['events'], 1)
        self.assertEqual(result['history'], dict(outbox=dict(total=1, matching=1), told=dict(total=1, matching=1)))
        self.assertEqual(len(self.calls), 1)
        self.assertIn('--readonly', self.calls[0])
        self.assertIn('--all-pages', self.calls[0])
        self.assertIn('rowid=', output)

    def test_patches_then_rewrites_same_event_and_repeat_is_noop(self):
        def runner(argv):
            if 'update' in argv:
                with sqlite3.connect(self.path) as db:
                    self.assertEqual(db.execute('SELECT DISTINCT name FROM demands').fetchall(), [(OLD,)])
            self.calls.append(argv)
            if 'events' in argv:
                return json.dumps(dict(events=[self.event]))
            self.event['summary'] = argv[argv.index('--summary') + 1]
            self.event['extendedProperties']['private']['event_key'] = next(x.split('=', 1)[1] for x in argv if x.startswith('event_key='))
            self.event['updated'] = '2026-10-20T15:30:00Z'
            return json.dumps({'event': self.event})
        result, _ = self.run_script(runner=runner)
        self.assertEqual(result['events'], 1)
        update = next(argv for argv in self.calls if 'update' in argv)
        self.assertEqual(update[update.index('update') + 1:update.index('update') + 3], ['schedule', 'same-id'])
        self.assertIn('event_key=movement:week:0', update)
        self.assertIn('other=keep', update)
        self.assertIn('--send-updates=none', update)
        with sqlite3.connect(self.path) as db:
            for table, column in [('demands', 'name'), ('blocks', 'event_key'), ('memories', 'content'), ('interests', 'name'), ('changes', 'summary')]:
                self.assertNotIn(OLD, str(db.execute(f'SELECT {column} FROM {table}').fetchall()).lower())
            spec = json.loads(db.execute('SELECT spec_json FROM demands LIMIT 1').fetchone()[0])
            self.assertEqual(spec['training_readiness'], 80)
            self.assertEqual(spec['producer'], 'strength_' + OLD)
            self.assertEqual(db.execute('SELECT text FROM outbox').fetchone()[0], OLD)
            self.assertEqual(db.execute('SELECT key FROM told').fetchone()[0], OLD)
        result, _ = self.run_script()
        self.assertFalse(any(result['rows'].values()))
        self.assertEqual(result['events'], 0)

    def test_collision_rolls_back_every_table(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO blocks VALUES('movement:week:0',1,'c','movement','applied','{}')")
        before = self.dump()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_script()
        self.assertEqual(before, self.dump())
        self.assertTrue(all('create' not in argv and 'delete' not in argv for argv in self.calls))

    def test_calendar_failure_compensates_and_store_is_untouched(self):
        before = self.dump()
        second = dict(self.event, id='second')
        calls = []
        def fail(argv):
            calls.append(argv)
            if 'events' in argv:
                return json.dumps(dict(events=[self.event, second]))
            if 'second' in argv and 'event_key=movement:week:0' in argv:
                raise RuntimeError('provider unavailable')
            return json.dumps(dict(event=dict(self.event, updated='2026-10-20T15:30:00Z')))
        with self.assertRaises(RuntimeError):
            self.run_script(runner=fail)
        self.assertEqual(before, self.dump())
        self.assertIn('event_key=' + OLD + ':week:0', calls[-1])
        self.assertEqual(calls[-1][calls[-1].index('--summary') + 1], OLD.title())

    def test_patched_timestamp_does_not_pin_fresh_projection(self):
        self.run_script()
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            observation = dict(events=[dict(self.event, calendar='schedule',
                               event_key='movement:week:0')],
                               **{'from': '2026-10-19T00:00:00Z', 'to': '2026-10-26T00:00:00Z'})
            rows, _ = calendar_b.projected(db, observation, {'schedule_calendar_id': 'schedule', 'main_calendar_ids': ['main']})
            self.assertEqual(rows[0]['state'], 'applied')
            self.assertEqual(json.loads(rows[0]['payload'])['updated'], self.event['updated'])
            now = datetime(2026, 10, 20, 15, 31, tzinfo=timezone.utc)
            cached = senses.snapshot(db, now)['calendars']
            self.assertEqual(cached['status'], 'ok')
            self.assertEqual(cached['at'], '2026-10-20T15:00:00Z')

    def test_calendar_observations_are_untouched(self):
        with sqlite3.connect(self.path) as db:
            for number in range(95):
                db.execute('INSERT INTO senses(name,at,status,payload,change_id,event) VALUES(?,?,?,?,?,?)',
                           ('calendars', '2026-10-20T15:00:00Z', 'ok', json.dumps(dict(sequence=number)), 'c', 0))
            before = db.execute('SELECT * FROM senses ORDER BY id').fetchall()
        result, _ = self.run_script()
        with sqlite3.connect(self.path) as db:
            after = db.execute('SELECT * FROM senses ORDER BY id').fetchall()
        self.assertEqual(after, before)
        self.assertEqual(result['calendar_senses'], 0)

    def test_busy_service_refuses_before_any_effect(self):
        before = self.dump()
        def busy():
            raise RuntimeError('tick active')
        with self.assertRaises(RuntimeError):
            self.run_script(idle=busy)
        self.assertEqual(before, self.dump())
        self.assertEqual(self.calls, [])

    def test_legacy_pins_and_deletion_barriers_are_retired(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE blocks SET state='pinned'")
            db.execute("INSERT INTO blocks VALUES('deleted:once:0',1,'c','old','tombstone','{}')")
        result, _ = self.run_script()
        self.assertEqual(result['unpinned'], 2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT DISTINCT state FROM blocks WHERE event_key='movement:week:0'").fetchall(), [('applied',)])
            self.assertEqual(db.execute("SELECT state FROM blocks WHERE event_key='deleted:once:0'").fetchone()[0], 'removed')

    def test_listing_failure_changes_nothing(self):
        before = self.dump()
        def fail(argv):
            raise RuntimeError('read failed')
        with self.assertRaises(RuntimeError):
            self.run_script(runner=fail)
        self.assertEqual(before, self.dump())

import contextlib
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import SOURCE
from steward import memory, jobs, cli
from steward.state import writer, StateError


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        self.ctx = writer(self.root, initialize=True)
        self.store = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.db = self.store.db
        self.now = datetime.fromisoformat('2027-02-02T12:00:00+02:00')
        self.files = self.root / 'memory'

    def file(self, end='2027-02-02T12:00:00+02:00'):
        memory.file_days(self.db, datetime.fromisoformat(end))

    def test_caps_bytes_and_periods(self):
        self.file()
        for layer, key, cap in [('day', '2027-02-01', 2048), ('week', '2027-01-24', 3072),
                                ('month', '2027-01', 4096), ('lessons', 'lessons', 6144),
                                ('synthesis', 'current', 2048)]:
            with self.subTest(layer=layer):
                memory.write(self.db, layer, key, 'x' * cap, now=self.now)
                self.assertEqual(memory.show(self.db, layer, key)[0]['content'], 'x' * cap)
                with self.assertRaisesRegex(StateError, f'^{layer} memory is {cap + 2} bytes; cap is {cap}$'):
                    memory.write(self.db, layer, key, 'é' * (cap // 2 + 1), now=self.now)
        for layer,key in [('day','2027-02-02'),('week','2027-01-31'),('month','2027-02'),('day','2026-10-15')]:
            with self.assertRaises(StateError):
                memory.write(self.db,layer,key,'no',now=self.now)

    def test_write_summary_names_memory_without_content_or_paths(self):
        self.file()
        for layer, key, label in [
            ('day', '2027-02-01', 'day memory Mon 01 Feb 2027'),
            ('week', '2027-01-24', 'week memory 2027-01-24'),
            ('month', '2027-01', 'month memory Jan 2027'),
            ('lessons', 'lessons', 'lessons'),
            ('synthesis', 'current', 'synthesis'),
        ]:
            with self.subTest(layer=layer):
                result = memory.write(self.db, layer, key, 'private content', now=self.now)
                summary = self.db.execute('SELECT summary FROM changes WHERE id=?',
                                          (result['change_id'],)).fetchone()[0]
                self.assertEqual(summary, 'Memory write: ' + label)

    def test_filing_immutable_and_crash_recovery(self):
        self.file('2026-10-17T00:00:00+03:00')
        path=self.files/'journal/2026-10-16.json'
        before=path.read_bytes()
        with self.db:
            jobs.change(self.db,'later',datetime.fromisoformat('2026-10-16T12:00:00+03:00'))
            self.db.execute('DELETE FROM journal_files')
        self.file('2026-10-17T00:00:00+03:00')
        self.assertEqual(before,path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(memory.filed(self.db),{'2026-10-16'})
        path.unlink()
        self.assertEqual(memory.filed(self.db),set())

    def test_due_boundaries_and_missing_record(self):
        self.file('2026-12-01T00:00:00+02:00')
        due=memory.due(self.db,self.now)
        pairs={(r['layer'],r['key']) for r in due}
        self.assertIn(('week','2026-11-01'),pairs)  # Nov 1 through Nov 7
        self.assertIn(('month','2026-11'),pairs)
        self.assertIn(('month','2026-10'),pairs)  # A partial launch month still closes.
        self.assertNotIn(('week','2026-10-11'),pairs)
        expected = [(date(2026, 10, 17) + timedelta(days=i), 0, 'day',
                     (date(2026, 10, 16) + timedelta(days=i)).isoformat()) for i in range(46)]
        expected += [(date.fromisoformat(key) + timedelta(days=7), 1, 'week', key)
                     for key in ('2026-10-18', '2026-10-25', '2026-11-01', '2026-11-08',
                                 '2026-11-15', '2026-11-22')]
        expected += [(date(2026, 11, 1), 3, 'month', '2026-10'),
                     (date(2026, 11, 29), 2, 'lessons', 'lessons'),
                     (date(2026, 11, 29), 3, 'month', '2026-11')]
        self.assertEqual([(r['layer'], r['key']) for r in due],
                         [(layer, key) for _, _, layer, key in sorted(expected)])
        (self.files/'journal/2026-11-01.json').unlink()
        pairs={(r['layer'],r['key']) for r in memory.due(self.db,self.now)}
        self.assertIn(('month','2026-11'),pairs)  # A gap below never blocks the month.
        self.assertNotIn(('week','2026-11-01'),pairs)

    def test_sunday_week_due_after_saturday_filing_across_dst(self):
        from steward.settings import local_zone
        key = '2026-10-18'
        expected = [f'2026-10-{day}' for day in range(18, 25)]
        def weeks(at):
            return [r for r in memory.due(self.db, datetime.fromisoformat(at)) if r['layer'] == 'week']
        self.file('2026-10-24T23:59:59+03:00')
        self.assertEqual(weeks('2026-10-24T23:59:59+03:00'), [])
        self.assertEqual(weeks('2026-10-25T00:00:00+03:00'), [])  # Saturday not filed yet.
        self.file('2026-10-25T00:00:00+03:00')
        for at in ['2026-10-24T21:00:00+00:00', '2026-10-25T03:30:00+03:00',
                   '2026-10-25T03:30:00+02:00', '2026-10-25T06:00:00+02:00']:
            with self.subTest(at=at):
                self.assertEqual(weeks(at), [dict(layer='week', key=key, day_memories=expected)])
        for at in ['2026-10-24T23:59:59+03:00', '2026-10-25T03:30:00+03:00',
                   '2026-10-25T03:30:00+02:00']:
            day = datetime.fromisoformat(at).astimezone(local_zone()).date()
            self.assertEqual(memory.week_start(day).isoformat(), key if day.day == 24 else '2026-10-25')
        # Week keys remain dates across year boundaries, not ISO year/week pairs.
        self.assertEqual(memory.week_start(date(2027, 1, 1)), date(2026, 12, 27))
        for invalid in ['2026-W43', '2026-10-19', '20261018']:
            with self.subTest(invalid=invalid), self.assertRaises((StateError, ValueError)):
                memory.period('week', invalid)
        for day in expected:
            memory.write(self.db, 'day', day, day, now=self.now)
        memory.write(self.db, 'day', '2026-10-17', 'outside', now=self.now)
        self.assertEqual([r['key'] for r in memory.show(self.db, 'day', week=key)], expected)
        memory.write(self.db, 'week', key, 'first week', now=self.now)
        self.assertEqual(memory.show(self.db, 'week', key)[0]['content'], 'first week')
        self.assertEqual(weeks('2026-10-25T06:00:00+02:00'), [])

    def test_nested_month_boundaries_and_dst(self):
        from steward.settings import local_zone
        self.assertEqual(memory.period('month', '2026-10'),
                         (date(2026, 9, 27), date(2026, 11, 1)))
        self.assertEqual(memory.period('month', '2026-11'),
                         (date(2026, 11, 1), date(2026, 11, 29)))
        for day, key in [('2026-09-27', '2026-10'), ('2026-10-31', '2026-10'),
                         ('2026-11-01', '2026-11'), ('2026-11-28', '2026-11'),
                         ('2026-11-29', '2026-12')]:
            self.assertEqual(memory.month_key(date.fromisoformat(day)), key)
        at = datetime(2026, 10, 25, 4, tzinfo=local_zone())
        self.assertEqual(at.utcoffset().total_seconds(), 7200)
        self.assertEqual(memory.local_day(at).isoformat(), '2026-10-25')
        self.assertEqual(memory.week_start(memory.local_day(at)), date(2026, 10, 25))

    def test_month_due_on_last_saturday_filing_not_week_success(self):
        self.file('2026-10-31T23:59:59+02:00')
        self.assertFalse(any(r['layer'] == 'month' for r in memory.due(self.db, self.now)))
        self.file('2026-11-01T00:00:00+02:00')
        self.assertIn(dict(layer='month', key='2026-10'), memory.due(self.db, self.now))
        # No week memories exist, and even a missing day journal is fail-soft.
        (self.files / 'journal/2026-10-25.json').unlink()
        memory.write(self.db, 'month', '2026-10', 'partial month; missing week', now=self.now)
        self.assertEqual(memory.show(self.db, 'month')[0]['key'], '2026-10')
        self.assertFalse(any(r['layer'] == 'month' for r in memory.due(self.db, self.now)))

    def test_month_night_due_order_and_independent_lessons_clock(self):
        before = datetime.fromisoformat('2026-10-31T23:59:59+02:00')
        night = datetime.fromisoformat('2026-11-01T04:00:00+02:00')
        self.file(before.isoformat())
        memory.write(self.db, 'lessons', 'lessons', 'early review', now=before)
        self.assertFalse(any(r['layer'] in ('lessons', 'month') for r in memory.due(self.db, before)))
        self.file(night.isoformat())
        expected = [dict(layer='day', key='2026-10-31'),
                    dict(layer='week', key='2026-10-25', day_memories=[f'2026-10-{d}' for d in range(25, 32)]),
                    dict(layer='lessons', key='lessons', month='2026-10'),
                    dict(layer='month', key='2026-10')]
        self.assertEqual(memory.due(self.db, night)[-4:], expected)
        # A failed week write cannot block either monthly step.
        with self.assertRaises(StateError):
            memory.write(self.db, 'week', '2026-10-25', 'x' * (memory.CAPS['week'] + 1), now=night)
        self.assertEqual(memory.due(self.db, night)[-4:], expected)
        # The month may even be written first; lessons remain independently due.
        memory.write(self.db, 'month', '2026-10', 'missing week; lessons not reviewed', now=night)
        self.assertIn(expected[2], memory.due(self.db, night))
        memory.write(self.db, 'lessons', 'lessons', 'month review', now=night)
        self.assertFalse(any(r['layer'] in ('month', 'lessons') for r in memory.due(self.db, night)))
        # November closes early, on Sat 28, not at calendar-month end.
        self.file('2026-11-28T23:59:59+02:00')
        self.assertFalse(any(r['layer'] == 'lessons' for r in memory.due(self.db, datetime.fromisoformat('2026-11-28T23:59:59+02:00'))))
        self.file('2026-11-29T04:00:00+02:00')
        next_night = datetime.fromisoformat('2026-11-29T04:00:00+02:00')
        self.assertEqual(memory.due(self.db, next_night)[-2:],
                         [dict(layer='lessons', key='lessons', month='2026-11'), dict(layer='month', key='2026-11')])
        # Successful lessons do not require any successful month memory.
        memory.write(self.db, 'lessons', 'lessons', 'November review', now=next_night)
        self.assertEqual(memory.due(self.db, next_night)[-1], dict(layer='month', key='2026-11'))

    def test_cli_sunday_week_write_show_and_filter(self):
        self.file('2026-10-25T00:00:00+03:00')
        # CLI opens its own writer.
        self.ctx.__exit__(None, None, None)
        self._cleanups.pop()
        for args, content in [
            (['memory', 'write', 'day', '2026-10-24'], 'Saturday'),
            (['memory', 'write', 'week', '2026-10-18'], 'First Sunday week'),
            (['memory', 'show', 'week', '2026-10-18'], None),
            (['memory', 'show', 'day', '--week', '2026-10-18'], None),
        ]:
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(content or '')), patch.object(jobs, 'now_utc', return_value=self.now), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main([*args, '--state', str(self.root)]), 0)
            if args[1] == 'show':
                rows = json.loads(output.getvalue())
                self.assertEqual(rows[0]['key'], '2026-10-18' if args[2] == 'week' else '2026-10-24')

    def test_undo_and_mirror(self):
        first=memory.write(self.db,'lessons','lessons','one',now=self.now)
        second=memory.write(self.db,'lessons','lessons','two',now=self.now)
        memory.undo(self.db,second['change_id'])
        self.assertEqual(memory.show(self.db)[0]['content'],'one')
        third=memory.latest(self.db)[0]
        memory.undo(self.db,third['change_id'])
        self.assertEqual(memory.show(self.db)[0]['content'],'two')
        self.assertEqual((self.files/'lessons/lessons.md').read_text(),'two')

    def test_first_write_undo_is_due_again(self):
        self.file('2026-10-17T00:00:00+03:00')
        result=memory.write(self.db,'day','2026-10-16','day',now=self.now)
        memory.undo(self.db,result['change_id'])
        self.assertEqual(memory.due(self.db,self.now),[dict(layer='day',key='2026-10-16')])
        self.assertFalse((self.files/'day/2026-10-16.md').exists())

    def test_recall_and_state_truncation(self):
        # Use fixture rows to cover months before launch without manufacturing filed history.
        for layer,key in [('lessons','lessons'),('day','2027-01-26'),('day','2027-02-01'),('day','2027-01-25'),('week','2027-01-17'),('week','2026-12-27'),('week','2027-01-24'),('month','2026-11'),('month','2026-07'),('month','2026-06'),('synthesis','current')]:
            with self.db:
                cid=jobs.change(self.db,'fixture',self.now)
                self.db.execute('INSERT INTO memories VALUES (?,?,?,?,?)',(layer,key,1,cid,'REMEMBER '+key))
        # The fading view is for consolidation and tool calls.
        recalled=memory.recall(self.db,self.now)
        for key in ['lessons','2027-01-26','2027-02-01','2027-01-17','2026-12-27','2026-11','2026-07']:
            self.assertIn('REMEMBER '+key,recalled)
        for key in ['2027-01-25','2027-01-24','2026-06']:
            self.assertNotIn('REMEMBER '+key,recalled)
        # A wake carries only the synthesis and an index of what is on file.
        text=memory.briefing(self.db,self.now,{'huge':'x'*200000})
        self.assertIn('REMEMBER current',text)
        self.assertNotIn('REMEMBER 2027-02-01',text)
        self.assertNotIn('REMEMBER lessons',text)
        self.assertIn('- day: 2027-01-25, 2027-01-26, 2027-02-01',text)
        self.assertNotIn('"huge"',text)
        self.assertNotIn('truncated',text)
        self.assertLessEqual(len(text.encode()),memory.BRIEFING_BYTES)
        self.assertNotIn('SYNTHETIC_SOUL_MARKER', text)
        self.assertNotIn('PROMPT', text)
        self.assertLess(text.index('## What I carry'), text.index('## Now ['))
        with self.assertRaisesRegex(StateError, '^synthesis memory is 2049 bytes; cap is 2048$'):
            memory.write(self.db,'synthesis','current','x'*2049)
        memory.write(self.db,'synthesis','current','now')  # not period-bound

    def test_realistic_briefing_keeps_decision_inputs(self):
        # A page selects decision inputs, not arbitrary large bookkeeping fields.
        state = dict(inbox=['owner instruction'], moments=['orientation'],
                     senses={'qmd': {'status': 'ok', 'data': {
                         'alive': True, 'sources': {}, 'feed': {'seen': 'x'*38000}}}},
                     interests='watcher evidence'*350, noise='x'*200000)
        receipt = {}
        text = memory.briefing(self.db, self.now, state, receipt=receipt)
        self.assertIn('owner instruction', text)
        self.assertIn('orientation', text)
        self.assertNotIn('watcher evidence', text)
        self.assertNotIn('seen', text)
        self.assertEqual(receipt['dropped_keys'], [])
        self.assertTrue(receipt['complete'])
        self.assertLessEqual(len(text.encode()), memory.BRIEFING_BYTES)

    def test_provocation_persisted_from_reference(self):
        self.file('2026-10-26T00:00:00+02:00')
        reference = Path(self.tmp.name) / 'mind/reference/provocations.md'
        reference.write_text('Provocations (code draws one per week memory):\n99. Reference-only question')
        draw = memory.provocations(self.db, self.now)
        reference.write_text('Provocations (code draws one per week memory):\n100. A different question')
        again = memory.provocations(self.db, self.now)
        self.assertEqual([(r['week'], r['number'], r['text']) for r in draw],
                         [('2026-10-18', 99, 'Reference-only question')])
        self.assertEqual(again, draw)

    def test_journal_bounds_and_owner_signals(self):
        with self.db:
            cid=jobs.change(self.db,'fixture',self.now)
            self.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)',('event',1,cid,'demand','applied','{}'))
            self.db.execute('INSERT INTO inbox VALUES (?,?,?,?,?,?,?,?)',(1,2,6,self.now.isoformat(),'owner instruction',None,0,'[]'))
            for i in range(80):
                self.db.execute('INSERT INTO inbox VALUES (?,?,?,?,?,?,?,?)',(i+2,i+3,6,self.now.isoformat(),'x'*4000,None,0,'[]'))
        result=memory.journal(self.db,day='2027-02-02')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(json.dumps(result).encode()),256*1024)
        self.assertEqual(result['calendar_b'][0]['state'],'applied')
        self.assertEqual(result['inbox'][0]['text'],'owner instruction')

    def test_journal_full_cli_and_filed_day(self):
        text = 'long message é ' * 40
        with self.db:
            cid = jobs.change(self.db, 'fixture', self.now)
            self.db.execute("INSERT INTO outbox(id,change_id,topic,text,state,message_id,sent_at) VALUES (?,?,?,?,?,?,?)",
                            ('message', cid, 'Today', text, 'sent', 1, self.now.isoformat()))
            self.db.execute('INSERT INTO inbox VALUES (?,?,?,?,?,?,?,?)',
                            (1, 2, 6, self.now.isoformat(), text, None, 0, '[]'))
        def read(*options):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['journal', '--state', str(self.root), *options]), 0)
            return output.getvalue()
        day = ['--day', '2027-02-02']
        default = read(*day)
        self.assertEqual(json.loads(default)['messages'][0]['text'], text[:300])
        self.assertTrue(json.loads(default)['messages'][0]['excerpt'])
        self.assertTrue(json.loads(default)['truncated'])
        for options in [day, ['--since', '2027-02-02T00:00:00+02:00', '--until', '2027-02-03T00:00:00+02:00']]:
            full = json.loads(read(*options, '--full'))
            self.assertEqual(full['messages'][0]['text'], text)
            self.assertEqual(full['inbox'][0]['text'], text)
        memory.file_days(self.db, datetime.fromisoformat('2027-02-03T00:00:00+02:00'))
        archived = json.loads(read(*day))
        self.assertEqual(archived['messages'][0]['text'], text)
        self.assertFalse(archived['messages'][0]['excerpt'])
        self.assertFalse(archived['truncated'])
        full = json.loads(read(*day, '--full'))
        self.assertEqual(full['messages'][0]['text'], text)
        self.assertEqual(full['inbox'][0]['text'], text)

    def test_unsupported_memory_commands_are_rejected(self):
        before = (self.root / 'state.sqlite').read_bytes()
        for verb in ['memory-range', 'memory-commit']:
            error = io.StringIO()
            with self.subTest(verb=verb), contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                cli.main([verb])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('invalid choice', error.getvalue())
            self.assertIn(verb, error.getvalue())
        self.assertEqual((self.root / 'state.sqlite').read_bytes(), before)

    def test_experiments_disappear_when_closed(self):
        with self.db:
            cid=jobs.change(self.db,'experiment: hypothesis; measure; ends 2027-02-08',self.now)
        self.assertEqual(memory.experiments(self.db)[0]['ends'],'2027-02-08')
        with self.db:
            jobs.change(self.db,'experiment closed: '+cid,self.now)
        self.assertEqual(memory.experiments(self.db),[])

    def test_experiment_discovery_cost_is_independent_of_ordinary_history(self):
        with self.db:
            self.db.executemany('INSERT INTO changes(id,at,actor,verb,summary) VALUES (?,?,?,?,?)',
                [(f'{n:032x}', self.now.isoformat(), 'tick', 'tick', 'tick') for n in range(4000)])
        queries = []
        self.db.set_trace_callback(queries.append)
        try:
            self.assertEqual(memory.experiments(self.db), [])
        finally:
            self.db.set_trace_callback(None)
        self.assertLessEqual(len(queries), 4)

    def test_experiments_in_run_reasons_close_or_undo_once(self):
        with self.db:
            cid = jobs.change(self.db, 'run plan', self.now)
            jobs.append(self.db, 'runs', dict(run_id='experiment', job='fixture', state='planned',
                planned_for=self.now.isoformat(), reason='experiment: try mornings; ends 2027-02-08',
                spec_json='{}'), cid)
        rows = memory.experiments(self.db)
        self.assertEqual([(r['id'], r['ends']) for r in rows], [(cid, '2027-02-08')])
        with self.db:
            closed = jobs.change(self.db, 'run plan', self.now)
            jobs.append(self.db, 'runs', dict(run_id='closure', job='fixture', state='planned',
                planned_for=self.now.isoformat(), reason='experiment closed: ' + cid, spec_json='{}'), closed)
        self.assertEqual(memory.experiments(self.db), [])
        with self.db:
            other = jobs.change(self.db, 'experiment: another test', self.now)
            jobs.change(self.db, 'undo', self.now, undo_of=other)
        self.assertEqual(memory.experiments(self.db), [])

    def test_capability_context_packet_without_harness_or_effects(self):
        import os
        import runpy
        from steward import capabilities, runtime
        self.file('2026-10-26T00:00:00+02:00')
        with self.db:
            cid=jobs.change(self.db,'fixture',self.now)
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',('consolidate',1,cid,json.dumps({'capability':'consolidate','justification':'learn from experience','deliver':'silent','model':'opus'}),'active'))
            jobs.append(self.db, 'runs', dict(run_id='fixture', job='consolidate', state='started',
                planned_for=self.now.isoformat(), started_at=self.now.isoformat(), reason='fixture',
                spec_json=jobs.get(self.db, 'jobs', 'consolidate')['spec_json']), cid)
        # agent-run takes the writer lock to persist its draw; release this fixture lock.
        self.ctx.__exit__(None,None,None)
        self._cleanups.pop()
        directory=Path(self.tmp.name)/'run'
        with patch.dict(os.environ, STEWARD_RUN_DIR=str(directory), STEWARD_STATE=str(self.root), STEWARD_JOB='consolidate', STEWARD_RUN_ID='fixture'), patch('sys.argv',['agent-run','consolidate']), patch.object(runtime,'harness',return_value='fixture-session'), patch.object(jobs,'now_utc',return_value=self.now), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(SOURCE/'jobs/agent-run'),run_name='__main__')
        packet=(directory/'packet.md').read_text()
        self.assertIn('memory_due',packet)
        self.assertIn('day_memories',packet)
        self.assertIn('provocation',packet)
        self.assertIn('SYNTHETIC_CONSOLIDATE_TASK', packet)
        self.assertNotIn('SYNTHETIC_SOUL_MARKER', packet)
        self.assertNotIn('SYNTHETIC_CHARTER_MARKER', packet)
        self.assertIn("~/.local/share/steward/mind/reference/memory.md",packet)
        self.assertIn('memory show|due|recall|write',packet)
        self.assertIn('steward memory show synthesis current (home and notable whereabouts)', packet)
        self.assertEqual(capabilities.load('consolidate')[0]['context'],['memory_due','provocation'])

    def test_cli_memory_roundtrip_and_read_only_queries(self):
        root=Path(self.tmp.name)/'other'
        with writer(root,initialize=True):
            pass
        output=io.StringIO()
        with patch('sys.stdin',io.StringIO('a fixture lesson')),contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['memory','write','lessons','--state',str(root)]),0)
        identity=json.loads(output.getvalue())['change_id']
        path = root / 'state.sqlite'
        before = path.read_bytes()
        queries = [['memory', 'show', 'lessons'], ['memory', 'due'],
                   ['journal', '--day', '2026-10-16']]
        reports = []
        with patch('steward.cli.writer', side_effect=AssertionError('read-only query')):
            for args in queries:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main([*args, '--state', str(root)]), 0)
                reports.append(json.loads(output.getvalue()))
        self.assertEqual(reports[0][0]['content'], 'a fixture lesson')
        self.assertEqual(reports[1], [])
        self.assertEqual(reports[2]['messages'], [])
        self.assertEqual(path.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['undo', identity, '--state', str(root)]), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['memory', 'show', 'lessons', '--state', str(root)]), 0)
        self.assertEqual(json.loads(output.getvalue()), [])

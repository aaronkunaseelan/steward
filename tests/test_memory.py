import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
        for layer,key in [('day','2027-02-01'),('week','2027-W04'),('month','2027-01'),('lessons','lessons')]:
            memory.write(self.db,layer,key,'x'*memory.CAPS[layer],now=self.now)
            with self.assertRaises(StateError):
                memory.write(self.db,layer,key,'é'*(memory.CAPS[layer]//2+1),now=self.now)
        for layer,key in [('day','2027-02-02'),('week','2027-W05'),('month','2027-02'),('day','2026-10-15')]:
            with self.assertRaises(StateError):
                memory.write(self.db,layer,key,'no',now=self.now)

    def test_write_summary_names_memory_without_content_or_paths(self):
        self.file()
        for layer, key, label in [
            ('day', '2027-02-01', 'day memory Mon 01 Feb 2027'),
            ('week', '2027-W04', 'week memory 2027-W04'),
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
        self.assertIn(('week','2026-W44'),pairs)  # Oct 26 through Nov 1
        self.assertIn(('month','2026-11'),pairs)
        self.assertNotIn(('month','2026-10'),pairs)
        self.assertNotIn(('week','2026-W42'),pairs)
        self.assertEqual([memory.period(r['layer'],r['key'])[1] for r in due],sorted(memory.period(r['layer'],r['key'])[1] for r in due))
        (self.files/'journal/2026-11-01.json').unlink()
        pairs={(r['layer'],r['key']) for r in memory.due(self.db,self.now)}
        self.assertNotIn(('month','2026-11'),pairs)
        self.assertNotIn(('week','2026-W44'),pairs)

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
        for layer,key in [('lessons','lessons'),('day','2027-01-26'),('day','2027-02-01'),('day','2027-01-25'),('week','2027-W03'),('week','2026-W53'),('week','2027-W04'),('month','2026-11'),('month','2026-06'),('month','2026-05'),('synthesis','current')]:
            with self.db:
                cid=jobs.change(self.db,'fixture',self.now)
                self.db.execute('INSERT INTO memories VALUES (?,?,?,?,?)',(layer,key,1,cid,'REMEMBER '+key))
        # The fading view is for consolidation and tool calls.
        recalled=memory.recall(self.db,self.now)
        for key in ['lessons','2027-01-26','2027-02-01','2027-W03','2026-W53','2026-11','2026-06']:
            self.assertIn('REMEMBER '+key,recalled)
        for key in ['2027-01-25','2027-W04','2026-05']:
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
        self.assertNotIn("# Steward's soul", text)
        self.assertNotIn('PROMPT', text)
        self.assertLess(text.index('## What I carry'), text.index('## Now ['))
        with self.assertRaises(Exception):
            memory.write(self.db,'synthesis','current','x'*2049)
        memory.write(self.db,'synthesis','current','now')  # not period-bound

    def test_realistic_briefing_keeps_decision_inputs(self):
        # A page selects decision inputs, not arbitrary large bookkeeping fields.
        state = dict(inbox=['owner instruction'], rituals=['orientation'],
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

    def test_provocation_persisted_from_soul(self):
        self.file('2026-10-26T00:00:00+02:00')
        with patch('steward.memory.secrets.choice',side_effect=lambda values:values[-1]) as choice:
            draw=memory.provocations(self.db,self.now)
            again=memory.provocations(self.db,self.now)
        self.assertEqual(draw,again)
        self.assertEqual(choice.call_count,1)
        self.assertEqual(draw[0]['week'],'2026-W43')
        self.assertIn(f"{draw[0]['number']}. {draw[0]['text']}",memory.mind.read('soul.md'))

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
            self.db.execute("INSERT INTO outbox(id,change_id,topic,kind,text,state,message_id,sent_at) VALUES (?,?,?,?,?,?,?,?)",
                            ('message', cid, 'Today', 'result', text, 'sent', 1, self.now.isoformat()))
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
        for options in [day, ['--since', '2027-02-02T00:00:00+02:00', '--until', '2027-02-03T00:00:00+02:00']]:
            full = json.loads(read(*options, '--full'))
            self.assertEqual(full['messages'][0]['text'], text)
            self.assertEqual(full['inbox'][0]['text'], text)
        memory.file_days(self.db, datetime.fromisoformat('2027-02-03T00:00:00+02:00'))
        self.assertEqual(read(*day), default)
        full = json.loads(read(*day, '--full'))
        self.assertEqual(full['messages'][0]['text'], text)
        self.assertEqual(full['inbox'][0]['text'], text)

    def test_legacy_verbs_gone(self):
        for verb in ['memory-range','memory-commit']:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main([verb])
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone())

    def test_experiments_closed_or_undone(self):
        with self.db:
            cid=jobs.change(self.db,'experiment: hypothesis; measure; ends 2027-02-08',self.now)
        self.assertEqual(memory.experiments(self.db)[0]['ends'],'2027-02-08')
        with self.db:
            jobs.change(self.db,'experiment closed: '+cid,self.now)
        self.assertEqual(memory.experiments(self.db),[])

    def test_capability_context_packet_without_harness_or_effects(self):
        import os
        import runpy
        from steward import capabilities, runtime
        self.file('2026-10-26T00:00:00+02:00')
        with self.db:
            cid=jobs.change(self.db,'fixture',self.now)
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',('consolidate',1,cid,json.dumps({'capability':'consolidate','justification':'learn from experience','deliver':'silent','model':'opus'}),'active'))
        # agent-run takes the writer lock to persist its draw; release this fixture lock.
        self.ctx.__exit__(None,None,None)
        self._cleanups.pop()
        directory=Path(self.tmp.name)/'run'
        with patch.dict(os.environ, STEWARD_RUN_DIR=str(directory), STEWARD_STATE=str(self.root), STEWARD_JOB='consolidate'), patch('sys.argv',['agent-run','consolidate']), patch.object(runtime,'harness',return_value='fixture-session'), patch.object(jobs,'now_utc',return_value=self.now), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(memory.SOURCE/'jobs/agent-run'),run_name='__main__')
        packet=(directory/'packet.md').read_text()
        self.assertIn('memory_due',packet)
        self.assertIn('day_memories',packet)
        self.assertIn('provocation',packet)
        self.assertIn("# Steward's soul",packet)
        self.assertIn('memory show|due|recall|write',packet)
        self.assertEqual(capabilities.load('consolidate')[0]['context'],['memory_due','provocation'])

    def test_cli_memory_roundtrip_and_read_only_queries(self):
        root=Path(self.tmp.name)/'other'
        with writer(root,initialize=True):
            pass
        output=io.StringIO()
        with patch('sys.stdin',io.StringIO('a fixture lesson')),contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['memory','write','lessons','--state',str(root)]),0)
        identity=json.loads(output.getvalue())['change_id']
        for args in [['memory','show','lessons'],['memory','due'],['journal','--day','2026-10-16'],['undo',identity]]:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([args[0],'--state',str(root)]+args[1:]),0)

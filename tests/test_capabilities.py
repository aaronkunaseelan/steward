from fixture_mind import use_settings
from datetime import timedelta
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import unittest
import test_jobs
from test_jobs import NOW, spec
from steward import capabilities as c, jobs, runtime
from steward.notify import notify
from steward.state import StateError, writer
from steward.cli import main


class CapabilityTests(unittest.TestCase):
    add = test_jobs.JobsTests.add
    tick = test_jobs.JobsTests.tick
    def setUp(self):
        test_jobs.JobsTests.setUp(self)
        self.caproot = Path(self.tmp.name) / 'mind/capabilities'
        self.meta = dict(name='sample', reads=['qmd:instapaper'], calls=['qmd'], record=['title'])
        self.writecap()

    def writecap(self):
        (self.caproot / 'sample.md').write_text('---\n' + '\n'.join(k + ': ' + json.dumps(v) for k,v in self.meta.items()) + '\n---\nUseful capability prose')

    def valid(self):
        return patch.object(c, 'validate', return_value=self.meta)

    def test_qmd_admission_rejects_contract_mismatch(self):
        with patch('shutil.which', return_value='/bin/qmd'), patch('subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, '{"sources":{"instapaper":{}}}')):
            with self.assertRaisesRegex(StateError, 'qmd sources unknown'):
                c.validate('sample')

    def test_frontmatter_and_dependencies(self):
        self.assertIn('Useful capability prose', c.load('sample')[1])
        with writer(self.root) as store:
            with self.assertRaisesRegex(StateError, 'missing capability'):
                jobs.job_verb(store.db, 'add', 'missing', spec(capability='missing'))
            with patch('shutil.which', return_value=None):
                with self.assertRaisesRegex(StateError, 'missing CLI: qmd'):
                    jobs.job_verb(store.db, 'add', 'missing', spec(capability='sample'))
            status = json.loads((Path(__file__).parent / 'fixtures/qmd.status.v1.json').read_text())
            status['sources'] = {}
            with patch('shutil.which', return_value='/bin/qmd'), patch('subprocess.run', return_value=subprocess.CompletedProcess([],0,json.dumps(status))):
                with self.assertRaisesRegex(StateError, 'unknown qmd source: instapaper'):
                    jobs.job_verb(store.db, 'add', 'missing', spec(capability='sample'))
            self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 0)
        self.meta['calls'] = 'qmd'
        self.writecap()
        with self.assertRaises(StateError):
            c.load('sample')

    def test_interests_version_and_undo(self):
        value = dict(topic='systems', evidence=['instapaper:1','instapaper:2'], source='qmd:instapaper', capability='sample', refreshed_at=NOW.isoformat())
        with writer(self.root) as store:
            with self.assertRaisesRegex(StateError, '3 distinct'):
                c.interest(store.db,'set','systems',value)
            value['evidence'].append('instapaper:3')
            c.interest(store.db,'set','systems',value)
            result = c.interest(store.db,'rm','systems')
            jobs.undo(store.db,result['change_id'])
            self.assertEqual(jobs.current(store.db,'interests')[0]['state'],'active')
            self.assertEqual(len(jobs.snapshot(store.db)['interests']),1)

    def test_optional_interest_stage_is_validated_and_versioned(self):
        value = dict(topic='systems', evidence=['instapaper:1', 'instapaper:2', 'instapaper:3'],
                     source='qmd:instapaper', capability='sample', refreshed_at=NOW.isoformat())
        with writer(self.root) as store:
            c.interest(store.db, 'set', 'systems', value)
            for stage in ('current', 'new', 'emerging'):
                value['stage'] = stage
                c.interest(store.db, 'set', 'systems', value)
                saved = json.loads(jobs.get(store.db, 'interests', 'systems')['spec_json'])
                self.assertEqual(saved['stage'], stage)
            count = store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0]
            for stage in ('old', '', None, [], 1):
                with self.subTest(stage=stage), self.assertRaises(StateError):
                    c.interest(store.db, 'set', 'systems', dict(value, stage=stage))
            with self.assertRaises(StateError):
                c.interest(store.db, 'set', 'systems', dict(value, extra='no'))
            self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], count)

    def test_told_check_pending_and_sent(self):
        with writer(self.root) as store:
            notify(store.db,'Finds','result','paper','10.1/paper',deliver='digest',now=NOW)
            self.assertEqual(c.told_check(store.db,['10.1/paper','new']), {'10.1/paper':True,'new':False})
            self.assertEqual(notify(store.db,'Finds','result','paper','10.1/paper',now=NOW)['status'],'already_told')
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['told','--state',str(self.root),'check','10.1/paper','new']),0)
        self.assertEqual(json.loads(output.getvalue()),{'10.1/paper':True,'new':False})

    def test_empty_runs_never_pause_a_job(self):
        with self.valid():
            self.add(capability='sample')
        with writer(self.root) as store:
            db = store.db
            cid = jobs.change(db, 'fixture', NOW)
            for i in range(4):
                jobs.append(db, 'runs', dict(run_id=str(i), version=0, job='test', state='succeeded',
                    planned_for=NOW.isoformat(), reason='No findings', started_at=NOW.isoformat(),
                    ended_at=NOW.isoformat(), unit=None, exit_code=0, manifest=None,
                    spec_json=json.dumps(spec(capability='sample'))), cid)
            db.commit()
        self.tick()
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'jobs', 'test')['state'], 'active')
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='retirements'").fetchone())

    def test_declared_permissions_not_capability_name_control_packet(self):
        import runpy
        with self.valid(): self.add(capability='sample')
        for name, permits in [('sample', ['page', 'alert', 'health', 'fix']), ('security-advisories', [])]:
            with self.subTest(name=name):
                with writer(self.root) as store, self.valid():
                    jobs.job_verb(store.db, 'edit', 'test', spec(capability=name))
                directory = Path(self.tmp.name) / name
                meta = dict(name=name, reads=[], calls=['fixture-tool'], record=['advisory_id'])
                if permits: meta['permits'] = permits
                with patch.dict(os.environ, STEWARD_RUN_DIR=str(directory), STEWARD_STATE=str(self.root), STEWARD_JOB='test'), \
                     patch.object(c, 'load', return_value=(meta, 'Research')), \
                     patch('steward.mind.read', return_value='fixture interrupt charter'), \
                     patch.object(runtime, 'harness', return_value='fake-run'), \
                     patch.object(sys, 'argv', ['agent-run', name]), redirect_stdout(io.StringIO()):
                    runpy.run_path(str(Path('jobs/agent-run').resolve()), run_name='__main__')
                packet = (directory / 'packet.md').read_text()
                self.assertIn('Declared tools: ["fixture-tool"]', packet)
                self.assertNotIn('secwatch', packet)
                self.assertNotIn('/jobs/security-', packet)
                for text in ('fixture interrupt charter', 'Also permitted: page put', 'Also permitted: notify Alerts',
                             'Read-only producer status/telemetry CLI calls',
                             'fix subcommands of the tools declared in calls',
                             'records each attempted/applied fix', 'Never bypass refusals'):
                    if permits: self.assertIn(text, packet)
                    else: self.assertNotIn(text, packet)

    def test_permits_metadata_is_optional_and_validated(self):
        self.assertNotIn('permits', c.load('sample')[0])
        self.meta['permits'] = ['page', 'alert', 'health', 'fix']
        self.writecap()
        self.assertEqual(c.load('sample')[0]['permits'], self.meta['permits'])
        for invalid in ('page', ['sudo'], ['security'], [1]):
            self.meta['permits'] = invalid
            self.writecap()
            with self.assertRaises(StateError): c.load('sample')

    def test_agent_packet_and_link(self):
        # Actual runner subprocess with fake tty; no model or network.
        with writer(self.root) as store:
            with patch.object(c,'validate'):
                jobs.job_verb(store.db,'add','test',spec(capability='papers-from-saves', deliver='silent', model='haiku'))
            value=dict(topic='systems',evidence=['instapaper:1','instapaper:2','instapaper:3'],source='qmd:instapaper',capability='papers-from-saves',refreshed_at=NOW.isoformat())
            c.interest(store.db,'set','systems',value)
            store.db.execute('INSERT INTO told VALUES (?,?,?,?,?,?)',('known-doi',NOW.isoformat(),'test',None,'Finds',1))
            store.db.commit()
        bindir=Path(self.tmp.name)/'bin'; bindir.mkdir()
        tty=bindir/'tty'; tty.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STEWARD_RUN_DIR/tty-args"\nprintf \'{"run":"fake-run"}\\n\'\n');tty.chmod(0o755)
        directory=Path(self.tmp.name)/'run'
        env=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],STEWARD_RUN_DIR=str(directory),STEWARD_STATE=str(self.root),STEWARD_JOB='test')
        subprocess.run([str(Path('jobs/agent-run').resolve()),'papers-from-saves'],env=env,check=True,capture_output=True)
        packet=(directory/'packet.md').read_text()
        for text in ('known-doi','instapaper:3','systems','A weak Find costs attention','keeps a test input fresh',
                     '[label](https://…)', 'bare URLs are refused', 'retain full source URLs'):
            self.assertIn(text,packet)
        self.assertIn('claude-haiku-5-5', (directory/'tty-args').read_text())
        self.assertIn('--deliver silent', packet)
        self.assertNotIn('--deliver digest', packet)
        self.assertEqual(json.loads((directory/'links.json').read_text()),{'tty_run':'fake-run'})


class ModelChoiceTests(unittest.TestCase):
    setUp = use_settings

    """Owner decisions: the wake is Opus at medium; async work is Sol or Haiku at high (2026-10-16),
    or Opus at medium like the wake (2026-10-18, consolidate)."""

    def test_wake_is_opus_medium(self):
        self.assertEqual(runtime.WAKE_AGENT, ['-a', 'claude', '-m', 'claude-opus-5-5'])
        self.assertEqual(runtime.WAKE_VENDOR_ARGS[runtime.WAKE_VENDOR_ARGS.index('--effort') + 1], 'medium')
        self.assertIn('--exclude-dynamic-system-prompt-sections', runtime.WAKE_VENDOR_ARGS)

    def test_async_models_at_high_effort(self):
        self.assertEqual(set(c.AGENTS), {'sol', 'haiku', 'opus'})
        self.assertIn('model_reasoning_effort=high', c.VENDOR_ARGS['sol'])
        self.assertEqual(c.VENDOR_ARGS['haiku'][-2:], ['--effort', 'high'])
        self.assertEqual(c.AGENTS['opus'], runtime.WAKE_AGENT)
        self.assertEqual(c.VENDOR_ARGS['opus'][1:], [arg for arg in runtime.WAKE_VENDOR_ARGS
                                                    if arg != '--exclude-dynamic-system-prompt-sections'])

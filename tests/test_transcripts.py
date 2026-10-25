from fixture_mind import use_settings
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from fixture_mind import command as released
from unittest.mock import patch

from steward import archive, cli, jobs, runtime, transcripts
from steward.contracts import issues, load
from steward.state import StateError, writer

NOW = datetime(2026, 10, 17, 8, 5, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / 'fixtures'
RUN = 'a' * 32


def validate(case, schema, value):
    """Validate independent schema bounds, patterns and exact-key constraints."""
    case.assertEqual(issues(schema, value), [])
    if isinstance(value, dict):
        if schema.get('additionalProperties') is False:
            case.assertFalse(set(value) - schema.get('properties', {}).keys())
        for k, v in value.items():
            if k in schema.get('properties', {}):
                validate(case, schema['properties'][k], v)
    elif isinstance(value, list):
        case.assertLessEqual(len(value), schema.get('maxItems', len(value)))
        for v in value:
            validate(case, schema.get('items', {}), v)
    elif isinstance(value, str):
        case.assertLessEqual(len(value), schema.get('maxLength', len(value)))
        if 'pattern' in schema:
            case.assertRegex(value, schema['pattern'])


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state, self.data, self.home = (self.root / p for p in ('state', 'data', 'home'))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.tty_calls = self.root / 'tty-calls'
        self.tty_map = self.root / 'tty-map.json'
        self.tty_map.write_text('{}')
        (self.bin / 'tty').write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
assert sys.argv[1] == 'show'
with open(os.environ['TEST_TTY_CALLS'], 'a') as f: f.write(sys.argv[2] + '\\n')
print(json.dumps(json.loads(pathlib.Path(os.environ['TEST_TTY_MAP']).read_text())[sys.argv[2]]))
''')
        (self.bin / 'tty').chmod(0o700)
        guard = patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                           TEST_TTY_MAP=str(self.tty_map), TEST_TTY_CALLS=str(self.tty_calls), STEWARD_DATA=str(self.data))
        guard.start()
        self.addCleanup(guard.stop)
        with writer(self.state, initialize=True):
            pass
        self.wake_row = dict(id=7, started_at=(NOW - timedelta(minutes=5)).isoformat(),
                             status='completed', checkpoint='{}', harness_run_id='wake-tty')
        self.job_row = dict(run_id=RUN, job='watch', started_at=(NOW - timedelta(minutes=5)).isoformat(),
                            planned_for=NOW.isoformat(), ended_at=NOW.isoformat(), state='succeeded', spec_json='{}', reason='Synthetic placement')
        self.claude = self.native('claude', 'synthetic-claude', cwd=str(self.state / 'wakes/7'))
        self.codex = self.native('codex', 'synthetic-codex')
        self.tty_map.write_text(json.dumps({
            'wake-tty': dict(agent='claude', thread='synthetic-claude', cwd=str(self.state / 'wakes/7'), ended=NOW.isoformat()),
            'job-tty': dict(agent='codex', thread='synthetic-codex', ended=NOW.isoformat())}))

    def native(self, agent, thread, cwd=None):
        if agent == 'claude':
            project = re.sub(r'[^A-Za-z0-9-]', '-', cwd) if cwd else '-synthetic'
            path = self.home / '.claude/projects' / project / (thread + '.jsonl')
        else:
            path = self.home / '.codex/sessions/2026/10/17' / ('rollout-synthetic-' + thread + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / ('transcript-' + agent + '.jsonl'), path)
        return path

    def manifest(self, links=None, **extra):
        directory = self.data / 'runs/watch' / RUN
        directory.mkdir(parents=True, exist_ok=True)
        manifest = dict(job='watch', run_id=RUN, started_at=self.job_row['started_at'], ended_at=NOW.isoformat(),
                        status='succeeded', links=links or [], **extra)
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        return directory, manifest

    def seed(self):
        with writer(self.state) as store:
            with store.db:
                row = self.wake_row
                store.db.execute('INSERT INTO wakes(id,started_at,status,checkpoint,harness_run_id) VALUES (?,?,?,?,?)',
                                 tuple(row[k] for k in ('id', 'started_at', 'status', 'checkpoint', 'harness_run_id')))
                cid = jobs.change(store.db, 'fixture', NOW)
                jobs.append(store.db, 'runs', self.job_row, cid)
        self.manifest([{'tty_run': 'job-tty'}])

    def test_archive_wake_job_and_idempotent_backfill_with_fake_tty(self):
        self.seed()
        first = archive.sweep(self.state, self.data, now=NOW, home=self.home)
        self.assertEqual(first, dict(complete=2, pending=0, errors=0))
        paths = list(self.data.rglob('transcript-meta.json'))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.assertEqual(archive.sweep(self.state, self.data, now=NOW + timedelta(minutes=20), home=self.home), first)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        self.assertEqual(len(self.tty_calls.read_text().splitlines()), 2)
        for identity, source in [('wake-7', self.claude), ('run-' + RUN, self.codex)]:
            directory, meta = transcripts.metadata(self.data, identity)
            session = meta['sessions'][0]
            copy = directory / 'sessions' / session['file']
            self.assertEqual(copy.read_bytes(), source.read_bytes())
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / 'transcript-meta.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(session['bytes'], source.stat().st_size)
            self.assertIsNotNone(session['model'])
            self.assertEqual(session['source'], str(source))
            self.assertEqual(meta['ended_at'], NOW.isoformat())

    def test_finished_archives_recover_end_from_last_native_turn_without_tty_end(self):
        mapping = json.loads(self.tty_map.read_text())
        for info in mapping.values():
            info.pop('ended')
        self.tty_map.write_text(json.dumps(mapping))
        end = NOW - timedelta(seconds=5)
        for path in (self.claude, self.codex):
            with path.open('a') as stream:
                row = dict(type='assistant', timestamp=end.isoformat(), message={'content': 'Final'}) if path == self.claude else dict(
                    type='response_item', timestamp=end.isoformat(), payload={'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'Final'}]})
                stream.write(json.dumps(row) + '\n')
                stream.write('{partial')
        # A complete archive still needs an evidence-backed completion time.
        directory = self.data / 'wakes/7'
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        meta_path = directory / 'transcript-meta.json'
        old = json.loads(meta_path.read_text())
        old.update(complete=True, ended_at=None)
        meta_path.write_text(json.dumps(old))
        self.assertEqual(transcripts.document(self.data, 'wake-7')['ended_at'], end.isoformat())
        wake = archive.wake(self.data, self.wake_row, now=NOW + timedelta(hours=2), home=self.home)
        self.assertEqual(wake['ended_at'], end.isoformat())
        _, manifest = self.manifest([{'tty_run': 'job-tty'}])
        manifest['ended_at'] = None
        job = archive.job(self.data, dict(self.job_row, ended_at=None), manifest, now=NOW, home=self.home)
        self.assertEqual(job['ended_at'], end.isoformat())
        self.assertTrue(job['complete'])

    def test_sweep_uses_journal_completion_for_non_model_wake_not_start_or_next_wake(self):
        with writer(self.state) as store:
            with store.db:
                store.db.execute('INSERT INTO wakes(id,started_at,status,checkpoint) VALUES (?,?,?,?)',
                                 (7, self.wake_row['started_at'], 'skipped', '{"model_calls":0}'))
        envelope = json.dumps(dict(MESSAGE=json.dumps(dict(wake_id=7, status='skipped', model_calls=0)),
                                  __REALTIME_TIMESTAMP=str(int(NOW.timestamp() * 1_000_000))))
        def journal(argv, **options):
            self.assertEqual(argv[:2], ['journalctl', '--user'])
            self.assertIn('steward-wake.service', argv)
            self.assertIn('steward.service', argv)
            import subprocess
            return subprocess.CompletedProcess(argv, 0, envelope + '\n{partial', '')
        with patch('steward.archive.subprocess.run', side_effect=journal):
            self.assertEqual(archive.sweep(self.state, self.data, now=NOW, home=self.home)['complete'], 1)
        doc = transcripts.document(self.data, 'wake-7')
        self.assertEqual(doc['ended_at'], NOW.isoformat())
        self.assertEqual(doc['sessions'], [])
        with patch('steward.archive.subprocess.run', side_effect=AssertionError('settled archives need no journal')):
            archive.sweep(self.state, self.data, now=NOW + timedelta(hours=2), home=self.home)

    def test_unknown_end_is_pending_and_unstarted_agent_run_needs_no_session(self):
        row = dict(self.wake_row, harness_run_id=None, checkpoint='{"model_calls":0}')
        self.assertFalse(archive.wake(self.data, row, now=NOW, home=self.home)['complete'])
        with self.assertRaises(StateError):
            transcripts.document(self.data, 'wake-7')
        # Reject complete metadata when no completion evidence exists.
        path = self.data / 'wakes/7/transcript-meta.json'
        old = json.loads(path.read_text())
        old['complete'] = True
        path.write_text(json.dumps(old))
        with self.assertRaisesRegex(StateError, 'completion time is unknown'):
            transcripts.document(self.data, 'wake-7')
        self.manifest(argv=['/release/gsd', 'steady'])
        row = dict(self.job_row, started_at=None, state='skipped')
        meta = archive.job(self.data, row, manifest={}, now=NOW, home=self.home)
        self.assertTrue(meta['complete'])
        self.assertEqual(meta['sessions'], [])

    def test_sweep_writes_edge_summary_and_transcript_once(self):
        self.seed()
        with writer(self.state) as store:
            with store.db:
                jobs.change(store.db, 'notify', NOW - timedelta(minutes=2), actor='model')
                jobs.change(store.db, 'memory write', NOW - timedelta(minutes=1), actor='model')
                jobs.change(store.db, 'job edit', NOW - timedelta(minutes=1))  # the owner's, not the wake's
                jobs.change(store.db, 'notify', NOW + timedelta(minutes=9), actor='model')  # after the wake
        directory, _ = self.manifest([{'tty_run': 'job-tty'}], exit_code=0)
        (directory / 'records.jsonl').write_text(json.dumps(
            {'counts': {'accepted': 3, 'rejected': 0}, 'published': ['x'], 'error': 'boom token=abc'}) + '\n')
        archive.sweep(self.state, self.data, now=NOW, home=self.home)
        wake = json.loads((self.data / 'wakes/7/summary.json').read_text())
        job = json.loads((directory / 'summary.json').read_text())
        for value in (wake, job):
            validate(self, load('steward.run-summary.v1'), value)
        # Claude usage once per message id; Codex totals are cumulative, a repeated total is not a call.
        self.assertEqual((wake['tokens'], wake['model_calls']), (dict(input=12, output=8, cache_write=100, cache_read=2000, weighted=345), 2))
        self.assertEqual((job['tokens'], job['model_calls']), (dict(input=200, output=60, cache_write=0, cache_read=300, weighted=290), 2))
        self.assertEqual((wake['tools'], wake['landed']), ({'Bash': 1}, {'notify': 1, 'memory write': 1}))
        self.assertEqual((job['tools'], job['landed']), ({'exec_command': 1}, {'accepted': 3, 'published': 1}))
        self.assertEqual((job['exit_code'], job['reason'], job['duration_s']), (0, 'Synthetic placement', 300.0))
        self.assertNotIn('abc', job['error'])
        validate(self, load('steward.transcript.v1'), json.loads((self.data / 'wakes/7/transcript.json').read_text()))
        before = (self.data / 'wakes/7/summary.json').stat().st_mtime_ns
        archive.sweep(self.state, self.data, now=NOW + timedelta(minutes=20), home=self.home)
        self.assertEqual((self.data / 'wakes/7/summary.json').stat().st_mtime_ns, before)

    def test_contract_rejects_extra_keys_in_real_output(self):
        self.seed()
        archive.sweep(self.state, self.data, now=NOW, home=self.home)
        directory = self.data / 'wakes/7'
        for identity, filename in (('steward.transcript.v1', 'transcript.json'),
                                   ('steward.run-summary.v1', 'summary.json')):
            with self.subTest(contract=identity):
                schema = load(identity)
                self.assertIs(schema['additionalProperties'], False)
                payload = json.loads((directory / filename).read_text())
                self.assertEqual(issues(schema, payload), [])
                payload['unexpected'] = True
                self.assertEqual(issues(schema, payload), ['$.unexpected: unexpected'])

    def test_real_output_for_both_agents_validates_draft(self):
        self.seed()
        archive.sweep(self.state, self.data, now=NOW, home=self.home)
        for identity in ('wake-7', 'run-' + RUN):
            out = io.StringIO()
            with redirect_stdout(out), patch('steward.cli.writer', side_effect=AssertionError('read only')), \
                 patch('steward.archive.tty_info', side_effect=AssertionError('archive only')):
                self.assertEqual(cli.main(['transcript', identity, '--json', '--data', str(self.data)]), 0)
            doc = json.loads(out.getvalue())
            validate(self, load('steward.transcript.v1'), doc)
            self.assertLessEqual(len(out.getvalue().encode()), 1_000_000)
            turns = doc['sessions'][0]['turns']
            self.assertEqual([t['i'] for t in turns], list(range(len(turns))))
            self.assertEqual([t['type'] for t in turns], ['text', 'thinking', 'text', 'tool_call', 'tool_result', 'text']
                             if identity == 'wake-7' else ['text', 'thinking', 'tool_call', 'tool_result', 'text'])
            call = next(t for t in turns if t['type'] == 'tool_call')
            self.assertEqual(call['text'], '{"command":"printf synthetic"}')
            self.assertEqual(next(t for t in turns if t['type'] == 'tool_result')['name'], call['name'])
            self.assertNotIn('bookkeeping', out.getvalue())
            self.assertNotIn('encrypted reasoning', out.getvalue())
            self.assertNotIn('Duplicate finished', out.getvalue())

    def test_gsd_records_archive_two_native_sessions_with_labels(self):
        directory, _ = self.manifest(argv=['/release/gsd', 'steady', '--once'])
        self.native('claude', 'synthetic-review')
        (directory / 'records.jsonl').write_text(json.dumps({'links': [
            {'session': 'synthetic-claude', 'agent': 'claude', 'label': 'a'},
            {'session': 'synthetic-review', 'agent': 'claude', 'label': 'r1'}]}) + '\n')
        meta = archive.job(self.data, self.job_row, now=NOW, home=self.home)
        self.assertTrue(meta['complete'])
        self.assertEqual([s['label'] for s in meta['sessions']], ['a', 'r1'])
        doc = transcripts.document(self.data, 'run-' + RUN)
        self.assertEqual(len(doc['sessions']), 2)
        self.assertEqual([s['label'] for s in doc['sessions']], ['a', 'r1'])
        self.assertFalse(self.tty_calls.exists())

    def test_growing_source_recopied_only_within_hour(self):
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        with self.claude.open('a') as out:
            out.write('{"type":"user","message":{"content":"late tail"}}\n')
        meta = archive.wake(self.data, self.wake_row, now=NOW + timedelta(minutes=30), home=self.home)
        self.assertEqual(meta['sessions'][0]['bytes'], self.claude.stat().st_size)
        frozen = meta['sessions'][0]['bytes']
        with self.claude.open('a') as out:
            out.write('{"type":"user","message":{"content":"too late"}}\n')
        meta = archive.wake(self.data, self.wake_row, now=NOW + timedelta(hours=2), home=self.home)
        self.assertEqual(meta['sessions'][0]['bytes'], frozen)
        self.assertEqual(len(self.tty_calls.read_text().splitlines()), 1)

    def test_missing_source_is_pending_and_retry_can_complete_even_after_hour(self):
        self.claude.unlink()
        meta = archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        self.assertFalse(meta['complete'])
        with self.assertRaisesRegex(StateError, 'not fully archived'):
            transcripts.document(self.data, 'wake-7')
        self.native('claude', 'synthetic-claude', cwd=str(self.state / 'wakes/7'))
        self.assertTrue(archive.wake(self.data, self.wake_row, now=NOW + timedelta(hours=2), home=self.home)['complete'])

    def test_no_agent_has_empty_sessions_but_missing_agent_links_are_unknown(self):
        self.manifest(argv=['/release/backup'])
        self.assertTrue(archive.job(self.data, self.job_row, now=NOW, home=self.home)['complete'])
        self.assertEqual(transcripts.document(self.data, 'run-' + RUN)['sessions'], [])
        shutil.rmtree(self.data)
        self.manifest(argv=['/release/gsd', 'steady'])
        self.assertFalse(archive.job(self.data, self.job_row, now=NOW, home=self.home)['complete'])
        row = dict(self.wake_row, harness_run_id=None, checkpoint='{"model_calls":1}')
        self.assertFalse(archive.wake(self.data, row, now=NOW, home=self.home)['complete'])

    def test_unknown_unarchived_and_traversal_ids_fail_without_stdout_or_writes(self):
        for identity in ('wake-999', 'run-' + 'b' * 32, '../escape', 'run-../../secret'):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertNotEqual(cli.main(['transcript', identity, '--json']), 0)
            self.assertEqual(out.getvalue(), '')
            self.assertTrue(err.getvalue())
        self.assertFalse(self.data.exists())

    def test_truncation_turn_limit_and_redaction_preserve_raw_archive(self):
        secret = 'sk-ant-' + 'S' * 40
        text = 'Bearer abcdef123456 PASSWORD=hunter2 API_KEY="secret-value" ' + secret + ' ' + 'x' * 9000
        rows = [dict(type='user', message={'content': text}) for _ in range(128)]
        self.claude.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        doc = transcripts.document(self.data, 'wake-7')
        validate(self, load('steward.transcript.v1'), doc)
        serialized = transcripts.compact(doc)
        self.assertTrue(doc['truncated'])
        self.assertLessEqual(len(serialized.encode()), 999_999)
        for value in (secret, 'hunter2', 'secret-value', 'abcdef123456'):  # gitleaks:allow
            self.assertNotIn(value, serialized)
        self.assertIn('[REDACTED]', serialized)
        directory, meta = transcripts.metadata(self.data, 'wake-7')
        self.assertEqual((directory / 'sessions' / meta['sessions'][0]['file']).read_bytes(), self.claude.read_bytes())

    def test_document_cap_drops_oldest_tool_result_text_before_other_turns(self):
        rows = [dict(type='user', message={'content': [dict(type='tool_result', tool_use_id='x', content='🦉' * 8000)]})
                for _ in range(40)]
        rows.append(dict(type='assistant', message={'content': 'Keep the newest answer.'}))
        self.claude.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        doc = transcripts.document(self.data, 'wake-7')
        turns = doc['sessions'][0]['turns']
        self.assertEqual(len(turns), 41)
        self.assertEqual(turns[0]['text'], '')
        self.assertTrue(turns[0]['truncated'])
        self.assertEqual(turns[-1]['text'], 'Keep the newest answer.')
        self.assertLessEqual(len(transcripts.compact(doc).encode()) + 1, 1_000_000)

    def test_session_limit_and_malformed_trailing_line(self):
        directory, manifest = self.manifest([{'session': f'synthetic-{n}', 'agent': 'claude'} for n in range(9)])
        for n in range(9):
            path = self.native('claude', f'synthetic-{n}')
            with path.open('a') as out:
                out.write('{partial')
        meta = archive.job(self.data, self.job_row, manifest, now=NOW, home=self.home)
        self.assertEqual(len(meta['sessions']), 9)  # Raw archive never truncates.
        doc = transcripts.document(self.data, 'run-' + RUN)
        self.assertEqual(len(doc['sessions']), 8)
        self.assertTrue(doc['truncated'])
        validate(self, load('steward.transcript.v1'), doc)

    def test_tool_inputs_stay_compact_valid_json_after_credential_redaction(self):
        value = dict(command='PASSWORD=hunter2 curl -H "Authorization: Bearer abcdef123"',  # gitleaks:allow
                     api_key='synthetic-secret', nested={'token': 'another-secret'})
        row = dict(type='assistant', message={'content': [dict(type='tool_use', id='t', name='Bash', input=value)]})
        self.claude.write_text(json.dumps(row) + '\n')
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        doc = transcripts.document(self.data, 'wake-7')
        text = doc['sessions'][0]['turns'][0]['text']
        expected = dict(command='PASSWORD="[REDACTED]" curl -H "Authorization: Bearer [REDACTED]"',
                        api_key='[REDACTED]', nested={'token': '[REDACTED]'})
        self.assertEqual(json.loads(text), expected)
        self.assertEqual(text, json.dumps(expected, ensure_ascii=False, separators=(',', ':')))
        for secret in ('hunter2', 'abcdef123', 'synthetic-secret', 'another-secret'):
            self.assertNotIn(secret, text)

    def test_turn_cap_without_document_size_pressure(self):
        self.claude.write_text(''.join(json.dumps(dict(type='user', message={'content': str(i)})) + '\n' for i in range(2001)))
        archive.wake(self.data, self.wake_row, now=NOW, home=self.home)
        doc = transcripts.document(self.data, 'wake-7')
        self.assertTrue(doc['truncated'])
        self.assertEqual(len(doc['sessions'][0]['turns']), 2000)

    def test_other_credential_forms_are_redacted(self):
        for value in ('ghp_' + 'x' * 25, 'AIza' + 'x' * 25, 'xoxb-123456789-longvalue',
                      'AKIA' + 'X' * 16, '1234567890:' + 'x' * 35):
            self.assertNotIn(value, transcripts.redact(value))
        for value in ('{"api_key":"secret"}', '--password secret', 'export MY_ACCESS_TOKEN=secret'):
            self.assertNotIn('secret', transcripts.redact(value))
        self.assertNotIn('private-material', transcripts.redact('-----BEGIN RSA PRIVATE KEY-----\nprivate-material\n-----END RSA PRIVATE KEY-----'))

    def test_wake_and_job_finalizers_call_archive_without_live_paths(self):
        from fixture_mind import install
        install(self, self.root)
        with writer(self.state) as store:
            jobs.job_verb(store.db, 'add', 'watch', dict(justification='Synthetic job', argv=[released()],
                          needs={'every': {'min': '1h', 'max': '2h'}}), now=NOW)
            run_id = jobs.run_verb(store.db, 'now', 'watch', 'Synthetic run', now=NOW)['run_id']
            with store.db:
                cid = jobs.change(store.db, 'fixture', NOW)
                row = jobs.get(store.db, 'runs', run_id)
                jobs.append(store.db, 'runs', dict(row, state='started', started_at=NOW.isoformat()), cid)
        self.assertEqual(runtime.execute(self.state, run_id, self.data, clock=lambda: NOW)['archive'], 'complete')
        self.assertTrue((self.data / 'runs/watch' / run_id / 'transcript-meta.json').exists())
        report = runtime.wake(self.state, clock=lambda: NOW, settings={'wake_model': False}, prompt='Synthetic', sender=lambda *a, **k: 1)
        self.assertEqual(report['archive'], 'complete')
        _, meta = transcripts.metadata(self.data, 'wake-' + str(report['wake_id']))
        self.assertEqual(meta['ended_at'], NOW.isoformat())

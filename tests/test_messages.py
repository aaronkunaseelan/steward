from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from fixture_mind import command as released
from unittest.mock import patch

from steward import jobs, messages, runtime, senses
from steward.notify import notify, flush
from steward.state import writer, StateError

NOW = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)  # 09:00 Athens
ORIENT = NOW - timedelta(hours=2)  # 07:00 Athens
BINDING = dict(chat_id=-1, owner_user_id=2, topics=dict(Today=2, Changes=3, Finds=4, Alerts=5, Talk=6))


class MessagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass
        self.sent = []
        guard = patch('steward.notify.api', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def send(self, topic, text, **kw):
        self.sent.append((topic, text, kw))
        return len(self.sent)

    def update(self, uid, text, chat=-1, owner=2, thread=6, reply=None):
        return dict(update_id=uid, message=dict(message_id=uid+100, chat=dict(id=chat),
                    **{'from':dict(id=owner)}, text=text, message_thread_id=thread,
                    reply_to_message=dict(message_id=reply)))

    def poll(self, db, updates):
        return messages.poll(db, NOW, {}, self.send, lambda offset: updates, BINDING)

    def add(self, db):
        return jobs.job_verb(db, 'add', 'watch', dict(justification='Keep inputs fresh', argv=[released()], needs={'every':{'min':'1h','max':'1d'}}), now=NOW)

    def test_poll_health_records_safe_error_and_success_clears_it(self):
        with writer(self.root) as st:
            self.assertIsNone(messages.poll_status(st.db, NOW)['last_poll_ok_at'])
            for target in ('binding', 'fetch'):
                with self.subTest(target=target), patch('steward.messages.binding',
                        side_effect=OSError('SECRET_TOKEN') if target == 'binding' else None,
                        return_value=BINDING):
                    def fail(offset):
                        raise OSError('SECRET_TOKEN')
                    writes = []
                    st.db.set_trace_callback(writes.append)
                    self.assertFalse(messages.poll(st.db, NOW, {}, fetcher=fail))
                    st.db.set_trace_callback(None)
                    self.assertEqual(sum(s.startswith('INSERT INTO message_meta') for s in writes), 1)
                    health = jobs.snapshot(st.db, now=NOW)['inbound_poll']
                    self.assertEqual(health['last_error'], dict(code='OSError', at=NOW.isoformat()))
                    self.assertEqual(health['status'], 'unknown')
                    self.assertNotIn('SECRET_TOKEN', str([tuple(r) for r in st.db.execute('SELECT * FROM message_meta')]))
                    self.assertNotIn('SECRET_TOKEN', str(health))
            self.assertFalse(self.poll(st.db, []))
            health = jobs.snapshot(st.db, now=NOW)['inbound_poll']
            self.assertEqual(health, dict(last_poll_ok_at=NOW.isoformat(), last_error=None,
                                          status='ok', reason=None))
            with patch('steward.messages.binding', side_effect=ValueError('SECRET_TOKEN')):
                messages.poll(st.db, NOW + timedelta(minutes=20), {})
            health = messages.poll_status(st.db, NOW + timedelta(minutes=20))
            self.assertEqual(health['last_poll_ok_at'], NOW.isoformat())
            self.assertEqual(health['last_error'], dict(code='ValueError', at=(NOW + timedelta(minutes=20)).isoformat()))

    def test_untrusted_updates_advance_offset_and_talk_is_unhandled(self):
        with writer(self.root) as st:
            self.assertTrue(self.poll(st.db, [self.update(1,'bad',chat=9),self.update(2,'bad',owner=9),self.update(3,'pause watch')]))
            self.assertEqual([r['update_id'] for r in messages.inbox(st.db)], [3])
            self.assertEqual(st.db.execute("SELECT value FROM message_meta WHERE key='offset'").fetchone()[0], '4')
            messages.done(st.db, [3], NOW, 'model')
            self.assertEqual(messages.inbox(st.db), [])

    def test_outstanding_cli_matches_wake_inbox_and_is_read_only(self):
        from contextlib import redirect_stdout
        import io
        import json
        from steward import cli, contracts
        with writer(self.root) as st:
            self.poll(st.db, [self.update(1, 'old Talk'), self.update(2, 'handled'),
                              self.update(3, 'Today instruction', thread=2),
                              self.update(4, 'not owner', owner=9)])
            messages.done(st.db, [2], NOW, 'model')
            expected = [dict(id=r['update_id'], at=r['at']) for r in messages.inbox(st.db)]
        path = self.root / 'state.sqlite'
        before = path.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output), patch('steward.runtime.config', return_value={}), \
                patch('steward.cli.writer', side_effect=AssertionError('writer forbidden')):
            code = cli.main(['inbox', '--outstanding', '--json', '--state', str(self.root)])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report, dict(schema='steward.inbox.v1', outstanding=expected))
        self.assertEqual([r['id'] for r in report['outstanding']], [1, 3])
        self.assertEqual(contracts.issues(contracts.load('steward.inbox.v1'), report), [])
        self.assertEqual(path.read_bytes(), before)
        with writer(self.root) as st:
            st.db.execute("UPDATE inbox SET at='not-a-date' WHERE update_id=1")
            with self.assertRaises(ValueError):
                messages.outstanding(st.db)

    def test_owner_pin_service_update_only_advances_offset(self):
        update = self.update(1, None, thread=2)
        del update['message']['text']
        update['message']['pinned_message'] = {'message_id': 99, 'text': 'pinned words'}
        with writer(self.root) as st:
            self.assertFalse(self.poll(st.db, [update]))
            self.assertEqual(messages.inbox(st.db), [])
            self.assertEqual(st.db.execute("SELECT value FROM message_meta WHERE key='offset'").fetchone()[0], '2')

    def test_all_topics_are_inbound_with_context(self):
        with writer(self.root) as st:
            for uid, thread in enumerate((2, 4, 5), 1):
                self.assertTrue(self.poll(st.db, [self.update(uid, 'instruction', thread=thread, reply=99)]))
            rows = messages.inbox(st.db, BINDING)
            self.assertEqual([r['topic'] for r in rows], ['Today', 'Finds', 'Alerts'])
            self.assertTrue(all(r['reply_to'] == 99 for r in rows))

    def test_media_refs_without_download_and_sticker_note(self):
        with writer(self.root) as st:
            for uid, kind in enumerate(('photo', 'document', 'audio', 'voice', 'video', 'video_note', 'animation', 'sticker'), 1):
                update = self.update(uid, None)
                file = dict(file_id=kind, file_unique_id='unique', mime_type='application/pdf',
                            file_name='test.pdf', file_size=10)
                update['message'][kind] = [dict(file, width=10, height=10), dict(file, file_id='small', width=1, height=1)] if kind == 'photo' else file
                update['message']['caption'] = 'caption'
                self.assertTrue(self.poll(st.db, [update]))
                row = messages.inbox(st.db)[-1]
                if kind == 'sticker':
                    self.assertEqual(row['text'], 'caption\n[sticker]')
                    self.assertEqual(row['attachments'], [])
                else:
                    self.assertEqual(row['attachments'][0]['file_id'], kind)
                    self.assertNotIn('result', row['attachments'][0])

    def test_attachment_results_in_wake_and_not_retried(self):
        import json
        import subprocess
        good = dict(schema='qmd.extract.v1', kind='image', text='x' * 5000,
                    model='azure-document-intelligence:prebuilt-read@2024-11-30',
                    pages=dict(covered=1, total=1, requested=None), error=None)
        from steward import contracts
        self.assertEqual(contracts.issues(contracts.load('qmd.extract.v1', vendor=True), good), [])
        cases = [
            (subprocess.CompletedProcess([], 0, json.dumps(good)), 'done', None),
            (subprocess.CompletedProcess([], 1, ''), 'unknown', 'qmd_exit'),
            (subprocess.CompletedProcess([], 0, json.dumps(dict(good, schema='bad'))), 'unknown', 'qmd_contract'),
            (subprocess.TimeoutExpired('qmd', 420), 'unknown', 'qmd_timeout'),
            (subprocess.CompletedProcess([], 0, 'not json'), 'unknown', 'qmd_json'),
            (subprocess.CompletedProcess([], 0, json.dumps({k:v for k,v in good.items() if k != 'model'})), 'unknown', 'qmd_contract'),
            (subprocess.CompletedProcess([], 4, json.dumps(dict(good, error='unsupported', text=None))), 'failed', 'unsupported'),
            (subprocess.CompletedProcess([], 0, json.dumps(dict(good, error='ocr_failed'))), 'failed', 'ocr_failed'),
            (subprocess.CompletedProcess([], 4, json.dumps(good)), 'failed', 'qmd_exit_4'),
            (subprocess.CompletedProcess([], 1, json.dumps(good)), 'unknown', 'qmd_exit'),
            (subprocess.CompletedProcess([], 0, json.dumps(dict(good, pages=1))), 'unknown', 'qmd_contract'),
        ]
        for uid, (completed, state, reason) in enumerate(cases, 1):
            with writer(self.root) as st:
                update = self.update(uid, None, thread=4)
                update['message']['document'] = dict(file_id='pdf', file_name='test.pdf')
                self.poll(st.db, [update])
            def fake(argv, **kw):
                self.assertEqual(argv, ['qmd', 'extract', '-', '--json'])
                self.assertEqual(kw['timeout'], 420)
                self.assertEqual(kw['input'], b'raw')
                # Network/extraction must not hold the writer.
                with writer(self.root):
                    pass
                if isinstance(completed, Exception):
                    raise completed
                return completed
            runtime.wake(self.root, clock=lambda:NOW, settings={}, prompt='test', sender=self.send,
                         attachment_downloader=lambda ref, deadline: b'raw', qmd_runner=fake)
            with writer(self.root) as st:
                result = messages.inbox(st.db)[-1]['attachments'][0]['result']
                self.assertEqual((result['state'], result['reason']), (state, reason))
                if state == 'done':
                    self.assertEqual(len(result['text']), 4000)
                    self.assertEqual(result['kind'], 'image')
                else:
                    self.assertIn(f'not read ({state}/{reason})', messages.inbox(st.db)[-1]['text'])
        messages.read_attachments(self.root, runner=lambda *a, **k: self.fail('retried'))

    def test_too_large_never_downloads_or_extracts(self):
        with writer(self.root) as st:
            update = self.update(1, None)
            update['message']['voice'] = dict(file_id='voice', file_size=20*1024*1024+1)
            self.poll(st.db, [update])
        messages.read_attachments(self.root, downloader=lambda *a: self.fail('downloaded'),
                                  runner=lambda *a, **k: self.fail('extracted'))
        with writer(self.root) as st:
            self.assertEqual(messages.inbox(st.db)[0]['attachments'][0]['result']['state'], 'too_large')

    def test_text_is_unchanged_and_takes_precedence_over_caption(self):
        update = self.update(1, 'Please check for replies')
        update['message']['caption'] = 'not the text'
        with writer(self.root) as st:
            self.assertTrue(self.poll(st.db, [update]))
            self.assertEqual(messages.inbox(st.db)[0]['text'], 'Please check for replies')

    def test_schema_9_migration_keeps_inbox_and_backup_restore(self):
        import sqlite3
        from steward.state import restore
        with writer(self.root) as st:
            self.poll(st.db, [self.update(1, 'keep this', thread=2)])
        db = sqlite3.connect(self.root / 'state.sqlite')
        db.execute('ALTER TABLE inbox DROP COLUMN attachments')
        db.execute('DROP TABLE pages')
        db.execute('DROP TABLE drafts')
        db.execute('DROP TABLE outbox_keys')
        db.execute('DROP TABLE moments'); db.execute("DELETE FROM changes WHERE actor=\'migration\'")
        db.execute('ALTER TABLE runs DROP COLUMN retry_of')
        db.execute('PRAGMA user_version=9')
        db.commit()
        db.close()
        with writer(self.root) as st:
            self.assertEqual(st.db.execute('PRAGMA user_version').fetchone()[0], 17)
            row = messages.inbox(st.db, BINDING)[0]
            self.assertEqual((row['text'], row['topic'], row['attachments']), ('keep this', 'Today', []))
            st.backup(Path(self.tmp.name) / 'backup')
        target = Path(self.tmp.name) / 'restored'
        restore(Path(self.tmp.name) / 'backup', target)
        with writer(target) as st:
            self.assertEqual(messages.inbox(st.db)[0]['text'], 'keep this')

    def test_caption_undo_matches_only_typed_words(self):
        with writer(self.root) as st:
            cid = self.add(st.db)['change_id']
            update = self.update(1, None)
            update['message'].update(caption='undo ' + cid[:8], photo=[{'file_id': 'unused'}])
            self.assertFalse(self.poll(st.db, [update]))
            self.assertEqual(jobs.get(st.db, 'jobs', 'watch')['state'], 'removed')
            self.assertIn('Restored watch', self.sent[0][1])

    def test_sticker_does_not_change_typed_undo(self):
        with writer(self.root) as st:
            cid = self.add(st.db)['change_id']
            update = self.update(1, 'undo ' + cid[:8], thread=5)
            update['message']['sticker'] = {'file_id': 'unused'}
            self.assertFalse(self.poll(st.db, [update]))
            self.assertEqual(jobs.get(st.db, 'jobs', 'watch')['state'], 'removed')

    def test_prefix_undo_answers_in_same_topic(self):
        with writer(self.root) as st:
            cid = self.add(st.db)['change_id']
            self.poll(st.db, [self.update(1,'undo '+cid[:8],thread=4)])
            self.assertEqual(jobs.get(st.db,'jobs','watch')['state'],'removed')
            self.assertEqual(self.sent[0][0], 'Finds')
            self.assertEqual(self.sent[0][2]['thread_id'],4)
            self.assertIn('Restored watch', self.sent[0][1])

    def test_reply_undo_and_ambiguous_prefix(self):
        with writer(self.root) as st:
            cid = self.add(st.db)['change_id']
            notify(st.db,'Changes','change','created watch',sender=self.send,now=NOW,change_id=cid)
            self.poll(st.db,[self.update(1,'undo',reply=1)])
            self.assertEqual(jobs.get(st.db,'jobs','watch')['state'],'removed')
            with st.db:
                for suffix in ('a'*24,'b'*24):
                    st.db.execute('INSERT INTO changes VALUES (?,?,?,?,?,?)',('12345678'+suffix,NOW.isoformat(),'owner','job add','x',None))
            self.poll(st.db,[self.update(2,'undo 12345678')])
            self.assertIn('ambiguous',self.sent[-1][1])

    def test_talk_wakes_without_damping(self):
        with writer(self.root) as st:
            with st.db:
                st.db.execute("INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,'completed','{}')", (NOW.isoformat(),))
        started=[]
        runtime.tick(self.root,Path(self.tmp.name)/'data',clock=lambda:NOW,settings={},
                     sense_runner=lambda argv: (_ for _ in ()).throw(OSError()), rollout_root=self.root/'none',
                     sender=self.send, update_fetcher=lambda offset:[self.update(1,'do it',thread=2)],telegram=BINDING,
                     wake_starter=lambda:started.append(True))
        self.assertEqual(started,[True])
        with writer(self.root) as st:
            self.assertEqual(runtime.gate(st.db,NOW),'owner message')

    def test_tick_retries_failed_moment_wake_not_completed_without_message(self):
        import json
        now = ORIENT.replace(hour=15, minute=55)  # 18:55 Athens
        with patch('steward.senses.collect', return_value={}), \
             patch('steward.senses.events', return_value=[]), \
             patch('steward.mind.history', return_value=None), \
             patch('steward.pages.poll', return_value=True), \
             patch('steward.archive.sweep', return_value={}), \
             patch('steward.memory.file_days'), patch('steward.memory.provocations'):
            for status, expected in [('failed', [True]), ('completed', [])]:
                with self.subTest(status=status), writer(self.root) as st:
                    st.db.execute('DELETE FROM wakes')
                    start = now - timedelta(minutes=25)
                    checkpoint = json.dumps(dict(model_calls=1, ended_at=start.isoformat(), briefing=dict(complete=True)))
                    st.db.execute('INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,?,?,?)',
                                  (start.isoformat(), status, checkpoint, 'model'))
                    st.db.commit()
                starts = []
                result = runtime.tick(self.root, Path(self.tmp.name)/'data', clock=lambda:now,
                                     settings={'wake_times': []}, sender=self.send,
                                     update_fetcher=lambda offset:[], telegram=BINDING,
                                     wake_starter=lambda:starts.append(True))
                self.assertEqual(starts, expected)
                self.assertEqual(result['wake'], 'event' if expected else None)

    def test_week_gist_does_not_suppress_orientation_fallback(self):
        # First week gist, Monday 26 Oct (Athens has switched to UTC+2).
        from fixture_mind import TEST_ZONE
        morning = datetime(2026, 10, 26, 7, tzinfo=TEST_ZONE)
        with writer(self.root) as st:
            notify(st.db, 'Today', 'result', 'Week gist', 'week:2026-W43',
                   self.send, morning - timedelta(hours=2))
            self.assertEqual(runtime.gate(st.db, morning), 'orientation')
        runtime.wake(self.root, clock=lambda:morning, settings={'wake_model':False},
                     prompt='test', sender=self.send)
        self.assertEqual(len(self.sent), 2)
        self.assertIn('(fallback)', self.sent[-1][1])
        with writer(self.root) as st:
            from steward.timing import moment_kept
            self.assertTrue(moment_kept(st.db, 'orientation', morning))

    def test_orientation_fallback_and_model_orientation(self):
        runtime.wake(self.root, clock=lambda:ORIENT,settings={'wake_model':False},prompt='test',sender=self.send)
        self.assertEqual(self.sent[0][0],'Today')
        self.assertIn('(fallback)',self.sent[0][1])
        self.assertIn('Calendars unknown',self.sent[0][1])
        runtime.wake(self.root, clock=lambda:ORIENT,settings={'wake_model':False},prompt='test',sender=self.send)
        self.assertEqual(len(self.sent),1)
        later=ORIENT+timedelta(days=1)
        def model(argv):
            with writer(self.root) as st:
                notify(st.db,'Today','result','Current state\nModel orientation',
                       sender=self.send,now=later, moment='orientation')
            return 'run-1'
        runtime.wake(self.root,clock=lambda:later,settings={'wake_model':True},runner=model,prompt='test',sender=self.send)
        self.assertEqual(len(self.sent),2)
        self.assertNotIn('fallback',self.sent[-1][1])

    def test_today_limit_refuses_detail_before_any_effect(self):
        with writer(self.root) as st:
            accepted = notify(st.db, 'Today', 'result', 'x' * 1200, sender=self.send, now=NOW)
            self.assertEqual(accepted['status'], 'sent')
            before = st.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
            with self.assertRaisesRegex(StateError, 'page'):
                notify(st.db, 'Today', 'result', 'x' * 1201, sender=self.send, now=NOW)
            self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0], before)
            self.assertEqual(len(self.sent), 1)

    def test_today_limit_digest_once_told_and_quiet(self):
        with writer(self.root) as st:
            with self.assertRaises(StateError):
                notify(st.db,'Today','result','x'*2000,sender=self.send,now=NOW)
            for n in range(3):
                notify(st.db,'Finds','result','source '+str(n),'url:'+str(n),sender=self.send,now=NOW)
            flush(st.db,self.send,NOW+timedelta(hours=9, minutes=29),{})
            self.assertEqual(self.sent,[])
            flush(st.db,self.send,NOW+timedelta(hours=9, minutes=30),{})
            self.assertEqual(len(self.sent),1)
            self.assertEqual(st.db.execute('SELECT count(*) FROM told').fetchone()[0],3)
            self.assertTrue(self.sent[0][2]['disable_notification'])
            flush(st.db,self.send,NOW+timedelta(hours=10),{})
            self.assertEqual(len(self.sent),1)
            notify(st.db,'Talk','result','quiet',sender=self.send,now=NOW-timedelta(minutes=1))
            self.assertTrue(self.sent[-1][2]['disable_notification'])

    def test_digest_splits_and_pending_dedup(self):
        with writer(self.root) as st:
            for i in range(3):
                notify(st.db,'Finds','result','x'*2100,str(i),sender=self.send,now=NOW)
            report=notify(st.db,'Finds','result','duplicate','0',sender=self.send,now=NOW)
            self.assertEqual(report['status'],'already_told')
            flush(st.db,self.send,NOW+timedelta(hours=10),{})
            self.assertEqual(len(self.sent),3)
            self.assertTrue(all(len(m[1])<=4000 for m in self.sent))

    def test_late_find_waits_for_next_day_close(self):
        from fixture_mind import TEST_ZONE
        athens = TEST_ZONE
        evening = datetime(2026, 10, 16, 23, 35, tzinfo=athens)
        with writer(self.root) as st:
            notify(st.db,'Finds','result','late find','k-late',sender=self.send,now=evening)
            flush(st.db,self.send,evening+timedelta(minutes=10),{})
            flush(st.db,self.send,datetime(2026, 10, 17, 18, 29, tzinfo=athens),{})
            self.assertEqual(self.sent,[])
            flush(st.db,self.send,datetime(2026, 10, 17, 18, 30, tzinfo=athens),{})
            self.assertEqual(len(self.sent),1)

    def test_timer_violation_is_a_silent_deduplicated_code_alert(self):
        with writer(self.root) as st:
            cid = jobs.change(st.db, 'tick', NOW, 'tick')
            for observation, count in [(dict(status='unknown', reason='unreadable'), 0),
                                       (dict(status='ok', data={'violation': None}), 0),
                                       (dict(status='ok', data={'violation': 'gsd-step.timer enabled'}), 1)]:
                senses.save(st.db, {'machine': observation}, NOW, cid)
                st.db.commit()
                for _ in range(2):
                    messages.alerts(st.db, NOW, self.send, {})
                self.assertEqual(len(self.sent), count)
            self.assertEqual(self.sent[0][0], 'Alerts')
            self.assertIn('gsd-step.timer enabled', self.sent[0][1])
            self.assertTrue(self.sent[0][2]['disable_notification'])

    def test_alerts_dedupe_and_unknown_duration(self):
        with writer(self.root) as st:
            with st.db:
                cid=jobs.change(st.db,'tick',NOW,'tick')
                senses.save(st.db,{'calendars':{'status':'unknown','reason':'unreadable'}},NOW,cid)
            messages.alerts(st.db,NOW+timedelta(hours=1),self.send,{})
            self.assertEqual(self.sent,[])
            for _ in range(2):
                messages.alerts(st.db,NOW+timedelta(hours=3),self.send,{})
            self.assertEqual(len(self.sent),1)
            messages.alerts(st.db,NOW+timedelta(days=1),self.send,{})
            self.assertEqual(len(self.sent),2)

    def test_wake_changes_are_aggregated_and_reply_is_ambiguous(self):
        def model(argv):
            with writer(self.root) as st:
                self.add(st.db)
                jobs.job_verb(st.db,'pause','watch',actor='model',now=ORIENT)
                jobs.job_verb(st.db,'resume','watch',actor='model',now=ORIENT)
            return 'run-1'
        runtime.wake(self.root,clock=lambda:ORIENT,settings={'wake_model':True},runner=model,prompt='test',sender=self.send)
        changes=[m for m in self.sent if m[0]=='Changes']
        self.assertEqual(len(changes),1)
        self.assertNotIn('undo ', changes[0][1])
        self.assertIn('You:', changes[0][1])
        self.assertEqual(changes[0][1].count('Steward:'), 2)
        with writer(self.root) as st:
            mid=next(i+1 for i,m in enumerate(self.sent) if m[0]=='Changes')
            self.poll(st.db,[self.update(9,'undo',reply=mid)])
        self.assertIn('ambiguous',self.sent[-1][1])

    def test_migration_baselines_historical_edits_but_reports_new_actors_once(self):
        with writer(self.root) as st:
            self.add(st.db)
            jobs.job_verb(st.db, 'pause', 'watch', actor='supervisor', now=NOW)
            jobs.job_verb(st.db, 'resume', 'watch', actor='owner', now=NOW)
            st.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
            st.db.execute('PRAGMA user_version=16')
            st.db.commit()
        with writer(self.root) as st:
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(self.sent, [])
            jobs.job_verb(st.db, 'pause', 'watch', actor='supervisor', now=NOW)
            jobs.job_verb(st.db, 'resume', 'watch', actor='owner', now=NOW)
            messages.changes(st.db, NOW, self.send, {})
            messages.changes(st.db, NOW, self.send, {})
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1]
        self.assertEqual(text.count('Supervisor:'), 1)
        self.assertEqual(text.count('You:'), 1)
        self.assertNotIn('Job add', text)

    def test_external_edits_are_named_once_without_a_wake_cursor(self):
        with writer(self.root) as st:
            self.add(st.db)
            jobs.job_verb(st.db, 'pause', 'watch', actor='supervisor', now=NOW)
            jobs.job_verb(st.db, 'resume', 'watch', actor='owner', now=NOW)
            messages.changes(st.db, NOW, self.send, {})
            messages.changes(st.db, NOW, self.send, {})
            rows = st.db.execute("SELECT id FROM changes WHERE verb IN ('job pause','job resume')").fetchall()
        text = '\n'.join(m[1] for m in self.sent if m[0] == 'Changes')
        self.assertEqual(text.count('Supervisor:'), 1)
        self.assertEqual(text.count('You: Job resume'), 1)
        for row in rows:
            self.assertNotIn(row[0][:8], text)
        self.assertEqual(len([m for m in self.sent if m[0] == 'Changes']), 1)

    def test_identical_change_lines_collapse_in_order_but_keep_all_undo_links(self):
        with writer(self.root) as st:
            with st.db:
                ids = [jobs.change(st.db, verb, NOW, actor) for verb, actor in [
                    ('block choose', 'model'), ('memory write', 'model'),
                    ('block choose', 'model'), ('block choose', 'owner'),
                    ('block choose', 'model')]]
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(self.sent[0][1].splitlines(), [
                'Steward: Block choose (×3)', 'Steward: Memory write', 'You: Block choose'])
            linked = [r[0] for r in st.db.execute(
                'SELECT change_id FROM message_changes ORDER BY rowid')]
            self.assertEqual(linked, ids)
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(len(self.sent), 1)
            self.poll(st.db, [self.update(9, 'undo', reply=1)])
            self.assertIn('ambiguous', self.sent[-1][1])
            self.assertEqual(st.db.execute(
                "SELECT count(*) FROM changes WHERE verb='undo'").fetchone()[0], 0)

    def test_change_summary_reads_plainly(self):
        with writer(self.root) as st:
            with st.db:
                cid=jobs.change(st.db,'block demand add',NOW,'model')
                st.db.execute('UPDATE changes SET summary=? WHERE id=?',('add demand gym: three slots a week',cid))
            messages.changes(st.db, NOW,self.send,{})
        self.assertEqual(self.sent[0][1], 'Steward: Add demand gym: three slots a week')
        self.assertTrue(self.sent[0][2]['disable_notification'])

    def test_failed_wakes_and_provider_limit_alert(self):
        with writer(self.root) as st:
            with st.db:
                cid=jobs.change(st.db,'tick',NOW,'tick')
                senses.save(st.db,{'sol':{'status':'ok','data':{'rate_limit_reached_type':'weekly'}}},NOW,cid)
                for minute in (0,1):
                    st.db.execute("INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,'failed','{}')",((NOW+timedelta(minutes=minute)).isoformat(),))
            messages.alerts(st.db,NOW,self.send,{})
            messages.alerts(st.db,NOW,self.send,{})
            self.assertEqual(len(self.sent),2)
            self.assertTrue(all(m[0]=='Alerts' for m in self.sent))
            self.assertTrue(all(m[2]['disable_notification'] for m in self.sent))

    def test_failed_sender_retries_committed_intent(self):
        def fail(*a,**k):
            raise OSError('TOKEN must stay private')
        with writer(self.root) as st:
            result=notify(st.db,'Talk','result','answer',sender=fail,now=NOW)
            self.assertEqual(result['status'],'pending')
            self.assertNotIn('TOKEN',str(result))
            flush(st.db,self.send,NOW,{})
            self.assertEqual(st.db.execute('SELECT state FROM outbox').fetchone()[0],'sent')

    def test_cli_inbox_done_and_today_validation(self):
        from contextlib import redirect_stdout
        import io
        from steward.cli import main
        with writer(self.root) as st:
            self.poll(st.db,[self.update(1,'instruction')])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['inbox','--state',str(self.root),'done','1']),0)
            self.assertEqual(main(['notify','--state',str(self.root),'--topic','Today','--kind','result','x'*2000]),2)

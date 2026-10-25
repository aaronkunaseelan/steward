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
                    self.assertFalse(messages.poll(st.db, NOW, {}, fetcher=fail))
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
        db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'result'")
        db.execute('PRAGMA user_version=9')
        db.commit()
        db.close()
        with writer(self.root) as st:
            self.assertEqual(st.db.execute('PRAGMA user_version').fetchone()[0], 18)
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
            notify(st.db,'Changes','created watch',sender=self.send,now=NOW,change_id=cid)
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
        with patch('steward.senses.events', return_value=[]), \
             patch('steward.mind.history', return_value=None), \
             patch('steward.pages.poll', return_value=True), \
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
        # Week gist after the first Sunday–Saturday week (Athens has switched to UTC+2).
        from fixture_mind import TEST_ZONE
        morning = datetime(2026, 10, 26, 7, tzinfo=TEST_ZONE)
        with writer(self.root) as st:
            notify(st.db, 'Today', 'Week gist', 'week:2026-10-18',
                   self.send, morning - timedelta(hours=2))
            self.assertEqual(runtime.gate(st.db, morning), 'orientation')
        runtime.wake(self.root, clock=lambda:morning, settings={'wake_model':False},
                     prompt='test', sender=self.send)
        self.assertEqual(len(self.sent), 2)
        self.assertIn('(fallback)', self.sent[-1][1])
        with writer(self.root) as st:
            from steward.timing import moment_obligation
            self.assertEqual(moment_obligation(st.db, jobs.get(st.db, 'moments', 'orientation'), morning)['state'], 'kept')

    def test_orientation_fallback_and_model_orientation(self):
        runtime.wake(self.root, clock=lambda:ORIENT,settings={'wake_model':False},prompt='test',sender=self.send)
        self.assertEqual(self.sent[0][0],'Today')
        self.assertIn('(fallback)',self.sent[0][1])
        self.assertIn('calendar unknown',self.sent[0][1])
        runtime.wake(self.root, clock=lambda:ORIENT,settings={'wake_model':False},prompt='test',sender=self.send)
        self.assertEqual(len(self.sent),1)
        later=ORIENT+timedelta(days=1)
        def model(argv):
            with writer(self.root) as st:
                notify(st.db,'Today','Current state\nModel orientation',
                       sender=self.send,now=later, moment='orientation')
            return 'run-1'
        runtime.wake(self.root,clock=lambda:later,settings={'wake_model':True},runner=model,prompt='test',sender=self.send)
        self.assertEqual(len(self.sent),2)
        self.assertNotIn('fallback',self.sent[-1][1])

    def test_fallback_has_stable_groups_time_first_and_unknowns_last(self):
        from steward.notify import render
        observed = dict(
            calendars=dict(verdict='fresh', data={'events': [
                dict(start={'dateTime': '2026-10-15T22:00:00Z'}, summary='Late local event'),
                dict(start={'dateTime': '2026-10-16T07:00:00Z'}, summary='Interview & prep')]}),
            gsd=dict(verdict='stale', reason='Refresh has not finished', data={
                'waiting_on_you': [dict(title='Review the reply')], 'waiting_on_others': []}))
        with writer(self.root) as st, patch('steward.messages.senses.snapshot', return_value=observed):
            text = messages.fallback(st.db, NOW)
            markup, visible = render(st.db, text, NOW)
            notify(st.db, 'Today', text, sender=self.send, now=NOW)
        expected = ('Fri 16 Oct · Current state (fallback)\n\nToday\n01:00 Late local event\n10:00 Interview & prep'
                    '\n\nYour list\nLast seen, not current\nReview the reply'
                    '\n\nWaiting\nLast seen, not current\nNo items waiting on others'
                    "\n\nCan't see\nyour list stale: Refresh has not finished")
        self.assertEqual(visible, expected)
        self.assertEqual(self.sent[0][1], markup)
        self.assertTrue(markup.startswith('<b>Fri 16 Oct · Current state (fallback)</b>'))
        self.assertIn('10:00 Interview &amp; prep', markup)
        self.assertNotIn('Compute', visible)
        self.assertNotIn('EEST', visible)

    def test_fallback_keeps_unknowns_when_lists_are_long(self):
        from steward.notify import render, TODAY_CHARACTERS
        observed = dict(calendars=dict(verdict='unknown', reason='Calendar unreadable ' * 30,
            data={'events': []}), gsd=dict(verdict='stale', reason='Refresh overdue ' * 30,
            data={key: [dict(title='Long title ' * 40) for _ in range(10)]
                  for key in ('waiting_on_you', 'waiting_on_others')}))
        with writer(self.root) as st, patch('steward.messages.senses.snapshot', return_value=observed):
            markup, visible = render(st.db, messages.fallback(st.db, NOW), NOW)
        self.assertLessEqual(len(visible), TODAY_CHARACTERS)
        self.assertIn("Can't see\ncalendar unknown:", visible)
        self.assertIn('your list stale:', visible)
        self.assertEqual(markup.count('<b>'), markup.count('</b>'))
        self.assertEqual(sum(line.startswith('Long title') for line in visible.splitlines()), 12)

    def test_moment_changes_and_canary_jobs(self):
        from steward import timing
        with writer(self.root) as st:
            timing.set_moment(st.db, 'orientation', '07:15', '07:45', now=NOW, actor='model')
            jobs.job_verb(st.db, 'add', 'format-canary', dict(justification='Test only',
                argv=[released()], needs={'every': {'min': '1h', 'max': '1d'}}), now=NOW)
            messages.changes(st.db, NOW, self.send, {})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], '<b>Changes</b>\n\n07:15–07:45 orientation · updated\n\nReply &quot;undo&quot; to undo it.')

    def test_today_limit_refuses_detail_before_any_effect(self):
        with writer(self.root) as st:
            accepted = notify(st.db, 'Today', 'x' * 1200, sender=self.send, now=NOW)
            self.assertEqual(accepted['status'], 'sent')
            before = st.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
            with self.assertRaisesRegex(StateError, 'page'):
                notify(st.db, 'Today', 'x' * 1201, sender=self.send, now=NOW)
            self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0], before)
            self.assertEqual(len(self.sent), 1)

    def test_digest_once_told_and_quiet(self):
        with writer(self.root) as st:
            for n in range(3):
                notify(st.db,'Finds','source '+str(n),'url:'+str(n),sender=self.send,now=NOW)
            flush(st.db,self.send,NOW+timedelta(hours=9, minutes=29),{})
            self.assertEqual(self.sent,[])
            flush(st.db,self.send,NOW+timedelta(hours=9, minutes=30),{})
            self.assertEqual(len(self.sent),1)
            self.assertEqual(st.db.execute('SELECT count(*) FROM told').fetchone()[0],3)
            self.assertTrue(self.sent[0][2]['disable_notification'])
            flush(st.db,self.send,NOW+timedelta(hours=10),{})
            self.assertEqual(len(self.sent),1)
            notify(st.db,'Talk','quiet',sender=self.send,now=NOW-timedelta(minutes=1))
            self.assertTrue(self.sent[-1][2]['disable_notification'])

    def test_digest_splits_and_pending_dedup(self):
        with writer(self.root) as st:
            for i in range(3):
                notify(st.db,'Finds','x'*2100,str(i),sender=self.send,now=NOW)
            report=notify(st.db,'Finds','duplicate','0',sender=self.send,now=NOW)
            self.assertEqual(report['status'],'already_told')
            flush(st.db,self.send,NOW+timedelta(hours=10),{})
            self.assertEqual(len(self.sent),3)
            self.assertTrue(all(len(m[1])<=4000 for m in self.sent))

    def test_late_find_waits_for_next_day_close(self):
        from fixture_mind import TEST_ZONE
        athens = TEST_ZONE
        evening = datetime(2026, 10, 16, 23, 35, tzinfo=athens)
        with writer(self.root) as st:
            notify(st.db,'Finds','late find','k-late',sender=self.send,now=evening)
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

    def test_only_requested_compute_changes_are_reported(self):
        with writer(self.root) as st:
            self.poll(st.db, [self.update(1, 'Resume the watch')])
            self.add(st.db)
            jobs.job_verb(st.db, 'pause', 'watch', actor='model', now=NOW)
            jobs.job_verb(st.db, 'resume', 'watch', actor='model', now=NOW, owner_message=1)
            jobs.job_verb(st.db, 'pause', 'watch', actor='maintenance', now=NOW)
            messages.changes(st.db, NOW, self.send, {})
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(len(self.sent), 1)
            text = self.sent[0][1]
            self.assertTrue(text.startswith('<b>Changes</b>\n\nYou: Job add watch'))
            self.assertIn('\n\nJob resume watch\nKeep inputs fresh', text)
            self.assertNotIn('Job pause', text)
            self.assertNotIn('Steward:', text)
            self.assertIn('Reply &quot;undo ID&quot;', text)
            self.poll(st.db, [self.update(9, 'undo', reply=1)])
            self.assertIn('ambiguous', self.sent[-1][1])

    def test_migration_baselines_historical_edits_but_reports_new_owner_changes_once(self):
        with writer(self.root) as st:
            self.add(st.db)
            jobs.job_verb(st.db, 'pause', 'watch', actor='maintenance', now=NOW)
            st.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
            st.db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'result'")
            st.db.execute('PRAGMA user_version=16')
            st.db.commit()
        with writer(self.root) as st:
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(self.sent, [])
            jobs.job_verb(st.db, 'resume', 'watch', actor='owner', now=NOW)
            messages.changes(st.db, NOW, self.send, {})
            messages.changes(st.db, NOW, self.send, {})
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('Maintenance:', self.sent[0][1])
        self.assertIn('You: Job resume watch', self.sent[0][1])
        self.assertNotIn('Job add', self.sent[0][1])

    def test_hidden_bookkeeping_never_becomes_reply_undo_target(self):
        with writer(self.root) as st:
            self.add(st.db)
            with st.db:
                for verb in ('page put', 'page rm', 'interest set', 'interest rm',
                             'memory write', 'block demand add', 'block demand edit',
                             'block demand rm', 'undo block demand', 'mind', 'release',
                             'tick skip', 'external-change', 'inbox done'):
                    jobs.change(st.db, verb, NOW, 'model')
            messages.changes(st.db, NOW, self.send, {})
            messages.changes(st.db, NOW, self.send, {})
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.sent[0][1], '<b>Changes</b>\n\nYou: Job add watch\nKeep inputs fresh\n\nReply &quot;undo&quot; to undo it.')
            self.poll(st.db, [self.update(9, 'undo', reply=1)])
            self.assertIn('Restored watch', self.sent[-1][1])
            self.assertEqual(jobs.get(st.db, 'jobs', 'watch')['state'], 'removed')

    def test_bookkeeping_alone_sends_nothing_and_is_not_recollected(self):
        with writer(self.root) as st:
            with st.db:
                jobs.change(st.db, 'memory write', NOW, 'model')
            messages.changes(st.db, NOW, self.send, {})
            with patch('steward.messages.visible_change', side_effect=AssertionError('recollected')):
                messages.changes(st.db, NOW, self.send, {})
        self.assertEqual(self.sent, [])

    def test_cli_keeps_owner_request_on_job_run_and_undo(self):
        from contextlib import redirect_stdout
        import io
        import json
        import os
        from steward.cli import main
        with writer(self.root) as st:
            self.poll(st.db, [self.update(1, 'Pause the watch, undo it, then plan a run')])
            self.add(st.db)
            messages.changes(st.db, NOW, self.send, {})
        def invoke(argv):
            output = io.StringIO()
            with redirect_stdout(output), patch.dict(os.environ, STEWARD_ACTOR='model'), patch('steward.jobs.now_utc', return_value=NOW):
                self.assertEqual(main([*argv, '--state', str(self.root), '--owner-message', '1']), 0)
            with writer(self.root) as st:
                messages.changes(st.db, NOW, self.send, {})
            return json.loads(output.getvalue())
        paused = invoke(['job', 'pause', 'watch'])
        self.assertIn('\n\nJob pause watch\nKeep inputs fresh', self.sent[-1][1])
        invoke(['undo', paused['change_id']])
        self.assertIn('\n\nUndo watch\nKeep inputs fresh', self.sent[-1][1])
        invoke(['run', 'plan', 'watch', '--at', (NOW + timedelta(hours=1)).isoformat(), '--reason', 'Owner asked'])
        self.assertIn('\n\nRun plan watch\nOwner asked', self.sent[-1][1])
        self.assertNotIn('You:', self.sent[-1][1])

    def test_owner_request_requires_real_inbox_message(self):
        with writer(self.root) as st:
            self.add(st.db)
            with self.assertRaisesRegex(StateError, 'unknown owner message'):
                jobs.job_verb(st.db, 'pause', 'watch', actor='model', owner_message=999)
            self.assertEqual(jobs.get(st.db, 'jobs', 'watch')['state'], 'active')

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
            self.assertEqual({m[1].split(' problem since')[0] for m in self.sent},
                             {'Sol: limit', 'harness: wake'})
            self.assertTrue(all(m[2]['disable_notification'] for m in self.sent))

    def test_failed_sender_retries_committed_intent(self):
        def fail(*a,**k):
            raise OSError('TOKEN must stay private')
        with writer(self.root) as st:
            result=notify(st.db,'Talk','answer',sender=fail,now=NOW)
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
        with writer(self.root) as st:
            self.assertEqual(messages.inbox(st.db), [])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['notify','--state',str(self.root),'--topic','Today','x'*2000]),2)
        self.assertIn('put the detail on a page', output.getvalue())
        with writer(self.root) as st:
            self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

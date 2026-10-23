from fixture_mind import TEST_CONFIG
from fixture_mind import TEST_ZONE
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from fixture_mind import command as released
from unittest.mock import patch

from steward import cli, intent, jobs, messages, notify, senses
from steward.state import writer

NOW = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)  # Midday Athens.
TICKS = dict(window_h=24, total=5, idle=1, launched=1, woke=2, skipped=1)


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from fixture_mind import install_release
        install_release(self, self.root)
        self.state = self.root / 'state'
        self.config = self.root / 'config.toml'
        self.config.write_text(TEST_CONFIG + 'interrupt_cap = 1\n')
        self.data = self.root / 'data'
        self.message_id = 0
        guard = patch.dict(os.environ, STEWARD_DATA=str(self.data))
        guard.start()
        self.addCleanup(guard.stop)
        with writer(self.state, initialize=True):
            pass
        guard = patch('steward.notify.api', side_effect=AssertionError('outward calls forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def doc(self, settings=None, now=NOW):
        with patch('steward.intent.ticks', return_value=TICKS):
            return intent.document(self.state, settings or {}, now)

    def block(self, doc, kind):
        return next(b for b in doc['blocks'] if b['kind'] == kind)

    def add_job(self, db, targets):
        result = jobs.job_verb(db, 'add', 'gsd-steady', dict(
            justification='Keep the obligations fresh.', argv=[released()],
            needs={'ready_by': targets}, max_minutes=120, heavy=True), now=NOW - timedelta(days=1))
        IntentTests.measure(self, db, 'gsd-steady', NOW - timedelta(days=1) + timedelta(minutes=1))
        return result

    def measure(self, db, name, at):
        jobs.append(db, 'runs', dict(run_id='measured-' + name, job=name, state='succeeded',
                    planned_for=at.isoformat(), started_at=at.isoformat(), ended_at=at.isoformat(),
                    reason='First measured run.', spec_json=json.dumps(dict(argv=[released()]))),
                    jobs.change(db, 'fixture', at))
        db.commit()

    def send(self, db, interrupt=None, sender=None, topic='Talk', **options):
        def deliver(*args, **kwargs):
            self.message_id += 1
            return self.message_id
        return notify.notify(db, topic, 'result', 'PRIVATE MESSAGE BODY', sender=sender or deliver,
                             now=NOW, interrupt=interrupt, settings={'interrupt_cap': 1}, **options)

    def save_healthy(self, db, now):
        self.add_job(db, ['00:01'])
        cid = jobs.change(db, 'fixture', now, 'tick')
        target = now.astimezone(TEST_ZONE).replace(hour=0, minute=1, second=0, microsecond=0).isoformat()
        jobs.append(db, 'runs', dict(run_id='fixture-good', job='gsd-steady', state='succeeded',
                    planned_for=target, started_at=target, ended_at=target,
                    reason='Fixture success.', spec_json=json.dumps(dict(argv=[released()]))), cid)
        readings = {name: dict(status='ok', data={'events': [], 'digest': 'fixture'})
                    for name in senses.INTERVALS if name not in ('health', 'place')}
        readings['gsd']['data']['cut'] = dict(until=now.isoformat(), committed_at=now.isoformat())
        readings['qmd']['data'].update(alive=True, sources={'gmail': dict(
            cadence_s=900, fresh_through=now.timestamp(), current=True, valid_until=now.timestamp() + 86400)})
        readings['sol']['data']['observed_at'] = now.isoformat()
        senses.save(db, readings, now, cid)

    def test_unfolded_completion_keeps_deadline_without_writing_state(self):
        from steward import runtime
        now = NOW.replace(hour=3)  # 06:00 Athens, exactly the promise tick.
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            self.save_healthy(st.db, now)
            jobs.job_verb(st.db, 'add', 'consolidate', dict(
                justification='Keep memory fresh.', argv=[released()],
                needs={'ready_by': ['06:00']}), now=now - timedelta(days=1))
            self.measure(st.db, 'consolidate', now - timedelta(days=1) + timedelta(minutes=1))
            cid = jobs.change(st.db, 'fixture', now, 'tick')
            row = dict(run_id='unfolded', job='consolidate', state='started',
                       planned_for=(now - timedelta(minutes=20)).isoformat(),
                       started_at=(now - timedelta(minutes=20)).isoformat(),
                       reason='Fixture run.', spec_json=json.dumps(dict(argv=[released()])))
            jobs.append(st.db, 'runs', row, cid)
            st.db.commit()
        path = runtime.manifest_path(self.data, row)
        path.parent.mkdir(parents=True)
        manifest = dict(run_id='unfolded', job='consolidate', started_at=row['started_at'],
                        ended_at=(now - timedelta(minutes=18)).isoformat(),
                        exit_code=0, status='succeeded')
        path.write_text(json.dumps(manifest))
        before = (self.state / 'state.sqlite').read_bytes()
        doc = self.doc(now=now)
        self.assertEqual(doc['state'], 'green', doc['headline'])
        promise = next(p for p in self.block(doc, 'day')['promises'] if p['label'] == 'consolidate')
        self.assertEqual((promise['state'], promise['met_at']), ('kept', '05:42'))
        run = next(r for r in self.block(doc, 'runs')['items'] if r['id'] == 'run-unfolded')
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual((self.state / 'state.sqlite').read_bytes(), before)
        for changes in ({'job': 'other'}, {'exit_code': True},
                        {'ended_at': (now + timedelta(minutes=1)).isoformat()},
                        {'ended_at': (now - timedelta(minutes=21)).isoformat()}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps(dict(manifest, **changes)))
                doc = self.doc(now=now)
                self.assertEqual(doc['state'], 'red')
                self.assertIn('consolidate missed 06:00', doc['headline'])

    def test_unfolded_gsd_completion_also_updates_chain_and_failure_reason(self):
        from steward import runtime
        now = NOW.replace(hour=15, minute=30)  # 18:30 Athens.
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            self.save_healthy(st.db, now)
            jobs.job_verb(st.db, 'edit', 'gsd-steady', dict(
                justification='Keep the obligations fresh.', argv=[released()],
                needs={'ready_by': ['18:30']}, max_minutes=120, heavy=True),
                now=now - timedelta(days=1))
            st.db.execute("INSERT INTO told VALUES ('orientation:2026-10-17', '2026-10-17T04:00:00+00:00', NULL, NULL, 'Today', 1)")
            row = dict(run_id='unfolded-gsd', job='gsd-steady', state='started',
                       planned_for=(now - timedelta(minutes=20)).isoformat(),
                       started_at=(now - timedelta(minutes=20)).isoformat(),
                       reason='Fixture run.', spec_json=json.dumps(dict(argv=[released()])))
            jobs.append(st.db, 'runs', row, jobs.change(st.db, 'fixture', now, 'tick'))
            st.db.commit()
        path = runtime.manifest_path(self.data, row)
        path.parent.mkdir(parents=True)
        manifest = dict(run_id=row['run_id'], job=row['job'], started_at=row['started_at'],
                        ended_at=(now - timedelta(minutes=18)).isoformat(),
                        exit_code=0, status='succeeded')
        path.write_text(json.dumps(manifest))
        doc = self.doc(now=now)
        self.assertEqual(doc['state'], 'green', doc['headline'])
        self.assertFalse(any(h['warn'] for h in self.block(doc, 'chain')['hops']))
        manifest.update(exit_code=1, status='failed')
        path.write_text(json.dumps(manifest))
        (path.parent / 'stderr.txt').write_text('fixture failure')
        doc = self.doc(now=now)
        self.assertEqual(doc['state'], 'red')
        self.assertIn('gsd-steady missed 18:30', doc['headline'])
        self.assertIn('latest run failed: exit 1', doc['headline'])
        self.assertIn('fixture failure', json.dumps(self.block(doc, 'runs')))

    def test_missed_promise_is_the_red_headline_reason(self):
        now = NOW.replace(hour=3)
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            self.save_healthy(st.db, now)
            jobs.job_verb(st.db, 'add', 'consolidate', dict(
                justification='Keep memory fresh.', argv=[released()],
                needs={'ready_by': ['06:00']}), now=now - timedelta(days=1))
            self.measure(st.db, 'consolidate', now - timedelta(days=1) + timedelta(minutes=1))
        doc = self.doc(now=now)
        self.assertEqual(doc['state'], 'red')
        self.assertIn('consolidate missed 06:00', doc['headline'])

    def test_inflight_wake_is_running_and_judges_last_finished_model(self):
        now = NOW.replace(hour=2)
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            self.save_healthy(st.db, now)
            good = json.dumps(dict(model_calls=1, ended_at=now.isoformat(), briefing=dict(complete=True)))
            st.db.execute("INSERT INTO wakes(id,started_at,status,checkpoint,harness_run_id) VALUES (1,?,'completed',?,'model-1')", (now.isoformat(), good))
            st.db.execute("INSERT INTO wakes(id,started_at,status,checkpoint) VALUES (2,?,'gated','{}')", (now.isoformat(),))
            st.db.commit()
        doc = self.doc(now=now)
        self.assertEqual(doc['state'], 'green')
        self.assertEqual(next(r for r in self.block(doc, 'runs')['items'] if r['id'] == 'wake-2')['status'], 'running')
        with writer(self.state) as st:
            # A skipped wake ('nothing new') still wrote its complete briefing before the gate.
            st.db.execute("UPDATE wakes SET status='skipped',checkpoint=? WHERE id=2",
                          (json.dumps(dict(model_calls=0, ended_at=now.isoformat(), briefing=dict(complete=True))),))
            st.db.commit()
        self.assertEqual(self.doc(now=now)['state'], 'green')
        with writer(self.state) as st:
            st.db.execute("UPDATE wakes SET status='failed',harness_run_id='model-2' WHERE id=2")
            st.db.commit()
        self.assertIn('newest wake briefing incomplete', self.doc(now=now)['headline'])

    def test_owner_suggestion_counts_as_edit_without_permanent_state(self):
        with writer(self.state) as store:
            for version, actor in ((1, 'model'), (2, 'tick')):
                cid = jobs.change(store.db, 'tick' if actor == 'tick' else 'block choose', NOW, actor)
                payload = dict(start=(NOW + timedelta(minutes=version)).isoformat(),
                               end=(NOW + timedelta(hours=1)).isoformat(), title='Movement')
                store.db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?)',
                                 ('movement:once:0', version, cid, 'movement', 'applied', json.dumps(payload)))
            store.db.commit()
        doc = self.doc()
        self.assertEqual(self.block(doc, 'learned')['edits'], 1)
        block = next(item for item in self.block(doc, 'day')['yours'] if item['label'] == 'movement')
        self.assertEqual(block['kind'], 'block')
        self.assertNotIn('stays put', block['why'])

    def test_oversize_page_fails_the_wake_and_intent_says_so_until_a_good_one(self):
        from steward import memory, runtime
        from fixture_mind import install
        install(self, self.root)
        now = NOW.replace(hour=2)
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            self.save_healthy(st.db, now)
            st.db.commit()
        original = runtime.snapshot
        def noisy(*args, **kwargs):
            return dict(original(*args, **kwargs), jobs=[{'justification': 'x'*200000}])
        with patch.object(runtime, 'snapshot', side_effect=noisy):
            with self.assertRaisesRegex(Exception, 'briefing page exceeds'):
                runtime.wake(self.state, clock=lambda: now, settings={}, prompt='test', sender=lambda *a, **k: 1)
        doc = self.doc(now=now)
        # The model is never called on an incomplete briefing, so the owner got no wake: say so.
        self.assertNotEqual(doc['state'], 'green')
        self.assertIn('newest wake briefing incomplete', doc['headline'])
        with writer(self.state) as st:
            saved = json.loads(st.db.execute('SELECT checkpoint FROM wakes ORDER BY id DESC LIMIT 1').fetchone()[0])
            self.assertFalse(saved['briefing']['complete'])
            self.assertGreater(saved['briefing']['bytes'], memory.BRIEFING_BYTES)
        runtime.wake(self.state, clock=lambda: now, settings={}, prompt='test', sender=lambda *a, **k: 1)
        self.assertEqual(self.doc(now=now)['state'], 'green')
        self.assertNotIn('wake briefed without', self.doc(now=now)['headline'])

    def test_headline_follows_included_sense_hops(self):
        now = NOW.replace(hour=2)
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
        doc = self.doc(now=now)
        self.assertTrue(self.block(doc, 'chain')['hops'][0]['warn'])
        self.assertEqual(doc['state'], 'red')
        self.assertIn('calendars', doc['headline'])
        with writer(self.state) as st:
            self.save_healthy(st.db, now)
            st.db.commit()
        self.assertEqual(self.doc(now=now)['state'], 'green')

    def test_inbound_poll_staleness_in_intent_and_state(self):
        now = NOW.replace(hour=2)  # All promises still due: isolate poll health.
        with writer(self.state) as st:
            self.save_healthy(st.db, now)
            st.db.commit()
        self.assertEqual(self.doc(now=now)['state'], 'red')
        self.assertIn('never succeeded', self.doc(now=now)['headline'])
        with writer(self.state) as st:
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
        for age, status in ((0, 'green'), (59, 'green'), (60, 'green'), (60.1, 'red')):
            with self.subTest(minutes=age):
                at = now + timedelta(minutes=age)
                # Isolate inbound poll age while the other readers keep refreshing.
                with writer(self.state) as st:
                    readings = {name: dict(status=sense['status'], data=sense.get('data', {}))
                                for name, sense in senses.snapshot(st.db, at).items() if sense['status'] == 'ok'}
                    senses.save(st.db, readings, at, jobs.change(st.db, 'fixture', at, 'tick'))
                    st.db.commit()
                doc = self.doc(now=at)
                self.assertEqual(doc['state'], status)
                hop = self.block(doc, 'chain')['hops'][1]
                self.assertEqual(hop['warn'], status == 'red')
                with writer(self.state) as st:
                    health = jobs.snapshot(st.db, now=at)['inbound_poll']
                self.assertEqual(health['status'], 'unknown' if status == 'red' else 'ok')
                if status == 'red':
                    self.assertIn('over 60 min', hop['note'])
                    self.assertIn(health['reason'], doc['headline'])

    def test_attention_uses_actual_interrupt_not_midday_or_overflow(self):
        with writer(self.state) as st:
            self.send(st.db)
            self.send(st.db, 'The owner must act before the booking expires.')
            self.send(st.db, 'This would exceed the cap.')
        doc = self.doc({'interrupt_cap': 1, 'quiet_hours': ['00:00', '24:00']})
        day = self.block(doc, 'day')
        self.assertEqual([a['buzz'] for a in day['attention']], [False, True, False])
        self.assertEqual(day['buzz_cap'], 1)
        self.assertEqual(day['quiet'], [])
        interrupted = self.block(doc, 'funnel')['steps'][-1]
        self.assertEqual(interrupted[:2], ['interrupted', 1])
        self.assertIn('within the daily cap', interrupted[2])
        self.assertNotIn('not built', json.dumps(doc))
        self.assertIn('1 interruptions', doc['headline'])

    def test_digest_items_count_as_one_delivered_message(self):
        delivered = []
        with writer(self.state) as st:
            for key in ('one', 'two'):
                notify.notify(st.db, 'Finds', 'result', 'Synthetic finding', told_key=key,
                              deliver='digest', now=NOW - timedelta(hours=3))
            notify.flush(st.db, lambda *a, **k: delivered.append(a) or 99, NOW.replace(hour=16), {})
            self.assertEqual(st.db.execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0], 2)
        self.assertEqual(len(delivered), 1)
        doc = self.doc()
        self.assertEqual(len(self.block(doc, 'day')['attention']), 1)
        self.assertEqual(self.block(doc, 'funnel')['steps'][3][1], 1)
        self.assertFalse(self.block(doc, 'day')['attention'][0]['buzz'])

    def test_failed_attempt_then_silent_retry_is_not_a_buzz_even_at_same_timestamp(self):
        def fail(*args, **options):
            raise OSError('unknown delivery outcome')
        with writer(self.state) as st:
            self.send(st.db, 'Act before the deadline.', sender=fail)
            notify.flush(st.db, lambda *a, **k: 10, NOW, {})
        self.assertFalse(self.block(self.doc(), 'day')['attention'][0]['buzz'])

    def test_gsd_promises_follow_current_ready_by_and_ignore_dry_runs(self):
        targets = ['07:10', '19:20']
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30', '18:30'])
            spec = json.loads(jobs.get(st.db, 'jobs', 'gsd-steady')['spec_json'])
            spec['needs'] = {'ready_by': targets}
            jobs.job_verb(st.db, 'edit', 'gsd-steady', spec, now=NOW)
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                for job, end in [('gsd-step-dry', '2026-10-17T03:00:00+00:00'),
                                 ('gsd-steady', '2026-10-17T15:30:00+00:00')]:
                    jobs.append(st.db, 'runs', dict(run_id=job, job=job, state='succeeded', planned_for=end,
                                reason='Fixture placement.', started_at=end, ended_at=end, spec_json=json.dumps(dict(argv=[released()]))), cid)
        promises = self.block(self.doc(now=NOW.replace(hour=17)), 'day')['promises']
        gsd = [p for p in promises if p['label'] == 'gsd-steady']
        self.assertEqual([p['at'] for p in gsd], targets)
        self.assertEqual([p['state'] for p in gsd], ['recovered', 'kept'])
        self.assertNotIn('dry run', json.dumps(gsd))
        self.assertEqual(gsd[1]['met_at'], '18:30')

    def test_later_kept_gsd_promise_recovers_miss_without_counting_it_as_kept(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['18:30', '06:30'])  # Input order is not chronology.
            messages.poll(st.db, NOW.replace(hour=16), {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                end = '2026-10-17T14:47:00+00:00'  # 17:47 Athens.
                jobs.append(st.db, 'runs', dict(run_id='recovery', job='gsd-steady', state='succeeded',
                            planned_for=end, started_at=end, ended_at=end,
                            reason='Fixture success.', spec_json=json.dumps(dict(argv=[released()]))), cid)
            self.send(st.db, topic='Today', told_key='orientation:2026-10-17')
            with st.db:
                st.db.execute("UPDATE outbox SET sent_at='2026-10-17T04:00:00+00:00'")
                st.db.execute("UPDATE told SET at='2026-10-17T04:00:00+00:00'")
                senses.save(st.db, {name: dict(status='ok', data={'events': [], 'digest': 'fixture'}) for name in senses.INTERVALS
                                   if name not in ('health', 'place')}, NOW.replace(hour=15),
                            jobs.change(st.db, 'fixture', NOW, 'owner'))
        doc = self.doc(now=NOW.replace(hour=15))  # Day close is still due.
        gsd = [p for p in self.block(doc, 'day')['promises'] if p['label'] == 'gsd-steady']
        self.assertEqual(gsd[0], dict(at='06:30', label='gsd-steady', state='recovered', met_at='17:47',
                                    why='Missed at 06:30; recovered: Finished at 17:47 EEST.'))
        self.assertEqual(gsd[1]['state'], 'kept')
        # Recovery no longer depends on keeping a later target; incomplete cut facts remain unknown.
        self.assertEqual(doc['state'], 'red')
        self.assertNotIn('gsd ready_by missed', doc['headline'])
        self.assertIn('gsd unknown', doc['headline'])

    def test_single_late_success_recovers_and_is_not_counted_kept(self):
        now = NOW.replace(hour=17)
        with writer(self.state) as st:
            self.save_healthy(st.db, now)
            spec = json.loads(jobs.get(st.db, 'jobs', 'gsd-steady')['spec_json'])
            spec['needs'] = {'ready_by': ['18:30']}
            jobs.job_verb(st.db, 'edit', 'gsd-steady', spec, now=now)
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            with st.db:
                cid = jobs.change(st.db, 'fixture', now)
                jobs.append(st.db, 'runs', dict(run_id='late', job='gsd-steady', state='succeeded',
                            planned_for=now.isoformat(), started_at=NOW.replace(hour=15).isoformat(),
                            ended_at=NOW.replace(hour=16, minute=23).isoformat(), reason='Late success.', spec_json=json.dumps(dict(argv=[released()]))), cid)
                for name, at in [('orientation', '04:00'), ('day_close', '16:05')]:
                    st.db.execute('INSERT INTO told VALUES (?, ?, NULL, NULL, ?, 1)',
                                  (name + ':2026-10-17', '2026-10-17T' + at + ':00+00:00', 'Today'))
        doc = self.doc(now=now)
        promises = self.block(doc, 'day')['promises']
        self.assertEqual([p['state'] for p in promises], ['kept', 'recovered', 'recovered'])
        self.assertIn('kept 1 promises', doc['headline'])
        self.assertEqual(doc['state'], 'green')
        with writer(self.state) as st:
            with st.db:
                jobs.append(st.db, 'runs', dict(run_id='later-failed', job='gsd-steady', state='failed',
                            planned_for=now.isoformat(), started_at=now.isoformat(), ended_at=now.isoformat(),
                            reason='Latest failure.', spec_json=json.dumps(dict(argv=[released()]))), jobs.change(st.db, 'fixture', now))
        doc = self.doc(now=now)
        self.assertEqual(next(p['state'] for p in self.block(doc, 'day')['promises']
                              if p['label'] == 'gsd-steady'), 'recovered')
        self.assertEqual(doc['state'], 'red')
        self.assertIn('latest run failed', doc['headline'])

    def test_ready_by_before_the_job_existed_or_before_a_resume_is_not_owed(self):
        # F-59: a job added at midday is not red for a target that passed before it existed.
        spec = dict(justification='Advisories before the day starts.', argv=[released()], needs={'ready_by': ['06:40', '18:40']})
        with writer(self.state) as st:
            jobs.job_verb(st.db, 'add', 'advisories', spec, now=NOW)  # 12:00 Athens; 06:40 already passed
            self.measure(st.db, 'advisories', NOW)
        mine = [p for p in self.block(self.doc(), 'day')['promises'] if p['label'] == 'advisories']
        self.assertEqual([(p['at'], p['state']) for p in mine], [('18:40', 'due')])
        with writer(self.state) as st:
            jobs.job_verb(st.db, 'edit', 'advisories', spec, now=NOW + timedelta(hours=1))  # an edit is not an activation
        mine = [p for p in self.block(self.doc(now=NOW + timedelta(hours=8)), 'day')['promises'] if p['label'] == 'advisories']
        self.assertEqual([(p['at'], p['state']) for p in mine], [('18:40', 'missed')])
        with writer(self.state) as st:
            jobs.job_verb(st.db, 'pause', 'advisories', now=NOW + timedelta(hours=2))
            jobs.job_verb(st.db, 'resume', 'advisories', now=NOW + timedelta(hours=7))  # 19:00 Athens, after 18:40
        mine = [p for p in self.block(self.doc(now=NOW + timedelta(hours=8)), 'day')['promises'] if p['label'] == 'advisories']
        self.assertEqual(mine, [])

    def test_miss_without_later_kept_same_label_stays_red(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30', '18:30'])
        doc = self.doc(now=NOW.replace(hour=16))
        gsd = [p for p in self.block(doc, 'day')['promises'] if p['label'] == 'gsd-steady']
        self.assertEqual([p['state'] for p in gsd], ['missed', 'missed'])
        self.assertIsNone(gsd[0]['met_at'])
        self.assertEqual(doc['state'], 'red')

    def test_due_and_quiet_promises_do_not_recover_miss(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30', '18:30'])
        doc = self.doc()
        promises = self.block(doc, 'day')['promises']
        gsd = [p for p in promises if p['label'] == 'gsd-steady']
        self.assertEqual([p['state'] for p in gsd], ['missed', 'due'])
        self.assertEqual(next(p['state'] for p in promises if p['label'] == 'Day close'), 'due')
        self.assertEqual(doc['state'], 'red')

    def test_later_kept_different_label_does_not_recover_miss(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30'])
            self.send(st.db, topic='Finds', deliver='immediate')
            with st.db:
                st.db.execute("UPDATE outbox SET sent_at='2026-10-17T07:15:00+00:00'")
        doc = self.doc()
        promises = self.block(doc, 'day')['promises']
        self.assertEqual(next(p['state'] for p in promises if p['label'] == 'gsd-steady'), 'missed')
        self.assertEqual(next(p['state'] for p in promises if p['label'] == 'Day close'), 'due')
        self.assertEqual(doc['state'], 'red')

    def test_ritual_promises_use_shared_times_and_configured_wake_minutes(self):
        promises = self.block(self.doc(), 'day')['promises']
        self.assertEqual([(p['label'], p['at']) for p in promises],
                         [('Orientation', '07:45'), ('Day close', '19:00')])
        doc = self.doc({'wake_times': ['01:00', '07:20', '20:40']})
        promises = self.block(doc, 'day')['promises']
        self.assertEqual([(p['label'], p['at']) for p in promises],
                         [('Orientation', '07:45'), ('Day close', '19:00')])

    def test_today_reply_after_day_close_start_leaves_close_due(self):
        now = NOW.replace(hour=15, minute=40)
        with writer(self.state) as st:
            notify.notify(st.db, 'Today', 'result', 'Reply, not the close',
                          sender=lambda *a, **options: 1, now=now)
        promises = self.block(self.doc(now=now), 'day')['promises']
        self.assertEqual(next(p['state'] for p in promises if p['label'] == 'Day close'), 'due')

    def test_day_close_late_is_missed_never_quiet(self):
        def close():
            promises = self.block(self.doc(now=NOW.replace(hour=17)), 'day')['promises']
            return next(p for p in promises if p['label'] == 'Day close')
        self.assertEqual(close()['state'], 'missed')
        with writer(self.state) as st:
            self.send(st.db, topic='Today', deliver='immediate')
            with st.db:
                st.db.execute("UPDATE outbox SET sent_at='2026-10-17T16:05:00+00:00'")
        self.assertEqual(close()['state'], 'missed')
        self.assertIsNone(close()['met_at'])
        with writer(self.state) as st:
            with st.db:
                st.db.execute("UPDATE outbox SET sent_at='2026-10-17T15:45:00+00:00'")
                st.db.execute("INSERT INTO told VALUES ('day_close:2026-10-17', '2026-10-17T15:45:00+00:00', NULL, NULL, 'Today', 1)")
        self.assertEqual(close()['state'], 'kept')

    def test_chain_warns_on_unknown_and_reports_oldest_sense_age(self):
        with writer(self.state) as st:
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                self.save_healthy(st.db, NOW - timedelta(minutes=10))
                senses.save(st.db, {'gsd': {'status': 'unknown', 'reason': 'PRIVATE SOURCE ERROR'}}, NOW - timedelta(seconds=12), cid)
        hop = self.block(self.doc(), 'chain')['hops'][0]
        self.assertEqual(hop, dict(name='Steward senses', lag_s=600, note='gsd unknown: PRIVATE SOURCE ERROR', warn=True))
        self.assertEqual(self.block(self.doc(), 'chain')['layer'], 'process')
        with writer(self.state) as st:
            with st.db:
                senses.save(st.db, {'gsd': {'status': 'ok', 'data': {'digest': 'fixture', 'cut': dict(until=NOW.isoformat(), committed_at=NOW.isoformat())}}}, NOW, cid)
        hop = self.block(self.doc(), 'chain')['hops'][0]
        self.assertFalse(hop['warn'])
        self.assertEqual(hop['lag_s'], 600)

    def test_unobserved_senses_and_sol_are_unknown_not_healthy_zeroes(self):
        doc = self.doc()
        hop = self.block(doc, 'chain')['hops'][0]
        self.assertTrue(hop['warn'])
        self.assertIsNone(hop['lag_s'])
        self.assertIn('gsd', hop['note'])
        meter = self.block(doc, 'meters')['items'][0]
        self.assertIsNone(meter['used'])
        self.assertIsNone(meter['pace'])
        with writer(self.state) as st:
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                senses.save(st.db, {'sol': {'status': 'ok', 'data': {'used_percent': 42,
                    'window_elapsed_fraction': 0.5, 'resets_at': '2026-10-22T00:00:00+00:00'}}}, NOW - timedelta(minutes=5), cid)
                senses.save(st.db, {'sol': {'status': 'unknown', 'reason': 'read failed'}}, NOW, cid)
        meter = self.block(self.doc(), 'meters')['items'][0]
        self.assertEqual(meter, dict(label='Sol weekly pool', used=None, pace=None, resets=None))

    def test_counts_use_model_attempts_configured_cap_and_only_page_two_senses(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30'])
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                for name in senses.INTERVALS:
                    senses.save(st.db, {name: {'status': 'unknown', 'reason': 'Synthetic failure'}}, NOW, cid)
                for status, calls in [('completed', 1), ('failed', 1), ('skipped', 0), ('idle', 0)]:
                    st.db.execute('INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,?,?)',
                                  (NOW.isoformat(), status, json.dumps({'model_calls': calls})))
        doc = self.doc()
        steps = self.block(doc, 'funnel')['steps']
        self.assertEqual(steps[0][1], 5)  # Health/place are intentionally not Page 2 signals.
        self.assertEqual(steps[1][1], 2)  # Failed model attempts still consumed a wake.
        self.assertEqual(self.block(doc, 'chips')['cap'], 8)

    def test_privacy_and_string_limits(self):
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30'])
            self.send(st.db, topic='Changes')
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                events = [dict(calendar='main', start={'dateTime': NOW.isoformat()},
                               end={'dateTime': (NOW + timedelta(hours=1)).isoformat()},
                               summary='PRIVATE EVENT TITLE', eventType=event_type) for event_type in ('default', 'outOfOffice')]
                senses.save(st.db, {'calendars': {'status': 'ok', 'data': {'events': events}}}, NOW, cid)
                st.db.execute('INSERT INTO memories VALUES (?,?,?,?,?)', ('lessons', 'lessons', 1, cid, '- PRIVATE LESSON TEXT\n* Another private lesson'))
                spec = json.dumps(dict(kind='gym', count=3, minutes=90, transparency='transparent', justification='j' * 500))
                st.db.execute('INSERT INTO demands VALUES (?,?,?,?,?)', ('gym', 1, cid, spec, 'active'))
        doc = self.doc()
        text = json.dumps(doc)
        for private in ('PRIVATE MESSAGE BODY', 'PRIVATE EVENT TITLE', 'PRIVATE LESSON TEXT'):
            self.assertNotIn(private, text)
        self.assertEqual([b['label'] for b in self.block(doc, 'day')['yours']], ['Busy', 'Out of office'])
        self.assertEqual(self.block(doc, 'learned')['lessons'], 2)
        def check(value):
            if isinstance(value, str):
                self.assertLessEqual(len(value), 200)
            elif isinstance(value, dict):
                for v in value.values():
                    check(v)
            elif isinstance(value, list):
                for v in value:
                    check(v)
        check(doc)
        self.assertLessEqual(len(doc['headline']), 160)

    def test_owner_deleted_block_is_not_planned_time_and_counts_as_learning(self):
        from steward import calendar_b
        with writer(self.state) as st:
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                payload = dict(start=NOW.isoformat(), end=(NOW + timedelta(hours=1)).isoformat(), title='Synthetic block')
                st.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)',
                              ('gym:once:0', 1, cid, 'gym', 'applied', json.dumps(payload)))
                calendar_b.reconcile(st.db, {'events': [], 'missing_event_keys': ['gym:once:0'],
                    'from': (NOW - timedelta(hours=1)).isoformat(),
                    'to': (NOW + timedelta(hours=2)).isoformat()},
                    {'schedule_calendar_id': 'schedule', 'main_calendar_ids': ['main']}, cid)
        doc = self.doc()
        self.assertEqual(self.block(doc, 'day')['yours'], [])
        self.assertEqual(self.block(doc, 'learned')['edits'], 1)

    def test_cli_reads_only_own_database_config_and_journal_without_locks(self):
        connects = []
        original = sqlite3.connect
        before = (self.state / 'state.sqlite').read_bytes()
        def connect(path, **options):
            connects.append(str(path))
            self.assertEqual(str(path), (self.state / 'state.sqlite').as_uri() + '?mode=ro')
            self.assertTrue(options['uri'])
            return original(path, **options)
        calls = []
        def journal(argv, **options):
            calls.append(argv)
            self.assertEqual(argv[:4], ['journalctl', '--user', '-u', 'steward-tick.service'])
            self.assertLessEqual(options['timeout'], 1)
            return subprocess.CompletedProcess(argv, 0, '{}\n', '')
        output = io.StringIO()
        with patch('steward.intent.sqlite3.connect', side_effect=connect), \
             patch('steward.intent.subprocess.run', side_effect=journal), \
             patch('steward.cli.writer', side_effect=AssertionError('writer forbidden')), \
             patch('steward.mind.directory', side_effect=AssertionError('mind reads forbidden')), \
             patch('steward.senses.run', side_effect=AssertionError('producer calls forbidden')), \
             redirect_stdout(output):
            self.assertEqual(cli.main(['intent', '--state', str(self.state / 'state.sqlite'),
                                       '--config', str(self.config), '--json']), 0)
        self.assertEqual(len(connects), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(output.getvalue())['blocks'][0]['buzz_cap'], 1)
        self.assertEqual((self.state / 'state.sqlite').read_bytes(), before)

    def test_unreadable_state_or_config_has_empty_stdout_and_nonzero_exit(self):
        corrupt = self.root / 'corrupt.sqlite'
        corrupt.write_bytes(b'not sqlite')
        for state in (self.root / 'missing.sqlite', corrupt):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = cli.main(['intent', '--state', str(state), '--config', str(self.config), '--json'])
            self.assertNotEqual(result, 0)
            self.assertEqual(output.getvalue(), '')
        self.assertFalse((self.root / 'missing.sqlite').exists())
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(['intent', '--state', str(self.state), '--config', str(self.root / 'missing.toml'), '--json']), 0)
        self.assertEqual(output.getvalue(), '')

    def test_ticks_outcomes_and_bounded_journal_failure(self):
        rows = ['human log line', '{}', '{"started":["run"]}', '{"wake":"event"}',
                '{"scheduled_wake":true}', '{"skipping":{"reason":"fixture"}}', '{partial']
        def journal(*args, **options):
            return subprocess.CompletedProcess(args, 0, '\n'.join(rows), '')
        self.assertEqual(intent.ticks(NOW, journal), dict(window_h=24, total=5, idle=3, launched=1, woke=0, skipped=1))
        def timeout(*args, **options):
            raise subprocess.TimeoutExpired(args[0], 1)
        result = intent.ticks(NOW, timeout)
        self.assertEqual(result['total'], 0)

    def test_gated_launch_is_launched_failed_model_is_woke_and_run_skip_is_not_tick_skip(self):
        base = NOW - timedelta(hours=1)
        rows = [dict(scheduled_wake=True, started=['run']), dict(wake='event'),
                dict(skipped=['host-load-run']), dict(skipping=NOW.isoformat()), dict(status='writer_busy')]
        entries = [dict(MESSAGE=json.dumps(row), __REALTIME_TIMESTAMP=str(int(
            (base + timedelta(minutes=10*i)).timestamp() * 1_000_000))) for i, row in enumerate(rows)]
        # A runner returning extra history must not change the advertised 24 h window.
        entries += [dict(MESSAGE='{}', __REALTIME_TIMESTAMP=str(int(
            (NOW - timedelta(hours=25)).timestamp() * 1_000_000)))]
        with writer(self.state) as st:
            with st.db:
                st.db.execute('INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,?,?)',
                              ((base + timedelta(minutes=10)).isoformat(), 'failed', '{"model_calls":1}'))
            def journal(*args, **options):
                return subprocess.CompletedProcess(args, 0, '\n'.join(map(json.dumps, entries)), '')
            self.assertEqual(intent.ticks(NOW, journal, db=st.db),
                             dict(window_h=24, total=5, idle=1, launched=1, woke=1, skipped=2))

    def test_ticks_count_model_event_wakes_not_gated_and_repair_old_lines(self):
        base = NOW - timedelta(hours=2)
        ticks = [dict(wake='event'), dict(wake='scheduled', scheduled_wake=True, started=['synthetic-run']),
                 dict(scheduled_wake=False), dict(wake=None)]
        envelopes = [json.dumps(dict(MESSAGE=json.dumps(t), __REALTIME_TIMESTAMP=str(int(
            (base + timedelta(minutes=20*i)).timestamp() * 1_000_000)))) for i, t in enumerate(ticks)]
        with writer(self.state) as st:
            with st.db:
                for i in (0, 1, 2):
                    st.db.execute('INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,?,?,?)',
                                  ((base + timedelta(minutes=20*i + 1)).isoformat(),
                                   'skipped' if i == 1 else 'completed', '{}', None if i == 1 else 'synthetic-tty'))
            def journal(*args, **options):
                self.assertIn('json', args[0])
                return subprocess.CompletedProcess(args, 0, '\n'.join(envelopes), '')
            self.assertEqual(intent.ticks(NOW, journal, db=st.db),
                             dict(window_h=24, total=4, idle=1, launched=1, woke=2, skipped=0))

    def test_schedule_and_runs_exact_keys_order_limits_and_unknowns(self):
        from steward import archive
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30', '18:30'])
            with st.db:
                cid = jobs.change(st.db, 'fixture', NOW)
                for i in range(105):
                    at = NOW - timedelta(minutes=i)
                    jobs.append(st.db, 'runs', dict(run_id=f'{i:032x}', job='gsd-steady', state='succeeded',
                                planned_for=at.isoformat(), reason='A Steward-authored reason ' + 'x' * 300,
                                started_at=(at - timedelta(minutes=10)).isoformat(), ended_at=at.isoformat(),
                                spec_json=json.dumps(dict(argv=[released()])), cost_minutes=10), cid)
                jobs.append(st.db, 'runs', dict(run_id='f' * 32, job='gsd-steady', state='succeeded',
                            planned_for=(NOW - timedelta(days=8)).isoformat(), started_at=(NOW - timedelta(days=8)).isoformat(),
                            ended_at=(NOW - timedelta(days=8)).isoformat(), spec_json=json.dumps(dict(argv=[released()])), reason='old'), cid)
                for i, delta in enumerate((1, 35, 37)):
                    jobs.append(st.db, 'runs', dict(run_id=f'{200+i:032x}', job='gsd-steady', state='planned',
                                planned_for=(NOW + timedelta(hours=delta)).isoformat(), started_at=None, ended_at=None,
                                spec_json='{"heavy":true}', reason='Chosen by Steward. ' + 'x' * 300), cid)
                st.db.execute('INSERT INTO wakes(id,started_at,status,checkpoint,gate_reason,harness_run_id) VALUES (?,?,?,?,?,?)',
                              (1000, NOW.isoformat(), 'gated', '{}', 'Owner Talk', 'synthetic'))
            row = jobs.get(st.db, 'runs', f'{0:032x}')
            archive.job(self.data, row, now=NOW)
        doc = self.doc({'wake_times': []})  # Moment wakes remain when ordinary wakes are disabled.
        schedule = self.block(doc, 'schedule')
        self.assertEqual(set(schedule), {'kind', 'layer', 'now', 'window_h', 'items'})
        self.assertEqual(schedule['layer'], 'process')
        self.assertEqual(schedule['window_h'], 36)
        self.assertEqual([i['at'] for i in schedule['items']], sorted(i['at'] for i in schedule['items']))
        job_items = [i for i in schedule['items'] if i['kind'] == 'job']
        self.assertEqual(len(job_items), 2)
        self.assertEqual(job_items[0]['est_min'], 10)
        for item in schedule['items']:
            self.assertEqual(set(item), {'at', 'kind', 'job', 'est_min', 'heavy', 'reason', 'run_id'})
            self.assertLessEqual(intent.when(item['at']), NOW + timedelta(hours=36))
            self.assertLessEqual(len(item['reason'] or ''), 200)
            if item['kind'] == 'wake':
                self.assertIsNone(item['job'])
                self.assertIsNone(item['run_id'])
                self.assertFalse(item['heavy'])
                self.assertIn(intent.hm(intent.when(item['at'])), ['07:00', '18:30'])
        runs = self.block(doc, 'runs')
        self.assertEqual(set(runs), {'kind', 'layer', 'items'})
        self.assertEqual(runs['layer'], 'process')
        self.assertEqual(len(runs['items']), 100)
        self.assertEqual([intent.when(i['started_at']) for i in runs['items']],
                         sorted((intent.when(i['started_at']) for i in runs['items']), reverse=True))
        for item in runs['items']:
            self.assertEqual(set(item), {'id', 'kind', 'job', 'started_at', 'ended_at', 'status', 'severity', 'sessions', 'turns', 'reason', 'transcript'})
            self.assertLessEqual(len(item['reason'] or ''), 200)
            self.assertGreaterEqual(intent.when(item['started_at']), NOW - timedelta(days=7))
        running = next(i for i in runs['items'] if i['id'] == 'wake-1000')
        self.assertIsNone(running['ended_at'])
        self.assertIsNone(running['turns'])
        self.assertFalse(running['transcript'])
        available = next(i for i in runs['items'] if i['id'] == 'run-' + f'{0:032x}')
        self.assertTrue(available['transcript'])
        self.assertEqual((available['sessions'], available['turns']), (0, 0))

    def test_schedule_uses_configured_wakes_and_preserves_moments(self):
        settings = {'wake_times': ['12:34']}
        items = self.block(self.doc(settings), 'schedule')['items']
        clocks = {intent.hm(intent.when(i['at'])) for i in items if i['kind'] == 'wake'}
        self.assertEqual(clocks, {'12:34', '07:00', '18:30'})
        clocks = {intent.hm(intent.when(i['at'])) for i in
                  self.block(self.doc({'wake_times': []}), 'schedule')['items'] if i['kind'] == 'wake'}
        self.assertEqual(clocks, {'07:00', '18:30'})

    def test_configured_wake_dst_slots_match_dispatch(self):
        from steward import runtime
        for now, expected in [
                ('2026-03-28T13:00:00+00:00', ['2026-03-30T00:30:00+00:00']),
                ('2026-10-24T12:00:00+00:00', ['2026-10-25T00:30:00+00:00', '2026-10-25T01:30:00+00:00'])]:
            settings = {'wake_times': ['03:30']}
            slots = intent.schedule([], datetime.fromisoformat(now), settings)['items']
            self.assertEqual([i['at'] for i in slots], expected)
            for item in slots:
                at = datetime.fromisoformat(item['at'])
                self.assertEqual(runtime.latest_wake(at, settings), at)

    def test_schedule_unknown_duration_and_dst_wall_times(self):
        row = dict(run_id='a'*32, job='synthetic', state='planned', spec_json=json.dumps(dict(argv=[released()])), reason=None,
                   planned_for=(NOW + timedelta(hours=1)).isoformat(), started_at=None, ended_at=None)
        item = next(i for i in intent.schedule([row], NOW, {})['items'] if i['kind'] == 'job')
        self.assertIsNone(item['est_min'])
        self.assertIsNone(item['reason'])
        now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        items = intent.schedule([], now, {})['items']
        self.assertEqual(len({i['at'] for i in items}), len(items))
        self.assertTrue(any(i['at'] == '2026-10-25T03:00:00+00:00' for i in items))  # 05:00 Athens after DST.

    def test_schedule_observed_duration_keeps_unknown_distinct_from_reservation_bound(self):
        from steward import planner
        row = dict(run_id='planned', job='synthetic', state='planned', spec_json='{}', reason=None,
                   planned_for=(NOW + timedelta(hours=1)).isoformat(), started_at=None, ended_at=None)
        runs = [row]
        def estimate():
            return next(i['est_min'] for i in intent.schedule(runs, NOW, {})['items'] if i['kind'] == 'job')
        self.assertIsNone(estimate())
        self.assertEqual(planner.expected(runs, 'synthetic', {'max_minutes': 45}), 45)
        for minutes, state in [(10, 'succeeded'), (20, 'failed')]:
            runs.append(dict(job='synthetic', state=state, started_at=(NOW - timedelta(minutes=minutes)).isoformat(),
                             ended_at=NOW.isoformat(), planned_for=NOW.isoformat(), cost_minutes=None))
        self.assertEqual(estimate(), 15)
        runs[-1]['cost_minutes'] = 8
        self.assertEqual(estimate(), 8)
        self.assertEqual(planner.expected(runs, 'synthetic', {'max_minutes': 45}), 8)

    def test_schedule_array_cap(self):
        rows = [dict(run_id=f'{i:032x}', job='synthetic', state='planned', spec_json=json.dumps(dict(argv=[released()])), reason=None,
                     planned_for=(NOW + timedelta(minutes=i)).isoformat(), started_at=None, ended_at=None) for i in range(405)]
        self.assertEqual(len(intent.schedule(rows, NOW, {})['items']), 400)

    def test_document_validates_against_mech_d_contract(self):
        # mech-d owns intent/1 and ships contracts/intent-1.json (vendored here, compared in test_contracts).
        from steward.contract import problems
        from steward.contracts import load
        with writer(self.state) as st:
            self.add_job(st.db, ['06:30', '18:30'])
            self.send(st.db, 'Act before the deadline.')
        doc = self.doc(now=datetime.now(timezone.utc))
        self.assertEqual(problems(load('intent-1', vendor=True), doc), [])
        self.assertTrue(problems(load('intent-1', vendor=True), dict(doc, extra=1)))

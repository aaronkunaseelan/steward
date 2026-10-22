from fixture_mind import use_settings
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from steward import contracts, intent, jobs, messages, runtime, senses
from steward.state import writer

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/garmin.today.v1.json').read_text())


class HealthTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass

    def collect(self, value=None, output=None, error=None, now=NOW):
        def runner(argv):
            if argv != ['garmin', 'today', '--json']:
                raise FileNotFoundError
            if error:
                raise error
            return output if output is not None else json.dumps(value if value is not None else fixture())
        return senses.collect(self.root, {}, now, runner, Path(self.tmp.name) / 'rollouts')['health']

    def save(self, observation, now=NOW):
        with writer(self.root) as store:
            with store.db:
                cid = jobs.change(store.db, 'tick', now, 'tick')
                senses.save(store.db, {'health': observation}, now, cid)

    def test_fresh_contract_and_state(self):
        self.assertEqual(contracts.issues(contracts.load('garmin.today.v1', vendor=True), fixture()), [])
        result = self.collect()
        self.assertEqual(result['status'], 'ok')
        data = result['data']
        self.assertEqual(senses.freshness('health', dict(result, at=NOW.isoformat()), NOW)['verdict'], 'fresh')
        self.assertEqual(data['watch_upload_age_s'], 1800)
        self.assertEqual(data['pulled_age_s'], 300)
        self.assertEqual(data['body_battery'], fixture()['body_battery'])
        self.save(result)
        with writer(self.root) as store:
            self.assertEqual(jobs.snapshot(store.db, now=NOW)['senses']['health']['data'], data)

    def test_values_are_dated_and_morning_requires_post_sleep_upload(self):
        value = fixture()
        data = self.collect(value)['data']
        self.assertTrue(data['morning_synced'])
        self.assertEqual(data['value_times']['body_battery']['at'], value['body_battery']['at'])
        self.assertEqual(data['value_times']['training_readiness']['date'], value['date'])
        self.assertIsNone(data['value_times']['training_readiness']['at'])  # producer has no measurement time
        for metric in ('body_battery', 'sleep', 'stress', 'hrv', 'resting_hr', 'steps', 'training_readiness', 'last_activity'):
            self.assertIn(metric, data['value_times'])
        for upload, sleep_date, duration, end in [
            ('2026-10-15T20:00:00Z', '2026-10-16', 28800, '2026-10-16T04:00:00Z'),
            ('2026-10-16T03:00:00Z', '2026-10-16', 28800, '2026-10-16T04:00:00Z'),
            ('2026-10-16T11:30:00Z', '2026-10-16', None, None),
            ('2026-10-16T11:30:00Z', '2026-10-15', 28800, '2026-10-15T04:00:00Z'),
            (None, '2026-10-16', 28800, '2026-10-16T04:00:00Z'),
        ]:
            with self.subTest(upload=upload, sleep_date=sleep_date, duration=duration):
                value.update(watch_uploaded_at=upload)
                value['sleep'].update(date=sleep_date, duration_s=duration, end=end)
                data = senses.health({}, NOW, lambda argv: json.dumps(value))
                self.assertFalse(data['morning_synced'])
                self.assertNotEqual(data['value_times']['body_battery']['period'], 'today')
                self.save(dict(status='ok', data=data))
                with writer(self.root) as store:
                    cached = senses.snapshot(store.db, NOW + timedelta(days=1))['health']['data']
                self.assertFalse(cached['morning_synced'])
        # A fresh cached sense must lose its morning claim at the next Athens midnight.
        self.save(dict(status='ok', data=senses.health({}, NOW, lambda argv: json.dumps(fixture()))))
        with writer(self.root) as store:
            cached = senses.snapshot(store.db, NOW + timedelta(hours=10))['health']['data']
        self.assertFalse(cached['morning_synced'])

    def test_stale_conditions_and_boundary(self):
        for auth, upload, stale in [
            ('ok', '2026-10-16T00:00:00Z', True),
            ('ok', '2026-10-16T00:00:01Z', False),
            ('ok', '2026-10-15T23:59:59Z', True),
            ('signed_out', '2026-10-16T11:30:00Z', True),
            ('rate_limited', '2026-10-16T11:30:00Z', True),
            ('error', '2026-10-16T11:30:00Z', True),
            ('ok', None, True),
        ]:
            with self.subTest(auth=auth, upload=upload):
                value = fixture()
                expiry = (datetime.fromisoformat(upload.replace('Z', '+00:00')) + timedelta(hours=12)).isoformat() if upload else None
                value.update(auth=auth, watch_uploaded_at=upload, watch_fresh_until=expiry, pulled_at=None)
                data = self.collect(value)['data']
                self.assertEqual(senses.freshness('health', dict(status='ok', at=NOW.isoformat(), data=data), NOW)['verdict'],
                                 'unknown' if upload is None else 'stale' if stale else 'fresh')
                self.assertIsNone(data['pulled_age_s'])
                if upload is None:
                    self.assertIsNone(data['watch_upload_age_s'])

    def test_invalid_documents_are_unknown_without_data(self):
        for bad in ('schema', 'missing', 'type', 'enum', 'date', 'naive', 'json'):
            with self.subTest(bad=bad):
                value = fixture()
                if bad == 'schema':
                    value['schema'] = 'wrong'
                elif bad == 'missing':
                    del value['sleep']['duration_s']
                elif bad == 'type':
                    value['body_battery']['now'] = True
                elif bad == 'enum':
                    value['auth'] = 'private'
                elif bad == 'date':
                    value['pulled_at'] = 'private'
                elif bad == 'naive':
                    value['watch_uploaded_at'] = '2026-10-16T11:30:00'
                result = self.collect(value, output='private' if bad == 'json' else None)
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(result['reason'].startswith('garmin.today.v1:'))
                self.assertNotIn('private', result['reason'])
                self.assertNotIn('data', result)

    def test_week_required_and_validated_at_read_time(self):
        from test_fulfilment import session
        for bad in ('missing', 'type', 'field', 'naive', 'zones', 'duration'):
            with self.subTest(bad=bad):
                value = fixture()
                value['week'] = [session()]
                if bad == 'missing':
                    del value['week']
                elif bad == 'type':
                    value['week'] = None
                elif bad == 'field':
                    del value['week'][0]['label']
                elif bad == 'naive':
                    value['week'][0]['start'] = '2026-10-16T07:00:00'
                elif bad == 'zones':
                    value['week'][0]['zone_s'] = [None]*4
                else:
                    value['week'][0]['duration_s'] = True
                result = self.collect(value)
                self.assertEqual(result['status'], 'unknown')
                self.assertIn('week', result['reason'])
        value = fixture()
        value['week'] = [session('running', day=16)]
        self.assertEqual(self.collect(value)['data']['week'], value['week'])

    def test_missing_binary_is_unknown(self):
        result = self.collect(error=FileNotFoundError('private'))
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('FileNotFoundError', result['reason'])
        self.assertNotIn('private', result['reason'])

    def test_interval_and_digest_excludes_ages(self):
        first = self.collect()
        later = self.collect(now=NOW + timedelta(hours=13))
        self.assertEqual(first['data']['digest'], later['data']['digest'])
        self.assertEqual(senses.freshness('health', dict(later, at=(NOW + timedelta(hours=13)).isoformat()), NOW + timedelta(hours=13))['verdict'], 'stale')
        self.save(first)
        def unavailable(argv):
            raise FileNotFoundError
        for seconds, expected in [(1799, False), (1800, True)]:
            observations = senses.collect(self.root, {}, NOW + timedelta(seconds=seconds),
                                          unavailable, Path(self.tmp.name) / 'rollouts')
            self.assertEqual('health' in observations, expected)

    def test_health_changes_never_wake_or_reach_page_two(self):
        with writer(self.root) as store:
            before = intent.chain(store.db, NOW)
        self.save(self.collect())
        value = fixture()
        value['body_battery']['now'] = 10
        changed = dict(status='ok', data=senses.health({}, NOW, lambda argv: json.dumps(value)))
        self.save(changed)
        self.save(dict(status='unknown', reason='unavailable'))
        with writer(self.root) as store:
            self.assertEqual(senses.events(store.db, 0), [])
            self.assertEqual(runtime.gate(store.db), 'nothing new')
            self.assertEqual(intent.chain(store.db, NOW), before)

    def test_signed_out_alert_once_per_transition_even_after_history_pruned(self):
        sent = []
        def sender(topic, text, **options):
            sent.append((topic, text, options))
            return len(sent)
        def observe(auth, now):
            value = fixture()
            value['auth'] = auth
            observation = dict(status='ok', data=senses.health({}, now, lambda argv: json.dumps(value)))
            self.save(observation, now)
            with writer(self.root) as store:
                messages.alerts(store.db, now, sender, {})
        observe('signed_out', NOW)
        for i in range(100):
            observe('signed_out', NOW + timedelta(minutes=i + 1))
        self.save(dict(status='unknown', reason='unavailable'))
        with writer(self.root) as store:
            messages.alerts(store.db, NOW + timedelta(hours=2), sender, {})
        observe('signed_out', NOW + timedelta(hours=3))
        self.assertEqual(len(sent), 1)
        observe('ok', NOW + timedelta(hours=4))
        observe('signed_out', NOW + timedelta(hours=5))
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(topic == 'Alerts' and options['disable_notification']
                            for topic, _, options in sent))
        self.assertEqual(sent[0][1],
                         'Garmin needs you to sign in again: run `~/tools/garmin/bin/garmin-login` on OCI')
        with writer(self.root) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM told WHERE key LIKE 'alert:garmin:%'").fetchone()[0], 2)
            self.assertEqual(runtime.gate(store.db), 'nothing new')

    def test_rate_limited_has_no_alert(self):
        value = fixture()
        value['auth'] = 'rate_limited'
        self.save(self.collect(value))
        with writer(self.root) as store:
            messages.alerts(store.db, NOW, lambda *a, **k: self.fail('unexpected alert'), {})
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

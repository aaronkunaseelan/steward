from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import jobs, memory
from steward.cli import main
from steward.notify import flush, interrupt_status, notify, telegram_sender
from steward.state import StateError, writer

NOW = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)  # 12:00 Athens
REASON = 'The booking expires before 14:00; the owner must confirm now.'


class InterruptTests(unittest.TestCase):
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

    def send(self, topic, text, **options):
        self.sent.append((topic, options))
        return len(self.sent)

    def request(self, db, now=NOW, **options):
        return notify(db, 'Talk', 'Act on the booking.', sender=self.send,
                      now=now, actor='model', interrupt=REASON, **options)

    def test_ordinary_messages_and_pending_retries_are_silent_for_every_topic(self):
        with writer(self.root) as st:
            for topic in ('Today', 'Changes', 'Finds', 'Alerts', 'Talk'):
                notify(st.db, topic, 'Fixture message.', sender=self.send,
                       now=NOW, deliver='immediate')
            with patch('steward.notify.send_rows'):
                notify(st.db, 'Talk', 'Previously queued.', sender=self.send, now=NOW)
            flush(st.db, self.send, NOW, {})
            self.assertEqual(len(self.sent), 6)
            self.assertTrue(all(options['disable_notification'] for _, options in self.sent))
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 0)

    def test_explicit_interrupt_sounds_and_is_reported_in_state_and_journal(self):
        with writer(self.root) as st:
            result = self.request(st.db)
            self.assertEqual(result['status'], 'sent')
            self.assertFalse(self.sent[0][1]['disable_notification'])
            status = jobs.snapshot(st.db, now=NOW)['interrupts']
            self.assertEqual((status['used'], status['cap']), (1, 3))
            record = status['records'][0]
            self.assertEqual(record['topic'], 'Talk')
            self.assertEqual(record['reason'], REASON)
            self.assertEqual(record['at'], NOW.isoformat())
            self.assertEqual(record['message_id'], result['message_id'])
            self.assertFalse(record['over_cap'])
            self.assertEqual(memory.journal(st.db, day='2026-10-17')['interrupts'], [record])

    def test_fourth_is_silent_and_recorded_and_budget_survives_reopening(self):
        with writer(self.root) as st:
            for _ in range(3):
                self.request(st.db)
        with writer(self.root) as st:
            fourth = self.request(st.db)
            self.assertEqual(fourth['status'], 'sent')
            self.assertEqual([m[1]['disable_notification'] for m in self.sent], [False]*3 + [True])
            status = jobs.snapshot(st.db, now=NOW)['interrupts']
            self.assertEqual((status['used'], status['cap'], status['over_cap']), (3, 3, 1))
            overflow = memory.journal(st.db, day='2026-10-17')['interrupts'][-1]
            self.assertTrue(overflow['over_cap'])
            self.assertEqual(overflow['message_id'], fourth['message_id'])
            self.assertEqual(overflow['reason'], REASON)
            memory.file_days(st.db, NOW + timedelta(days=1))
            self.assertEqual(memory.journal(st.db, day='2026-10-17')['interrupts'][-1], overflow)

    def test_resets_at_athens_midnight_not_utc_midnight(self):
        before = datetime(2026, 10, 17, 20, 59, tzinfo=timezone.utc)  # 23:59 Athens
        after = before + timedelta(minutes=1)  # 00:00 Athens; same UTC date
        with writer(self.root) as st:
            for _ in range(4):
                self.request(st.db, before)
            self.assertEqual(interrupt_status(st.db, before)['used'], 3)
            self.assertEqual(interrupt_status(st.db, after)['used'], 0)
            self.request(st.db, after)
            self.assertFalse(self.sent[-1][1]['disable_notification'])
            status = interrupt_status(st.db, after)
            self.assertEqual((status['day'], status['used'], status['over_cap']), ('2026-10-18', 1, 0))
            self.request(st.db, after.replace(hour=23))  # UTC midnight must not reset it
            self.request(st.db, after.replace(hour=23) + timedelta(hours=1))
            self.assertEqual(interrupt_status(st.db, after.replace(hour=23) + timedelta(hours=1))['used'], 3)

    def test_dst_repeated_hour_does_not_reset_cap(self):
        before = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)  # 03:30 +03
        after = before + timedelta(hours=1)  # 03:30 +02, same Athens day
        with writer(self.root) as st:
            for _ in range(3):
                self.request(st.db, before)
            self.request(st.db, after)
            self.assertTrue(self.sent[-1][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, after)['used'], 3)

    def test_configurable_cap_and_zero(self):
        with writer(self.root) as st:
            for cap, expected in [(0, True), (1, False), (1, True)]:
                self.request(st.db, settings={'interrupt_cap': cap})
                self.assertEqual(self.sent[-1][1]['disable_notification'], expected)
            status = jobs.snapshot(st.db, now=NOW, interrupt_cap=1)['interrupts']
            self.assertEqual((status['used'], status['cap'], status['over_cap']), (1, 1, 2))

    def test_cap_is_checked_at_send_not_enqueue_time(self):
        with writer(self.root) as st:
            for _ in range(3):
                self.request(st.db)
            with patch('steward.notify.send_rows'):
                pending = self.request(st.db)
            self.assertEqual(pending['status'], 'pending')
            next_day = NOW + timedelta(days=1)
            flush(st.db, self.send, next_day, {})
            self.assertFalse(self.sent[-1][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, next_day)['used'], 1)
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 3)

    def test_queued_interrupt_cannot_bypass_newly_exhausted_cap(self):
        with writer(self.root) as st:
            with patch('steward.notify.send_rows'):
                self.request(st.db)
            for _ in range(3):
                self.request(st.db)
            flush(st.db, self.send, NOW, {})
            self.assertTrue(self.sent[-1][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, NOW)['over_cap'], 1)

    def test_crash_after_send_cannot_buzz_again_on_retry(self):
        def crash(topic, text, **options):
            raise SystemExit('simulated crash after Telegram may have accepted')
        with writer(self.root) as st:
            with self.assertRaises(SystemExit):
                notify(st.db, 'Talk', 'Act.', sender=crash, now=NOW, interrupt=REASON)
        with writer(self.root) as st:
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 1)
            flush(st.db, self.send, NOW, {})
            self.assertTrue(self.sent[-1][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 1)

    def test_failed_send_reserves_budget_and_retry_is_silent(self):
        def ambiguous(topic, text, **options):
            # The durable slot is already visible if Telegram accepts then the process dies.
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 1)
            self.assertFalse(options['disable_notification'])
            raise OSError('private failure detail')
        with writer(self.root) as st:
            result = notify(st.db, 'Talk', 'Act.', sender=ambiguous,
                            now=NOW, interrupt=REASON)
            self.assertEqual(result['status'], 'pending')
            self.assertNotIn('private', str(result))
            record = interrupt_status(st.db, NOW)['records'][0]
            self.assertIsNone(record['message_id'])
            flush(st.db, self.send, NOW, {})
            self.assertTrue(self.sent[-1][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 1)
            self.assertEqual(interrupt_status(st.db, NOW)['records'][0]['message_id'], 1)

    def test_interrupt_find_is_immediate_and_dedup_does_not_spend_again(self):
        with writer(self.root) as st:
            for _ in range(2):
                result = notify(st.db, 'Finds', 'Act.', 'fixture-key', sender=self.send,
                                now=NOW, interrupt=REASON)
            self.assertEqual(result['status'], 'already_told')
            self.assertEqual(len(self.sent), 1)
            self.assertFalse(self.sent[0][1]['disable_notification'])
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 1)
            flush(st.db, self.send, NOW + timedelta(days=1), {})
            self.assertEqual(len(self.sent), 1)

    def test_reason_route_and_config_validation_precede_intent(self):
        with writer(self.root) as st:
            for reason in ('', '  ', True):
                with self.assertRaises(StateError):
                    notify(st.db, 'Talk', 'Act.', sender=self.send, now=NOW, interrupt=reason)
            for options in ({'actor': 'tick'}, {'deliver': 'digest'}, {'deliver': 'silent'},
                            {'settings': {'interrupt_cap': -1}}, {'settings': {'interrupt_cap': True}}):
                with self.assertRaises(StateError):
                    notify(st.db, 'Talk', 'Act.', sender=self.send, now=NOW, interrupt=REASON, **options)
            self.assertEqual(st.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 0)
            self.assertEqual(self.sent, [])

    def test_cli_requires_reason_and_state_uses_configured_cap(self):
        args = ['notify', '--state', str(self.root), '--topic', 'Talk', 'Act.']
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                main(args + ['--interrupt'])
            self.assertEqual(exc.exception.code, 2)
        config = Path(self.tmp.name) / 'config/steward/config.toml'
        with config.open('a') as stream:
            stream.write('interrupt_cap = 1\n')
        def injected(*args, **options):
            return notify(*args, **options, sender=self.send, now=NOW)
        with patch('steward.cli.notify', side_effect=injected), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ['--interrupt', REASON]), 0)
            self.assertEqual(main(args + ['--interrupt', REASON]), 0)
        self.assertEqual([m[1]['disable_notification'] for m in self.sent], [False, True])
        output = io.StringIO()
        with patch('steward.jobs.now_utc', return_value=NOW), redirect_stdout(output):
            self.assertEqual(main(['state', '--state', str(self.root)]), 0)
        status = json.loads(output.getvalue())['interrupts']
        self.assertEqual((status['used'], status['cap'], status['over_cap']), (1, 1, 1))

    def test_telegram_payload_is_silent_by_default_and_preserves_explicit_interrupt(self):
        payloads = []
        def capture(method, payload):
            payloads.append(payload)
            return {'message_id': 7}
        with patch('steward.notify.binding', return_value={'chat_id': -1, 'topics': {'Talk': 6}}), \
             patch('steward.notify.api', side_effect=capture):
            telegram_sender('Talk', 'Fixture.')
            telegram_sender('Talk', 'Fixture.', disable_notification=False)
        self.assertEqual([p['disable_notification'] for p in payloads], [True, False])

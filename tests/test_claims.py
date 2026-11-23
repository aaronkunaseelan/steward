"""A Today message may not restate structured facts the state contradicts."""
from fixture_mind import use_settings
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from steward import jobs, senses
from steward.notify import notify
from steward.state import StateError, writer

LIST = '[Your list](https://finds.example.test/your-list)'


class ClaimTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)  # Tue 07:00 Athens

    def store(self, name):
        return writer(Path(self.tmp.name) / name, initialize=True)

    def seed(self, db, name, data, at=None):
        at = at or self.now
        senses.save(db, {name: dict(status='ok', data=data)}, at, jobs.change(db, 'tick', at, 'tick'))

    def sent_find(self, db):
        cid = jobs.change(db, 'notify', self.now, 'model')
        db.execute("INSERT INTO outbox(id,change_id,topic,text,state,message_id,sent_at,delivery) "
                   "VALUES ('find-1',?,'Finds','Synthetic find one','sent',1,?,'immediate')",
                   (cid, self.now.isoformat()))

    def pending_finds(self, db, count):
        cid = jobs.change(db, 'notify', self.now, 'model')
        for number in range(count):
            db.execute("INSERT INTO outbox(id,change_id,topic,text,state,delivery) "
                       "VALUES (?,?,'Finds','Synthetic find','pending','digest')",
                       (f'pending-{number}', cid))

    def today_rows(self, db):
        return db.execute("SELECT count(*) FROM outbox WHERE topic='Today'").fetchone()[0]

    def refused(self, db, text, **options):
        options.setdefault('actor', 'model')
        options.setdefault('moment', 'orientation')
        with self.assertRaises(StateError) as caught:
            notify(db, 'Today', text, sender=lambda *a, **k: 1, now=self.now, **options)
        return str(caught.exception)

    def accepted(self, db, text, **options):
        options.setdefault('actor', 'model')
        notify(db, 'Today', text, sender=lambda *a, **k: 1, now=self.now, **options)

    def test_no_finds_refused_after_a_sent_find(self):
        text = 'Synopsis\n\nNo finds tonight · ' + LIST
        with self.store('sent') as st:
            self.sent_find(st.db)
            reason = self.refused(st.db, text)
            self.assertIn('claim check: finds', reason)
            self.assertEqual(self.today_rows(st.db), 0)
        with self.store('empty') as st:
            self.accepted(st.db, text, moment='orientation')
            self.assertEqual(self.today_rows(st.db), 1)

    def test_finds_count_must_match(self):
        with self.store('waiting') as st:
            self.pending_finds(st.db, 2)
            self.assertIn('claim check: finds', self.refused(st.db, 'Synopsis\n\n3 finds tonight'))
            self.assertEqual(self.today_rows(st.db), 0)
            self.accepted(st.db, 'Synopsis\n\n2 finds tonight')
            self.assertEqual(self.today_rows(st.db), 1)
            # Counts that are not tonight's finds (a job's history, another day) are not this claim.
            self.accepted(st.db, 'Removed the live-events job: 0 finds in 9 runs\n\n4 finds Mon, from your saves')
            self.assertEqual(self.today_rows(st.db), 2)

    def test_cant_see_calendar_refused_when_ahead_covers_it(self):
        events = []
        with self.store('covered') as st:
            self.seed(st.db, 'calendars', dict(events=events, ahead=dict(
                status='ok', day='2026-10-20', **{'from': '2026-10-23T00:00:00+03:00',
                'to': '2028-10-01T00:00:00+03:00'})))
            reason = self.refused(st.db, "Can't see: calendar after Mon.")
            self.assertIn('claim check: coverage', reason)
            self.assertIn('2028-10-01', reason)
            self.assertEqual(self.today_rows(st.db), 0)
        with self.store('failed_ahead') as st:
            self.seed(st.db, 'calendars', dict(events=events, ahead=dict(
                status='failed', day='2026-10-20', reason='read failed')))
            self.accepted(st.db, "Can't see: calendar after Mon.")
            self.assertEqual(self.today_rows(st.db), 1)

    def event(self, start, summary, transparency=None, event_key=None, all_day=False):
        moment = dict(dateTime=start) if not all_day else dict(date=start)
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
        return dict(calendar='schedule', id=start + summary, start=moment,
                    end=dict(dateTime=end) if not all_day else dict(date=start),
                    all_day=all_day, transparency=transparency, summary=summary,
                    status='confirmed', eventType='default', event_key=event_key)

    def test_no_meetings_day_refused_over_an_opaque_meeting(self):
        monday = '2026-10-26T'
        marker = self.event(monday + '09:00:00+02:00', 'No meetings', all_day=False)
        opaque = self.event(monday + '14:30:00+02:00', 'Synthetic appointment')
        block = self.event(monday + '11:00:00+02:00', 'movement', transparency='transparent', event_key='block-key')
        text = 'Mon · no-meetings day\n\nClosing line'
        with self.store('busy') as st:
            self.seed(st.db, 'calendars', dict(events=[marker, opaque, block]))
            reason = self.refused(st.db, text)
            self.assertIn('claim check: no meetings', reason)
            self.assertIn('1', reason)
            self.assertEqual(self.today_rows(st.db), 0)
        with self.store('clear') as st:
            self.seed(st.db, 'calendars', dict(events=[marker, block]))
            cid = jobs.change(st.db, 'block choose', self.now, 'owner')
            st.db.execute("INSERT INTO blocks(event_key,version,change_id,demand,state,payload) "
                          "VALUES ('block-key',1,?,'movement','applied','{}')", (cid,))
            self.accepted(st.db, text)

    def test_bare_weekday_due_refused_for_past_deadline(self):
        saturday = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
        self.now = saturday
        with self.store('due') as st:
            self.seed(st.db, 'gsd', dict(deadlines=[dict(id='dcr', title='DCR comments', state='ready',
                                                        next_actor='me', due_at='2026-10-22T02:00:00+03:00')],
                                        horizon_deadlines=[]))
            reason = self.refused(st.db, 'DCR comments: due Thu, still open')
            self.assertIn('claim check: due Thu', reason)
            self.assertIn('Thu 22 Oct', reason)
            self.assertEqual(self.today_rows(st.db), 0)
            self.accepted(st.db, 'DCR comments: was due Thu 19')
            self.assertEqual(self.today_rows(st.db), 1)
            self.refused(st.db, 'DCR comments: due Thu 22, still open')
            self.assertEqual(self.today_rows(st.db), 1)
            # A day number still ahead this month (or next) is a future deadline: 19 Nov is a Thursday.
            self.accepted(st.db, 'AWS review: due Thu 19, still open')
            self.assertEqual(self.today_rows(st.db), 2)

    def test_limits_only_in_last_group(self):
        with self.store('middle') as st:
            reason = self.refused(st.db, 'Headline\n\nNot placed: two blocks\n\nClosing line')
            self.assertIn('claim check: limits', reason)
            self.assertEqual(self.today_rows(st.db), 0)
        with self.store('last') as st:
            self.accepted(st.db, 'Headline\n\nNot placed: two blocks\n\nNo finds tonight · ' + LIST)
            self.assertEqual(self.today_rows(st.db), 1)
        with self.store('two') as st:
            reason = self.refused(st.db, "Can't see: calendar after Mon.\nCan't see: gsd is stale")
            self.assertIn('claim check: limits', reason)
            self.assertEqual(self.today_rows(st.db), 0)

    def test_tick_messages_are_not_checked(self):
        with self.store('tick') as st:
            self.sent_find(st.db)
            self.accepted(st.db, 'No finds tonight', actor='tick')
            self.assertEqual(self.today_rows(st.db), 1)

    def test_invalid_plan_reason_is_kept(self):
        from steward.errors import safe_reason
        from steward.scheduler import InvalidPlan
        self.assertEqual(safe_reason(InvalidPlan('invalid object schema')),
                         'InvalidPlan: invalid object schema')
        self.assertEqual(len(safe_reason(InvalidPlan('x' * 500))), 200)

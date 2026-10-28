"""A known reading can honestly contain an unknown location."""
from pathlib import Path
import json
from steward import briefing, jobs, messages, senses
from test_place import fixture
from steward.state import writer
import test_intent
import unittest


class PlaceVerdictTests(unittest.TestCase):
    setUp = test_intent.IntentTests.setUp
    doc = test_intent.IntentTests.doc
    add_job = test_intent.IntentTests.add_job
    save_healthy = test_intent.IntentTests.save_healthy

    def test_current_unknown_place_is_fresh_green_and_still_unknown_on_page(self):
        now = test_intent.NOW.replace(hour=2)
        with writer(self.state) as st:
            self.save_healthy(st.db, now)
            messages.poll(st.db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
            value = dict(fixture(), state='unknown', place=None, since=None,
                         last=None, today=[])
            def runner(argv):
                if argv == ['phone', 'where', '--json']:
                    return json.dumps(value)
                raise FileNotFoundError
            observation = senses.collect(self.state, {}, now, runner,
                                         Path(self.tmp.name) / 'rollouts')['place']
            self.assertEqual(observation['status'], 'ok')
            senses.save(st.db, {'place': observation},
                        now, jobs.change(st.db, 'fixture', now, 'tick'))
            state = jobs.snapshot(st.db, now=now)
            place = state['senses']['place']
            self.assertEqual(place['data']['state'], 'unknown')
            self.assertIsNone(place['data']['place'])
            self.assertEqual(place['verdict'], 'fresh')
            page = briefing.render(state, now, {})
            self.assertIn('## Place [fresh]', page)
            self.assertIn('- state: unknown', page.split('## Place', 1)[1])
        self.assertEqual(self.doc(now=now)['state'], 'green')

    def test_failed_place_read_is_not_fresh(self):
        now = test_intent.NOW.replace(hour=2)
        with writer(self.state) as st:
            senses.save(st.db, {'place': dict(status='unknown', reason='fixture read failed')},
                        now, jobs.change(st.db, 'fixture', now, 'tick'))
            place = senses.snapshot(st.db, now)['place']
        self.assertEqual(place['verdict'], 'unknown')
        self.assertEqual(place['reason'], 'fixture read failed')

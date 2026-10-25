from fixture_mind import use_settings
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import contracts, intent, jobs, runtime, senses
from steward.state import writer

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/phone.place.v1.json').read_text())


class PlaceTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass

    def collect(self, value=None, output=None, error=None, now=NOW):
        def runner(argv):
            if argv != ['phone', 'where', '--json']:
                raise FileNotFoundError
            if error:
                raise error
            return output if output is not None else json.dumps(value if value is not None else fixture())
        return senses.collect(self.root, {}, now, runner, Path(self.tmp.name) / 'rollouts')

    def save(self, observation, now=NOW):
        with writer(self.root) as store:
            with store.db:
                cid = jobs.change(store.db, 'tick', now, 'tick')
                senses.save(store.db, {'place': observation}, now, cid)

    def test_fixture_contract_and_valid_states(self):
        self.assertEqual(contracts.issues(contracts.load('phone.place.v1', vendor=True), fixture()), [])
        for state, place in [('at', 'gym'), ('out', None), ('unknown', None)]:
            with self.subTest(state=state):
                value = fixture()
                value.update(state=state, place=place, since=None if state == 'unknown' else value['since'])
                result = self.collect(value)['place']
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['data']['state'], state)
                self.assertEqual(result['data']['place'], place)
                self.assertEqual(result['data']['since'], value['since'])
                self.assertEqual(result['data']['today'], value['today'])

    def test_invalid_documents_are_unknown_and_do_not_echo_values(self):
        for bad in ('schema', 'missing', 'type', 'enum', 'time', 'naive', 'json'):
            with self.subTest(bad=bad):
                value = fixture()
                if bad == 'schema':
                    value['schema'] = 'PRIVATE'
                elif bad == 'missing':
                    del value['last_event']['place']
                elif bad == 'type':
                    value['today'][0]['place'] = 5
                elif bad == 'enum':
                    value['state'] = 'PRIVATE'
                elif bad == 'time':
                    value['today'][0]['at'] = 'PRIVATE'
                elif bad == 'naive':
                    value['since'] = '2026-10-17T12:00:00'
                result = self.collect(value, output='PRIVATE' if bad == 'json' else None)['place']
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(result['reason'].startswith('phone.place.v1:'))
                self.assertNotIn('PRIVATE', result['reason'])
                self.assertNotIn('data', result)

    def test_missing_binary_is_unknown(self):
        result = self.collect(error=FileNotFoundError('PRIVATE'))['place']
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('FileNotFoundError', result['reason'])
        self.assertNotIn('PRIVATE', result['reason'])

    def test_interval(self):
        self.save(self.collect()['place'])
        for seconds, expected in [(599, False), (600, True)]:
            self.assertEqual('place' in self.collect(now=NOW + timedelta(seconds=seconds)), expected)

    def test_changes_never_wake_and_page_two_has_no_place_names(self):
        self.save(self.collect()['place'])
        value = fixture()
        value.update(state='out', place=None)
        self.save(self.collect(value, now=NOW + timedelta(minutes=10))['place'])
        self.save(dict(status='unknown', reason='unavailable'))
        with writer(self.root) as store:
            self.assertEqual(senses.events(store.db, 0), [])
            self.assertEqual(runtime.gate(store.db), 'nothing new')
        self.save(self.collect(now=NOW + timedelta(minutes=10))['place'])
        document = json.dumps(intent.document(self.root, {}, NOW))
        for name in ('home', 'office', 'gym'):
            self.assertNotIn(name, document)
        self.assertNotIn('phone.place', document)

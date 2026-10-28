from fixture_mind import use_settings
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import briefing, contracts, intent, jobs, runtime, senses
from steward.state import writer

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/phone.place.v3.json').read_text())


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
        self.assertEqual(contracts.issues(contracts.load('phone.place.v3', vendor=True), fixture()), [])
        for state, place in [('at', 'gym'), ('out', None), ('unknown', None)]:
            with self.subTest(state=state):
                value = fixture()
                value.update(state=state, place=place, since=None if state == 'unknown' else value['since'])
                result = self.collect(value)['place']
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['data']['state'], state)
                self.assertEqual(result['data']['place'], place)
                self.assertEqual(result['data']['since'], value['since'])
                self.assertEqual(len(result['data']['today']), len(value['today']))
                self.assertTrue(all(isinstance(row, str) for row in result['data']['today']))

    def test_invalid_documents_are_unknown_and_do_not_echo_values(self):
        for bad in ('schema', 'v2', 'missing', 'type', 'enum', 'time', 'naive', 'last_time',
                    'ssid', 'battery', 'charging', 'moved_m', 'json'):
            with self.subTest(bad=bad):
                value = fixture()
                if bad == 'schema':
                    value['schema'] = 'PRIVATE'
                elif bad == 'v2':
                    value['schema'] = 'phone.place.v2'
                elif bad == 'missing':
                    del value['last']['place']
                elif bad == 'type':
                    value['today'][0]['place'] = 5
                elif bad == 'enum':
                    value['state'] = 'PRIVATE'
                elif bad == 'time':
                    value['today'][0]['at'] = 'PRIVATE'
                elif bad == 'naive':
                    value['since'] = '2026-10-17T12:00:00'
                elif bad == 'last_time':
                    value['last']['at'] = 'PRIVATE'
                elif bad in ('ssid', 'battery', 'charging'):
                    value['last'][bad] = []
                elif bad == 'moved_m':
                    value['nights'][0]['moved_m'] = 'PRIVATE'
                result = self.collect(value, output='PRIVATE' if bad == 'json' else None)['place']
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(result['reason'].startswith('phone.place.v3:'))
                self.assertNotIn('PRIVATE', result['reason'])
                self.assertNotIn('data', result)

    def test_nights_compress_runs_not_gaps_or_city_changes(self):
        nights = [dict(date=day, city=city, country='GR' if city else None, at=None,
                       ssid=None, moved_m=None)
                  for day, city in [('2026-10-21', 'Example'), ('2026-10-22', 'Example'),
                                    ('2026-10-23', None), ('2026-10-24', None),
                                    ('2026-10-25', 'Other'), ('2026-10-27', 'Other')]]
        text = senses.night_runs(nights)
        self.assertEqual(text, '21 Oct 2026–22 Oct 2026 Example, GR / unknown (2 nights); '
                         '23 Oct 2026–24 Oct 2026 unknown / unknown (2 nights); '
                         '25 Oct 2026 Other, GR / unknown (1 night); 27 Oct 2026 Other, GR / unknown (1 night)')
        self.assertEqual(senses.night_runs([]), '')
        value = fixture()
        result = self.collect(value)['place']['data']
        self.assertEqual(result['last_city'], value['last_city'])
        self.assertEqual(result['today'][0], '03:05 EEST night / Example / Example WiFi')
        self.assertIn('Example, GR', result['nights'])
        self.assertNotIn('home', result)

    def test_v3_vendor_is_the_agreed_evidence_bytes(self):
        import hashlib
        vendor = Path(__file__).resolve().parents[1] / 'contracts/vendor/phone.place.v3.json'
        self.assertEqual(hashlib.sha256(vendor.read_bytes()).hexdigest(),
                         '4057db39d828fbc368328126785e087ff8992e502a1369bfd2fa9313adea7d32')
        self.assertFalse(vendor.with_name('phone.place.v2.json').exists())
        self.assertFalse(vendor.with_name('phone.place.v1.json').exists())

    def test_night_runs_split_wifi_and_large_moves_in_same_city(self):
        nights = [dict(date=f'2026-10-{day:02d}', city='Example', country='GR',
                       ssid=ssid, at=None, moved_m=moved)
                  for day, ssid, moved in [(21, 'WiFi A', None), (22, 'WiFi A', 2000),
                                           (23, 'WiFi A', 2001), (24, 'WiFi B', 10),
                                           (25, None, 10), (26, None, None)]]
        self.assertEqual(senses.night_runs(nights),
                         '21 Oct 2026–22 Oct 2026 Example, GR / WiFi A (2 nights); '
                         '23 Oct 2026 Example, GR / WiFi A (1 night); '
                         '24 Oct 2026 Example, GR / WiFi B (1 night); '
                         '25 Oct 2026–26 Oct 2026 Example, GR / unknown (2 nights)')

    def test_last_snapshot_fields_age_and_nulls(self):
        value = fixture()
        result = self.collect(value)['place']['data']['last']
        self.assertEqual(result, dict(trigger='time:1450', city='Example', country='GR',
                                      ssid='Fitness WiFi', battery=67, charging=False, age_s=600))
        value['last'].update(city=None, country=None, ssid=None, battery=None, charging=None)
        result = self.collect(value)['place']['data']['last']
        for key in ('city', 'country', 'ssid', 'battery', 'charging'):
            self.assertIsNone(result[key])
        value['last'] = None
        value['today'] = []
        result = self.collect(value)['place']['data']
        self.assertIsNone(result['last'])
        self.assertEqual(result['today'], [])

    def test_realistic_place_section_is_compact(self):
        value = fixture()
        self.assertEqual(len(value['nights']), 30)
        self.assertEqual(len(value['today']), 15)
        self.save(self.collect(value)['place'])
        with writer(self.root) as store:
            state = jobs.snapshot(store.db, now=NOW)
        page = briefing.render(state, NOW, {})
        place = briefing.sections(page)['Place']
        self.assertLess(len(place.encode()), 2000)
        for row in state['senses']['place']['data']['today']:
            self.assertIn('- ' + row + '\n', place)
        self.assertIn('age s: 600.0', place)
        self.assertIn('battery: 67', place)
        self.assertIn('charging: False', place)
        self.assertNotIn('carrier', place)

    def test_bad_last_city_or_night_is_unknown(self):
        for field in ['city_time', 'night_time', 'night_date']:
            value = fixture()
            if field == 'city_time':
                value['last_city']['at'] = 'PRIVATE'
            elif field == 'night_time':
                value['nights'][0]['at'] = 'PRIVATE'
            else:
                value['nights'][0]['date'] = 'PRIVATE'
            result = self.collect(value)['place']
            self.assertEqual(result['status'], 'unknown')
            self.assertNotIn('PRIVATE', result['reason'])

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

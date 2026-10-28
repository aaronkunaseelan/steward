"""A bundle reserves time for gsd-owned items with code-rendered descriptions."""
from fixture_mind import use_settings
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from steward import calendar_b as b, pages, senses
from steward.jobs import instant
from steward.state import writer, StateError
from test_calendar_b import Gog, NOW, SETTINGS

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/gsd.worklist.v1.json').read_text())
BUNDLE = dict(title='Grants: Pat Synthetic email, funding triage', minutes=60, kind='other', transparency='transparent',
              windows={str(i): [['09:00', '18:00']] for i in range(7)}, deadline=(NOW + timedelta(days=2)).isoformat(),
              source='gsd:a1', justification='Two grants items, one sitting', items=['a1', 'a2'])


def worklist(*ids):
    raw = json.loads(json.dumps(FIXTURE))
    base, project = raw['items'][0], raw['projects'][0]
    raw['items'] = [dict(base, id=i, title='Item ' + i, project_id=project['id'] if n == 0 else None, next_actor='user')
                    for n, i in enumerate(ids)]
    raw['priorities'] = dict(mine=[dict(id=i, title='Item ' + i, project=None, due_at=None, priority=None) for i in ids],
                             waiting_on_others=[])
    return raw


class BundleTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.enterContext(writer(Path(self.tmp.name), initialize=True)).db
        self.gog = Gog()
        self.worklist = worklist('a1', 'a2', 'a3')
        self.gsd_fails = False
        self.gsd_calls = 0
        senses.save(self.db, {'health': dict(status='ok', data=dict(
            date=NOW.date().isoformat(), week=[], auth='ok', watch_uploaded_at=NOW.isoformat(),
            watch_fresh_until=(NOW + timedelta(days=1)).isoformat()))}, NOW, b.change(self.db, 'fixture', NOW, 'owner'))
        self.objects = {}

    def runner(self, argv):
        if argv[:2] == ['gsd', 'worklist']:
            self.gsd_calls += 1
            self.assertEqual(argv, ['gsd', 'worklist', '--json'])
            if self.gsd_fails:
                raise ValueError('external command failed')
            return json.dumps(self.worklist)
        return self.gog(argv)

    def transport(self, method, path, **kw):
        if method == 'PUT':
            self.objects[path] = kw['data']
            return 204, b''
        return 200, self.objects[path]

    def plan(self, now=NOW):
        return b.plan(self.db, SETTINGS, now, self.runner)

    def choose(self, candidate, reason='Make progress'):
        return b.choose(self.db, SETTINGS, {'schema_version': 1, 'selections': [{'candidate_id': candidate['id'], 'reason': reason}]}, NOW, runner=self.runner)

    def candidate(self, key, action, after=''):
        return next(c for c in self.plan()['candidates']['choices']
                    if c['task_id'] == key and c['action'] == action and (c.get('start') or '') >= after)

    def event(self, key):
        return next(e for e in self.gog.events['schedule'] if e['extendedProperties']['private']['event_key'] == key)

    def add(self, name='grants', **spec):
        return b.demand(self.db, 'add', name, dict(BUNDLE, **spec), NOW, runner=self.runner)

    def test_items_are_validated_against_the_worklist(self):
        self.add()
        self.assertEqual(self.gsd_calls, 1)
        for bad in ([], ['a1', 'a1'], ['a1', 'a2', 'a3', 'a4', 'a5', 'a6'], [1], 'a1'):
            with self.subTest(bad=bad), self.assertRaisesRegex(StateError, 'invalid items'):
                self.add('bad', items=bad)
        with self.assertRaisesRegex(StateError, 'invalid items'):
            self.add('bad', kind='movement', items=['a1'])
        with self.assertRaisesRegex(StateError, 'unknown gsd item: zzz'):
            self.add('bad', items=['a1', 'zzz'])
        self.gsd_fails = True
        with self.assertRaisesRegex(StateError, 'gsd worklist unknown'):
            self.add('bad')
        self.assertEqual([r['name'] for r in b.latest(self.db, 'demands')], ['grants'])
        self.assertEqual(json.loads(b.latest(self.db, 'demands')[0]['spec_json'])['items'], ['a1', 'a2'])

    def test_code_renders_items_and_the_prepared_page(self):
        self.add()
        self.choose(self.candidate('grants:once:0', 'place'))
        event = self.event('grants:once:0')
        self.assertEqual(event['description'], 'Item a1 (Fixture project)\nItem a2')
        self.assertEqual(self.plan()['description_refreshes'], {})
        page = pages.put(self.db, 'Prep', b'ready', 'prep.md', items=['a1', 'a2'], block='grants',
                         now=NOW, transport=self.transport, runner=self.runner)
        self.assertEqual(pages.prep_slug(self.db, ('block', 'grants'), NOW.date()), page['slug'])
        prepared = 'Prepared: ' + page['url'] + '\nItem a1 (Fixture project)\nItem a2'
        self.assertEqual(self.plan()['description_refreshes'], {'grants:once:0': prepared})
        self.choose(self.candidate('grants:once:0', 'keep'), 'Prepared page is live')
        self.assertEqual(self.event('grants:once:0')['description'], prepared)
        self.worklist = worklist('a1', 'a3')
        self.assertEqual(self.plan()['description_refreshes'],
                         {'grants:once:0': 'Prepared: ' + page['url'] + '\nItem a1 (Fixture project)'})

    def test_bundle_completes_when_all_items_close(self):
        self.add()
        self.choose(self.candidate('grants:once:0', 'place'))
        self.worklist = worklist('a3')
        plan = self.plan()
        self.assertIn('grants:once:0', plan['context']['deleted_task_ids'])
        self.choose(self.candidate('grants:once:0', 'drop'), 'All items closed in gsd')
        self.assertEqual(self.gog.events['schedule'], [])
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'deleted')
        self.assertFalse([c for c in self.plan()['candidates']['choices'] if c['task_id'].startswith('grants:')])

    def test_elapsed_bundle_block_is_history(self):
        self.add()
        self.choose(self.candidate('grants:once:0', 'place'))
        end = instant(self.event('grants:once:0')['end']['dateTime'])
        self.worklist = worklist('a3')
        plan = self.plan(end + timedelta(hours=1))
        self.assertNotIn('grants:once:0', plan['context']['deleted_task_ids'])
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_unknown_worklist_holds_bundles(self):
        self.add()
        self.add('comms', items=['a3'], source='gsd:a3')
        self.choose(self.candidate('grants:once:0', 'place'))
        self.worklist = worklist('a3')
        self.gsd_fails = True
        self.objects['/_api/pages/prep-grants-' + self.event('grants:once:0')['start']['dateTime'][:10].replace('-', '')] = b'x'
        plan = self.plan()
        self.assertEqual(plan['description_refreshes'], {})
        self.assertEqual(plan['context']['deleted_task_ids'], [])
        with self.assertRaisesRegex(StateError, 'gsd worklist unknown'):
            self.choose(self.candidate('comms:once:0', 'place', after=self.event('grants:once:0')['end']['dateTime']))
        self.assertEqual([r['demand'] for r in b.latest(self.db, 'blocks')], ['grants'])
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_briefing_shows_blocks_with_items_page_and_use(self):
        from steward import briefing, jobs
        self.add()
        self.choose(self.candidate('grants:once:0', 'place'))
        event = self.event('grants:once:0')
        self.assertEqual(event['start']['dateTime'][11:16], '09:00')
        page = pages.put(self.db, 'Prep', b'ready', 'prep.md', items=['a1', 'a2'], block='grants',
                         now=NOW, transport=self.transport, runner=self.runner)
        slug = page['slug']

        def state(done):
            raw = dict(self.worklist, done=done)
            senses.save(self.db, {'gsd': dict(status='ok', data=senses.gsd(NOW, lambda argv: json.dumps(raw)))},
                        NOW, b.change(self.db, 'fixture', NOW, 'tick'))
            result = jobs.snapshot(self.db, now=NOW)
            result.update(calendar_b=b.briefing(self.db, NOW), live_pages=pages.live(self.db, NOW),
                          moments=['day_close'], day_close={})
            return result

        text = briefing.render(state([]), NOW, {})
        blocks = text.split('## Blocks ', 1)[1].split('\n## ', 1)[0]
        self.assertIn('- key: grants:once:0', blocks)
        self.assertIn('- items:\n  - a1\n  - a2', blocks)
        self.assertIn('- url: ' + page['url'], blocks)
        self.assertIn('- used: open', blocks)
        self.assertNotIn('- key:', text.split('## Movement ', 1)[1].split('\n## ', 1)[0])
        self.assertIn('Grants: Pat Synthetic email, funding triage 09:00: open', text.split('Done today', 1)[1])
        later = briefing.render(state([]), NOW + timedelta(hours=2), {})
        self.assertIn('- used: not yet known: gsd learns closures at its runs', later)
        closed = [dict(id='a1', title='Item a1', project=None, state='done', at='2026-10-19T05:30:00Z')]
        used = briefing.render(state(closed), NOW + timedelta(hours=2), {})
        self.assertIn('- closed today:\n  - 08:30 a1', used)
        self.assertIn('- used: yes', used)
        self.assertIn('Grants: Pat Synthetic email, funding triage 09:00: yes', used.split('Done today', 1)[1])
        pages.remove(self.db, slug, now=NOW, transport=self.transport)
        self.assertIn('- prepared: no: no live prepared page', briefing.render(state([]), NOW, {}))

    def test_demands_without_items_never_read_gsd(self):
        b.demand(self.db, 'add', 'focus', {k: v for k, v in BUNDLE.items() if k != 'items'}, NOW, runner=self.runner)
        self.choose(self.candidate('focus:once:0', 'place'))
        self.assertEqual(self.gsd_calls, 0)
        self.assertNotIn('description', self.event('focus:once:0'))


if __name__ == '__main__':
    unittest.main()

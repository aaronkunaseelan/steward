from fixture_mind import use_settings
"""BR4 G5: producer ordering and project labels survive the installed read path."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from steward import briefing, jobs, messages, senses
from steward.state import writer

NOW = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)


def worklist():
    raw = json.loads((Path(__file__).parent / 'fixtures/gsd.worklist.v1.json').read_text())
    base = raw['items'][0]
    raw['items'] = [dict(base, id=str(i), title='Owner task ' + str(i), priority='10', next_actor='user')
                    for i in range(1, 7)]
    # Equal priorities, producer due order differs from raw graph order; project may be inactive.
    raw['priorities']['mine'] = [dict(id=str(i), title='Canonical task ' + str(i), priority='10',
        due_at=f'2026-10-2{rank}T18:00:00+00:00', project='Producer project')
        for rank, i in enumerate((1, 3, 2, 5, 6, 4))]
    raw['priorities']['waiting_on_others'] = [dict(id='external-' + str(i), title='Person task ' + str(i),
        priority=None, due_at='2026-10-21T18:00:00+00:00', project=None) for i in range(3)]
    return raw


class PrioritiesTests(unittest.TestCase):
    setUp = use_settings

    def test_live_worklist_priority_order_survives_steward(self):
        raw = worklist()
        result = senses.gsd({}, NOW, lambda argv: json.dumps(raw))
        self.assertEqual(result['waiting_on_you'], raw['priorities']['mine'])

    def test_waiting_on_other_people_survives_steward(self):
        raw = worklist()
        result = senses.gsd({}, NOW, lambda argv: json.dumps(raw))
        self.assertEqual(result.get('waiting_on_others'), raw['priorities']['waiting_on_others'])

    def test_state_briefing_and_fallback_render_the_same_producer_lists(self):
        raw = worklist()
        projected = senses.gsd({}, NOW, lambda argv: json.dumps(raw))
        with tempfile.TemporaryDirectory() as directory, writer(Path(directory), initialize=True) as st:
            senses.save(st.db, {'gsd': dict(status='ok', data=projected)}, NOW,
                        jobs.change(st.db, 'fixture', NOW, 'tick'))
            state = jobs.snapshot(st.db, now=NOW)
            self.assertEqual(state['senses']['gsd']['data']['waiting_on_you'], raw['priorities']['mine'])
            page = briefing.render(state, NOW, {})
            fallback = messages.fallback(st.db, NOW)
        self.assertIn('## Your items', page)
        self.assertIn('## Waiting on others', page)
        for text in (page, fallback):
            positions = [text.index(row['title']) for row in raw['priorities']['mine']]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(raw['priorities']['mine'][5]['title'], text)
            for row in raw['priorities']['waiting_on_others']:
                self.assertIn(row['title'], text)
        self.assertIn('Producer project', page)
        self.assertLess(fallback.index('gsd unknown:'), fallback.index('Canonical task 1'))

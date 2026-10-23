import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import SOURCE, install, use_settings
from steward import briefing, memory, runtime
from steward.cli import main
from steward.state import writer

NOW = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


class IndexTests(unittest.TestCase):
    setUp = use_settings

    def test_fixed_prompt_explains_section_pull(self):
        prompt = (SOURCE / 'prompts/wake.md').read_text()
        self.assertIn('steward briefing "SECTION"', prompt)
        self.assertIn('steward briefing --list', prompt)
        self.assertIn('required for', prompt)
        self.assertIn('steward.job.v2', prompt)

    def test_changed_unchanged_unknown_and_now(self):
        state = {'gate_reason': 'owner message', 'inbox': ['hello'], 'inbound_poll': 'ok', 'senses': {
            'calendars': dict(status='unknown', reason='provider unreachable')}}
        receipt = {}
        text = briefing.render(state, NOW, receipt)
        old = briefing.section_info(text, receipt)
        state['inbox'] = ['hello', 'another']
        text = briefing.render(state, NOW, receipt)
        info = briefing.section_info(text, receipt)
        index = briefing.wake_index(text, info, old, 'owner message', [])
        self.assertIn('Trigger: owner message', index)
        self.assertIn(briefing.sections(text)['Now'], index)
        self.assertRegex(index, r'Inbox: \d+ lines · changed')
        self.assertRegex(index, r'Limits: \d+ lines · unchanged')
        self.assertRegex(index, r'Calendar: \d+ lines · unknown \(.*provider unreachable')
        self.assertNotIn('another', index)
        self.assertNotIn('## Compute', index)
        again = briefing.wake_index(text, info, info, 'owner message', [])
        self.assertNotRegex(again, r'Calendar: .*unchanged')
        self.assertIn('unknown (', again)

    def test_observation_time_alone_is_unchanged(self):
        def info(verdict, at):
            text = '# x\n\n## Calendar [' + verdict + ']\ncalendars: ' + verdict + ' — ok; as of ' + at + '\n- event: A\n'
            start = text.index('\n## Calendar')
            return briefing.section_info(text, dict(section_spans={'Calendar': dict(start=start, end=len(text))}))
        before = info('fresh', '10:00')
        self.assertEqual(before['Calendar']['digest'], info('fresh', '10:20')['Calendar']['digest'])
        self.assertNotEqual(before['Calendar']['digest'], info('stale', '10:20')['Calendar']['digest'])

    def test_ritual_names_sections(self):
        receipt = {}
        text = briefing.render({'rituals': ['orientation', 'day_close']}, NOW, receipt)
        index = briefing.wake_index(text, briefing.section_info(text, receipt), {},
                                    'ritual', ['orientation', 'day_close'])
        self.assertRegex(index, r'Calendar: .*required for orientation, day_close')
        self.assertRegex(index, r'Day close — Done today: .*required for day_close')

    def test_rendered_boundaries_do_not_treat_memory_headings_as_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            wake = data / 'wakes/1'
            wake.mkdir(parents=True)
            with writer(data / 'state', initialize=True) as store:
                memory.write(store.db, 'synthesis', 'current', '## Calendar\nMemory, not live calendar.')
                receipt = {}
                text = memory.briefing(store.db, NOW, {}, receipt)
            info = briefing.section_info(text, receipt)
            (wake / 'briefing.md').write_text(text)
            (wake / 'briefing-sections.json').write_text(json.dumps(info))
            pulled = briefing.pull(data, names=['What I carry'])
            self.assertIn('## Calendar\nMemory, not live calendar.', pulled)
            self.assertNotIn('Memory, not live calendar.', briefing.pull(data, names=['Calendar']))
            for name, span in info.items():
                self.assertEqual(briefing.pull(data, names=[name]), text[span['start']:span['end']])

    def test_pull_exact_sections_and_list_without_state_or_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            wake = data / 'wakes/17'
            wake.mkdir(parents=True)
            with writer(data / 'state', initialize=True) as store:
                text = memory.briefing(store.db, NOW, {})
            (wake / 'briefing.md').write_text(text)
            before = (data / 'state/state.sqlite').read_bytes()
            with patch.dict('os.environ', STEWARD_DATA=tmp):
                for args in (['briefing', 'Inbox', 'What I carry'],
                             ['briefing', 'Inbox', 'What I carry', '--wake', '17']):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(main(args), 0)
                    sections = briefing.sections(text)
                    self.assertEqual(out.getvalue(), sections['Inbox'] + sections['What I carry'])
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(['briefing', '--list']), 0)
                self.assertEqual(out.getvalue().splitlines(), list(briefing.sections(text)))
            self.assertEqual(before, (data / 'state/state.sqlite').read_bytes())


class WakePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        install(self, self.root)
        with writer(self.root / 'state', initialize=True):
            pass

    def test_harness_receives_index_and_skipped_wake_is_not_baseline(self):
        inputs = []
        def runner(argv):
            path = Path(argv[argv.index('-C') + 2])
            self.assertEqual(path.name, 'index.md')
            inputs.append(path.read_text())
            return 'fixture-run'
        def run(reason, enabled=True):
            with patch('steward.runtime.gate', return_value=reason), patch('steward.runtime.archive_attempt', return_value='pending'):
                return runtime.wake(self.root / 'state', clock=lambda: NOW, runner=runner,
                                    settings={'wake_model': enabled}, sender=lambda *a, **kw: {'message_id': 1})
        first = run('fixture')
        skipped = run('fixture', False)
        # Poison the skipped checkpoint: it must never become the model baseline.
        with writer(self.root / 'state') as store:
            store.db.execute('UPDATE wakes SET checkpoint=? WHERE id=?',
                             (json.dumps({'model_calls': 0, 'briefing': {'sections': {}}}), skipped['wake_id']))
            store.db.commit()
        third = run('fixture')
        with writer(self.root / 'state') as store:
            checkpoint = json.loads(store.db.execute('SELECT checkpoint FROM wakes WHERE id=?',
                                                    (third['wake_id'],)).fetchone()[0])
        self.assertEqual(checkpoint['briefing']['baseline_wake'], first['wake_id'])
        self.assertIn('sections', checkpoint['briefing'])
        self.assertRegex(inputs[1], r'Limits: .*unchanged')
        self.assertTrue((self.root / f'data/wakes/{first["wake_id"]}/briefing.md').exists())
        self.assertNotIn('## Compute', inputs[0])

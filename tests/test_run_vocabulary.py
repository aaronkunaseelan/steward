from fixture_mind import use_settings
import unittest
from steward import jobs


class RunVocabularyTests(unittest.TestCase):
    setUp = use_settings

    def test_native_classification(self):
        for state in ('succeeded', 'completed', 'skipped', 'cancelled'):
            self.assertEqual(jobs.run_severity(state), 'ok')
        for state in ('planned', 'started', 'gated', 'running'):
            self.assertEqual(jobs.run_severity(state), 'running')
        for state in ('failed', 'refused', 'wake_busy', 'new-unhandled-state'):
            self.assertEqual(jobs.run_severity(state), 'bad')

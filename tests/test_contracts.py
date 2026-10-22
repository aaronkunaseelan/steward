from fixture_mind import use_settings
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from steward.cli import main
from steward.contract import problems
from steward.contracts import DIRECTORY, load


class ContractsTests(unittest.TestCase):
    setUp = use_settings

    def test_every_output_contract_has_real_producer_coverage(self):
        # Real-code producer cases, not another copy of their payload shapes.
        import importlib
        cases = {
            'steward.inbox.v1': 'test_messages.MessagesTests.test_outstanding_cli_matches_wake_inbox_and_is_read_only',
            'steward.record.v1': 'test_parcel.ParcelTests.test_read_only_lookup_tells_new_scan_once_and_records',
            'steward.transcript.v1': 'test_transcripts.TranscriptTests.test_real_output_for_both_agents_validates_draft',
            'steward.run-summary.v1': 'test_transcripts.TranscriptTests.test_sweep_writes_edge_summary_and_transcript_once',
        }
        self.assertEqual({p.stem for p in DIRECTORY.glob('*.json')}, set(cases))
        for target in cases.values():
            module, cls, method = target.split('.')
            self.assertTrue(callable(getattr(getattr(importlib.import_module(module), cls), method)))

    def test_sense_fixtures_validate(self):
        for identity in ('gsd.worklist.v1', 'qmd.status.v1'):
            with self.subTest(contract=identity):
                payload = json.loads((Path(__file__).parent / 'fixtures' / (identity + '.json')).read_text())
                self.assertEqual(problems(load(identity, vendor=True), payload), [])

    def compare_installed(self, producer, identity):
        if not shutil.which(producer):
            self.skipTest(producer + ' command not installed')
        help_result = subprocess.run([producer, '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(help_result.returncode, 0, producer + ' --help failed')
        commands = re.findall(r'\{([^}]+)\}', help_result.stdout)
        if not any('contract' in group.split(',') for group in commands):
            self.skipTest(producer + ' contract command not installed')
        result = subprocess.run([producer, 'contract', identity], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, producer + ' contract failed')
        self.assertEqual(json.loads(result.stdout), load(identity, vendor=True))

    def test_installed_gsd_contract_matches_vendor(self):
        self.compare_installed('gsd', 'gsd.worklist.v1')

    def test_installed_qmd_contract_matches_vendor(self):
        self.compare_installed('qmd', 'qmd.status.v1')

    def test_installed_mech_d_intent_contract_matches_vendor(self):
        shipped = Path.home() / '.local/lib/mech-d/current/contracts/intent-1.json'
        if not shipped.is_file():
            self.skipTest('mech-d release with contracts/intent-1.json not installed')
        self.assertEqual(json.loads(shipped.read_text()), load('intent-1', vendor=True))

    def test_own_contracts_print_verbatim_without_state(self):
        for identity, suffix in (('steward.job.v1', '.md'), ('steward.record.v1', '.json'), ('steward.transcript.v1', '.json')):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['contract', identity]), 0)
            self.assertEqual(output.getvalue(), (DIRECTORY / (identity + suffix)).read_text())

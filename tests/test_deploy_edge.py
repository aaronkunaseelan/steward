from fixture_mind import use_settings
"""Deploy secret plumbing, using only synthetic credentials and no deployment."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('deploy_edge', str(Path(__file__).resolve().parents[1] / 'bin/deploy-edge'))
spec = importlib.util.spec_from_loader(loader.name, loader)
deploy = importlib.util.module_from_spec(spec)
loader.exec_module(deploy)


class DeploySecrets(unittest.TestCase):
    setUp = use_settings

    def test_shared_password_and_stable_private_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'home', return_value=Path(tmp)):
            config = Path(tmp) / '.config'
            (config / 'steward').mkdir(parents=True)
            (config / 'mech-d').mkdir()
            pages = config / 'steward/pages.env'
            pages.write_text('STEWARD_PAGES_TOKEN=synthetic-token\n')
            credentials = config / 'mech-d/credentials.json'
            credentials.write_text(json.dumps({'PAGE_PASSWORD':'synthetic-password'}))
            first = deploy.page_secrets()
            self.assertEqual(first['PAGE_PASSWORD'], 'synthetic-password')
            self.assertEqual(first['STEWARD_PAGES_TOKEN'], 'synthetic-token')
            self.assertGreaterEqual(len(first['SESSION_SECRET']), 32)
            self.assertEqual(pages.stat().st_mode & 0o777, 0o600)
            credentials.write_text(json.dumps({'PAGE_PASSWORD':'changed-password'}))
            second = deploy.page_secrets()
            self.assertEqual(second['SESSION_SECRET'], first['SESSION_SECRET'])
            self.assertEqual(second['PAGE_PASSWORD'], 'changed-password')
            self.assertEqual(pages.read_text().count('SESSION_SECRET='), 1)

    def test_missing_password_does_not_generate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'home', return_value=Path(tmp)):
            with self.assertRaises(SystemExit):
                deploy.page_secrets()

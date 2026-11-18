"""Deploy secret plumbing, using only synthetic credentials and no deployment."""
from fixture_mind import use_settings
import importlib.machinery
import importlib.util
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

    def test_only_the_pages_token_is_uploaded(self):
        # People sign in through Cloudflare Access (cloudflare D1/P3): no page password or session key goes up.
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'home', return_value=Path(tmp)):
            pages = Path(tmp) / '.config/steward/pages.env'
            pages.parent.mkdir(parents=True)
            original = 'STEWARD_PAGES_TOKEN=synthetic-token\nSESSION_SECRET=left-over\n'
            pages.write_text(original)
            self.assertEqual(deploy.page_secrets(), {'STEWARD_PAGES_TOKEN': 'synthetic-token'})
            self.assertEqual(pages.read_text(), original)

    def test_missing_token_stops_the_deploy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'home', return_value=Path(tmp)):
            pages = Path(tmp) / '.config/steward/pages.env'
            pages.parent.mkdir(parents=True)
            pages.write_text('OTHER=1\n')
            with self.assertRaisesRegex(SystemExit, '^Pages token unavailable$'):
                deploy.page_secrets()


if __name__ == '__main__':
    unittest.main()

"""Runtime tokens are injected by the service, never discovered in config files."""
from fixture_mind import use_settings
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import notify, pages
from steward.state import StateError


class ProviderCredentialsTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(directory) / 'steward'
        self.config.mkdir()
        (self.config / 'pages.env').write_text('STEWARD_PAGES_TOKEN=synthetic-file-pages\n')
        telegram = self.config / 'telegram.env'
        telegram.write_text('STEWARD_TELEGRAM_BOT_TOKEN=synthetic-file-telegram\n')
        (self.config / 'telegram.toml').write_text(f'token_env_file = "{telegram}"\n')
        self.enterContext(patch.dict(os.environ, XDG_CONFIG_HOME=directory))
        os.environ.pop('STEWARD_PAGES_TOKEN', None)
        os.environ.pop('STEWARD_TELEGRAM_BOT_TOKEN', None)

    def test_files_do_not_supply_missing_environment_tokens(self):
        for token, message in ((pages.token, 'pages token unavailable'),
                               (notify.bot_token, 'Telegram token unavailable')):
            with self.subTest(token=token.__name__), self.assertRaisesRegex(StateError, message):
                token()

    def test_missing_tokens_fail_before_any_provider_transport(self):
        with patch('urllib.request.build_opener', side_effect=AssertionError('page transport attempted')) as opener, \
             patch('urllib.request.urlopen', side_effect=AssertionError('Telegram transport attempted')) as urlopen:
            with self.assertRaisesRegex(StateError, 'pages token unavailable'):
                pages.request('PUT', '/_api/pages/credential-fixture', data=b'fixture')
            with self.assertRaisesRegex(StateError, 'Telegram token unavailable'):
                notify.api('sendMessage', {'text': 'fixture'})
            opener.assert_not_called()
            urlopen.assert_not_called()

    def test_environment_tokens_need_no_config_read(self):
        with patch.dict(os.environ, STEWARD_PAGES_TOKEN='synthetic-env-pages',
                        STEWARD_TELEGRAM_BOT_TOKEN='synthetic-env-telegram'), \
             patch('steward.runtime.config_dir', side_effect=AssertionError('config read attempted')), \
             patch('steward.notify.binding', side_effect=AssertionError('binding read attempted')):
            self.assertEqual(pages.token(), 'synthetic-env-pages')
            self.assertEqual(notify.bot_token(), 'synthetic-env-telegram')

    def test_services_load_page_and_telegram_tokens_from_environment_files(self):
        root = Path(__file__).resolve().parents[1]
        for service in ('steward-tick.service', 'steward-wake.service'):
            with self.subTest(service=service):
                section, files = None, []
                for line in (root / 'systemd' / service).read_text().splitlines():
                    line = line.strip()
                    if line.startswith('['):
                        section = line
                    key, _, value = line.partition('=')
                    if section == '[Service]' and key.strip() == 'EnvironmentFile':
                        files.extend(value.split())
                self.assertIn('%h/.config/steward/telegram.env', files)
                self.assertIn('%h/.config/steward/pages.env', files)

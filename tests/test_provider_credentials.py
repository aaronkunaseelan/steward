"""Runtime tokens are injected by the service, never discovered in config files."""
from fixture_mind import use_settings
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import notify, pages, service
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

    def service_files(self):
        secrets = self.config.parent / 'agent-secrets'
        secrets.mkdir()
        (secrets / 'credentials.env').write_text(
            'GOG_KEYRING_PASSWORD=synthetic-gog\nUNRELATED_SECRET=synthetic-extra\n'
            'MALFORMED_EXTRA="ignored\n')
        with (self.config / 'pages.env').open('a') as stream:
            stream.write('SESSION_SECRET=synthetic-edge-only\n')
        return secrets

    def test_unit_loader_reads_only_listed_names_from_original_files(self):
        self.service_files()
        self.assertEqual(service.credentials(), {
            'GOG_KEYRING_PASSWORD': 'synthetic-gog',
            'STEWARD_PAGES_TOKEN': 'synthetic-file-pages',
            'STEWARD_TELEGRAM_BOT_TOKEN': 'synthetic-file-telegram',
        })

    def test_unit_exec_receives_credentials_not_argv_or_extra_secrets(self):
        self.service_files()
        with patch.dict(os.environ, {'PATH': '/usr/bin', 'HOME': str(self.config.parent)}, clear=True), \
             patch('steward.service.config_dir', return_value=self.config), \
             patch('sys.argv', ['steward.service', 'tick', '--state', 'fixture']), \
             patch('os.execvpe') as execute:
            service.main()
        _, argv, env = execute.call_args.args
        self.assertEqual(argv[1:], ['-m', 'steward', 'tick', '--state', 'fixture'])
        self.assertIn('GOG_KEYRING_PASSWORD', env)
        for name in ('UNRELATED_SECRET', 'MALFORMED_EXTRA', 'SESSION_SECRET'):
            self.assertNotIn(name, env)
        self.assertNotIn('synthetic', ' '.join(argv))

    def test_unit_loader_quotes_are_literal_and_required_errors_are_redacted(self):
        secrets = self.service_files()
        path = secrets / 'credentials.env'
        path.write_text('GOG_KEYRING_PASSWORD="synthetic $literal # value"\n')
        self.assertEqual(service.credentials()['GOG_KEYRING_PASSWORD'], 'synthetic $literal # value')
        for content in ('UNRELATED_SECRET=synthetic-extra\n', 'GOG_KEYRING_PASSWORD=\n',
                        'GOG_KEYRING_PASSWORD="synthetic-unclosed\n'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(StateError) as error:
                service.credentials()
            self.assertIn('GOG_KEYRING_PASSWORD', str(error.exception))
            self.assertNotIn('synthetic', str(error.exception))

    def test_services_use_loader_without_broad_environment_files(self):
        root = Path(__file__).resolve().parents[1]
        for verb in ('tick', 'wake'):
            unit = (root / 'systemd' / ('steward-' + verb + '.service')).read_text()
            with self.subTest(verb=verb):
                self.assertIn('-m steward.service ' + verb, unit)
                self.assertNotIn('EnvironmentFile=', unit)

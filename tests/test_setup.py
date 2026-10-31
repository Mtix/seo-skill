import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'seo-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('seo_setup', SCRIPTS / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_private_storage_and_symlink_rejection(self):
        root, secrets = setup.prepare(self.root)
        path = secrets / 'test.json'
        setup.write_private(path, {'token': 'dummy'})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(secrets.stat().st_mode), 0o700)
        self.assertIn('/.seo/', (self.root / '.gitignore').read_text())
        target = self.root / 'target'
        target.write_text('unchanged')
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(setup.SetupError):
            setup.write_private(path, {})
        self.assertEqual(target.read_text(), 'unchanged')

    def test_yandex_verification_selection_and_secret_separation(self):
        _, secrets = setup.prepare(self.root)
        env = self.root / '.env'
        env.write_text('YANDEX_WEBMASTER_TOKEN=dummy-secret\n')
        hosts = {'hosts': [
            {'host_id': 'bad', 'ascii_host_url': 'https://bad.test/', 'verified': False},
            {'host_id': 'https:example.com:443', 'ascii_host_url': 'https://example.com/', 'verified': True}]}
        with patch.object(setup, 'api_get', side_effect=[{'user_id': 123}, hosts]) as api:
            entry = setup.connect_yandex(secrets, env, 'https://example.com/', False)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[1], 'OAuth dummy-secret')
        self.assertEqual(entry['user_id'], '123')
        self.assertNotIn('dummy-secret', json.dumps(entry))
        self.assertEqual(json.loads((secrets/'yandex-token.json').read_text())['token'], 'dummy-secret')

    def test_bad_access_does_not_replace_previous_token(self):
        _, secrets = setup.prepare(self.root)
        setup.write_private(secrets/'yandex-token.json', {'token': 'old-secret'})
        env = self.root / '.env'
        env.write_text('YANDEX_WEBMASTER_TOKEN=new-secret\n')
        with patch.object(setup, 'api_get', side_effect=setup.SetupError('Denied')):
            with self.assertRaises(setup.SetupError):
                setup.connect_yandex(secrets, env, None, False)
        self.assertEqual(json.loads((secrets/'yandex-token.json').read_text())['token'], 'old-secret')

    def test_both_preserves_partial_success_and_reports_failure(self):
        entry = {'property': 'sc-domain:example.com', 'checked_at': 'today'}
        with patch.object(setup, 'connect_google', return_value=entry), patch.object(setup, 'connect_yandex', side_effect=setup.SetupError('Denied')):
            with self.assertRaises(SystemExit) as error:
                setup.main(['init', '--project', str(self.root), '--provider', 'both'])
        self.assertEqual(error.exception.code, 1)
        saved = json.loads((self.root/'.seo/connections.json').read_text())
        self.assertEqual(saved['connections'], {'google': entry})

    def test_api_redirect_and_destination_protection(self):
        with self.assertRaises(setup.SetupError):
            setup.api_get('https://evil.test/', 'Bearer dummy')
        self.assertIsNone(setup.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test/'))

    def test_google_pkce_loopback_and_private_token(self):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            self.skipTest('Google dependencies not installed')
        from unittest.mock import Mock
        _, secrets = setup.prepare(self.root)
        client = self.root / 'desktop.json'
        client.write_text(json.dumps({'installed': {
            'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'dummy-client',
            'auth_uri': setup.AUTH_URI, 'token_uri': setup.TOKEN_URI,
            'redirect_uris': ['http://localhost']}}))
        creds = Mock(valid=True, refresh_token='dummy-refresh', token='dummy-access', granted_scopes=[setup.SCOPE])
        creds.to_json.return_value = json.dumps({'token': 'dummy-access', 'refresh_token': 'dummy-refresh'})
        with patch.object(InstalledAppFlow, 'from_client_config') as factory, patch.object(setup, 'api_get', return_value={
            'siteEntry': [{'siteUrl': 'sc-domain:example.com', 'permissionLevel': 'siteOwner'}]}):
            factory.return_value.run_local_server.return_value = creds
            entry = setup.connect_google(secrets, client, 'sc-domain:example.com', False)
            self.assertTrue(factory.call_args.kwargs['autogenerate_code_verifier'])
            options = factory.return_value.run_local_server.call_args.kwargs
            self.assertEqual(options['host'], '127.0.0.1')
            self.assertEqual(options['port'], 0)
            self.assertIsNone(options['authorization_prompt_message'])
            self.assertEqual(factory.call_args.kwargs['scopes'], [setup.SCOPE])
        self.assertNotIn('dummy', json.dumps(entry))
        self.assertEqual(stat.S_IMODE((secrets/'google-token.json').stat().st_mode), 0o600)

    def test_google_rejects_alternative_oauth_endpoints(self):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            self.skipTest('Google dependencies not installed')
        _, secrets = setup.prepare(self.root)
        client = self.root / 'desktop.json'
        client.write_text(json.dumps({'installed': {'client_id': 'test', 'auth_uri': 'https://evil.test', 'token_uri': setup.TOKEN_URI}}))
        with patch.object(InstalledAppFlow, 'from_client_config') as factory:
            with self.assertRaises(setup.SetupError):
                setup.google_credentials(secrets, client)
            factory.assert_not_called()

if __name__ == '__main__':
    unittest.main()

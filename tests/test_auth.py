import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import baidu_auth as auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'credentials.dpapi'
        self.value = {'version': 1, 'client_id': 'test-client', 'client_secret': 'test-secret',
                      'refresh_token': 'test-refresh', 'access_token': 'test-access',
                      'expires_at': 2000}

    def tearDown(self):
        self.temp.cleanup()

    def client(self, payload, status=200):
        session = Mock()
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        return auth.BaiduClient(self.path, session=session, now=lambda: 1000)

    def test_dpapi_roundtrip_and_no_plaintext(self):
        auth.save_credentials(self.value, self.path)
        self.assertEqual(auth.load_credentials(self.path), self.value)
        self.assertNotIn(b'test-secret', self.path.read_bytes())
        self.assertNotIn(b'test-refresh', self.path.read_bytes())

    def test_cached_token_does_not_contact_network(self):
        auth.save_credentials(self.value, self.path)
        client = self.client({})
        self.assertEqual(client.get_access_token(), 'test-access')
        client.session.get.assert_not_called()

    def test_refresh_rotates_and_persists_credentials(self):
        auth.save_credentials(self.value, self.path)
        client = self.client({'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600})
        self.assertEqual(client.get_access_token(force_refresh=True), 'new-access')
        saved = auth.load_credentials(self.path)
        self.assertEqual(saved['refresh_token'], 'new-refresh')
        self.assertEqual(saved['expires_at'], 4600)

    def test_auth_code_is_exchanged_and_never_saved(self):
        client = self.client({'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600})
        client.authorize('test-client', 'test-secret', 'one-time-code')
        self.assertNotIn('one-time-code', json.dumps(auth.load_credentials(self.path)))
        params = client.session.get.call_args.kwargs['params']
        self.assertEqual(params['grant_type'], 'authorization_code')
        self.assertEqual(params['redirect_uri'], 'oob')

    def test_error_does_not_echo_server_secrets_or_replace_credentials(self):
        auth.save_credentials(self.value, self.path)
        before = self.path.read_bytes()
        client = self.client({'error': 'invalid_grant', 'error_description': 'test-secret one-time-code'}, 400)
        with self.assertRaises(auth.BaiduClientError) as caught:
            client.authorize('test-client', 'test-secret', 'one-time-code')
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('one-time-code', str(caught.exception))
        self.assertEqual(before, self.path.read_bytes())

    def test_authorization_url_scopes_and_no_secret(self):
        query = parse_qs(urlsplit(auth.authorization_url('test-client')).query)
        self.assertEqual(query['scope'], ['basic,netdisk'])
        self.assertEqual(query['redirect_uri'], ['oob'])
        self.assertNotIn('client_secret', query)

    def test_corrupt_storage_is_redacted(self):
        self.path.write_bytes(b'not-dpapi')
        with self.assertRaises(auth.BaiduClientError):
            auth.load_credentials(self.path)


if __name__ == '__main__':
    unittest.main()

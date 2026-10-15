import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch, Mock

from native_auth import NativeAuth, NoRedirect, PROFILE_URL, NativeAuthUnavailable

TOKEN = 'Bearer sk-ant-oat01-fixture-native-token-for-tests'


class NativeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        path = self.root / 'gmail.json'
        path.write_text(json.dumps({'shuntAccountUuid':'user', 'shuntOrganizationUuid':'personal-org', 'claudeAiOauth':{'accessToken':'fixture-store-token'}}))
        path.chmod(0o600)
        self.auth = NativeAuth(self.root, ['gmail'])

    def tearDown(self): self.temp.cleanup()

    def response(self, org='personal-org'):
        return io.BytesIO(json.dumps({'account':{'uuid':'user'}, 'organization':{'uuid':org}}).encode())

    def test_provider_verified_identity_is_cached_as_fingerprint_only(self):
        with patch.object(self.auth.opener, 'open', return_value=self.response()) as request:
            self.assertEqual(self.auth.authenticate(TOKEN), 'gmail')
            self.assertEqual(self.auth.authenticate(TOKEN), 'gmail')
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0].full_url, PROFILE_URL)
            self.assertEqual(request.call_args.args[0].get_header('Authorization'), TOKEN)
        self.assertNotIn(TOKEN, str(self.auth.cache))
        self.assertNotIn('fixture-store-token', str(self.auth.identities))

    def test_other_organization_expired_token_and_network_failure_fail_closed(self):
        with patch.object(self.auth.opener,'open',return_value=self.response('company-org')):
            self.assertIsNone(self.auth.authenticate(TOKEN))
        self.auth.cache.clear()
        with patch.object(self.auth.opener,'open',side_effect=TimeoutError):
            with self.assertRaises(NativeAuthUnavailable):self.auth.authenticate(TOKEN)

    def test_local_control_tokens_and_malformed_credentials_are_never_sent_upstream(self):
        with patch.object(self.auth.opener,'open') as request:
            for value in ['Bearer local-private-client-token', '', 'Basic secret', TOKEN+'\r\nX-Other: value', TOKEN*1000]:
                self.assertIsNone(self.auth.authenticate(value))
            request.assert_not_called()

    def test_expired_login_is_unauthorized_but_provider_outage_is_retryable(self):
        for code in (401, 403, 429, 503):
            self.auth.cache.clear()
            error = urllib.error.HTTPError(PROFILE_URL, code, 'fixture', {}, None)
            with patch.object(self.auth.opener, 'open', side_effect=error):
                if code in (401, 403): self.assertIsNone(self.auth.authenticate(TOKEN))
                else:
                    with self.assertRaises(NativeAuthUnavailable): self.auth.authenticate(TOKEN)
            error.close()
        self.auth.cache.clear()
        with patch.object(self.auth.opener, 'open', return_value=io.BytesIO(b'[]')):
            with self.assertRaises(NativeAuthUnavailable): self.auth.authenticate(TOKEN)

    def test_unsafe_store_and_unknown_account_cannot_become_native_identities(self):
        (self.root/'gmail.json').chmod(0o644)
        auth = NativeAuth(self.root,['gmail','../outside'])
        self.assertEqual(auth.identities,{})

    def test_redirects_cannot_forward_the_native_login(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'Found',{},'https://unrelated.example/'))

    def test_verification_concurrency_and_cache_size_are_bounded(self):
        self.auth.slots.acquire();self.auth.slots.acquire()
        with patch.object(self.auth.opener,'open') as request:
            with self.assertRaises(NativeAuthUnavailable):self.auth.authenticate(TOKEN)
            request.assert_not_called()
        self.auth.slots.release();self.auth.slots.release()
        for i in range(70):
            with patch.object(self.auth.opener,'open',return_value=self.response()):
                self.assertEqual(self.auth.authenticate(TOKEN+str(i)),'gmail')
        self.assertLessEqual(len(self.auth.cache),64)


if __name__ == '__main__': unittest.main()

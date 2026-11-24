"""Resolve one registered ZenTao credential inside redirected Windows profiles."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import task_remote


class CredentialPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.redirected = self.root / 'mxc-local'
        self.profile = self.root / 'user'
        self.real_local = self.profile / 'AppData' / 'Local'
        self.cfg = {'base_url': 'http://localhost:8088', 'credential_ref': 'windows-dpapi:dotflow/zentao'}
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.redirected),
                                          'USERPROFILE': str(self.profile)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def credential(self, local, username='fixture-user', base_url='http://localhost:8088'):
        path = local / 'DotFlow' / 'credentials' / 'zentao.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'protection': 'windows-dpapi-current-user',
                                   'base_url': base_url, 'username': username,
                                   'encrypted_password': '00'}), encoding='utf-8')
        return path

    def resolve(self):
        # The cryptographic OS boundary is mocked, never real credentials.
        with patch.object(task_remote, '_dpapi', return_value='test-only-secret'.encode('utf-16-le')):
            return task_remote._credentials(self.cfg)

    def test_redirected_localappdata_falls_back_to_same_user_registered_path(self):
        self.credential(self.real_local)
        try:
            actual = self.resolve()
        except (FileNotFoundError, KeyError) as error:
            self.fail('Registered current-user credential was not resolved: ' + type(error).__name__)
        self.assertEqual(actual, ('fixture-user', 'test-only-secret'))

    def test_existing_localappdata_credential_keeps_priority(self):
        self.credential(self.redirected, username='environment-user')
        self.credential(self.real_local, username='profile-user')
        self.assertEqual(self.resolve()[0], 'environment-user')

    def test_instance_mismatch_does_not_select_a_different_credential(self):
        self.credential(self.redirected, base_url='http://another-instance.invalid')
        self.credential(self.real_local)
        with self.assertRaises(task_remote.RemoteRejected):
            self.resolve()

    def test_absent_localappdata_can_still_resolve_same_profile(self):
        self.credential(self.real_local)
        with patch.dict(os.environ):
            os.environ.pop('LOCALAPPDATA', None)
            try:
                actual = self.resolve()
            except (FileNotFoundError, KeyError) as error:
                self.fail('Current-user fallback was not used: ' + type(error).__name__)
            self.assertEqual(actual[0], 'fixture-user')

    def test_missing_registered_file_does_not_scan_other_user_profiles(self):
        self.credential(self.root / 'another-user' / 'AppData' / 'Local')
        with self.assertRaises((FileNotFoundError, task_remote.RemoteRejected)):
            self.resolve()

    def test_unsupported_reference_cannot_use_the_profile_fallback(self):
        self.credential(self.real_local)
        self.cfg['credential_ref'] = 'windows-dpapi:some-other-application'
        with self.assertRaises(task_remote.RemoteRejected):
            self.resolve()


if __name__ == '__main__':
    unittest.main()

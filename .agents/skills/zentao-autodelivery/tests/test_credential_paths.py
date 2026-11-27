"""Local login configuration for all worker platforms."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import task_remote


class CredentialPathTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'zentao-credentials.json'
        self.cfg = {'base_url': 'http://localhost:8088'}
        self.secret = '  fixture-only  '
        self.record = {**self.cfg, 'username': 'fixture', 'password': self.secret}
        self.patch = patch.object(task_remote, 'CREDENTIAL_PATH', self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_reads_project_local_json_without_platform_or_reference(self):
        self.path.write_text(json.dumps(self.record), encoding='utf-8')
        self.assertEqual(task_remote._credentials(self.cfg), ('fixture', self.secret))

    def test_never_uses_credentials_for_a_different_instance(self):
        self.path.write_text(json.dumps({**self.record, 'base_url': 'https://other.invalid'}))
        with self.assertRaises(task_remote.RemoteRejected) as caught:
            task_remote._credentials(self.cfg)
        self.assertNotIn(self.secret, str(caught.exception))

    def test_missing_malformed_or_empty_password_has_safe_message(self):
        for value in (None, 'invalid', json.dumps({**self.record, 'password': ''})):
            if value is not None:
                self.path.write_text(value)
            with self.assertRaises(task_remote.RemoteRejected) as caught:
                task_remote._credentials(self.cfg)
            self.assertNotIn(self.secret, str(caught.exception))

import os
import tempfile
import unittest

from reviewtrail.config import Config
from reviewtrail.vault import FileVault
from tools.vault_export import export


class ReviewerHighFindingsTest(unittest.TestCase):
    def test_application_vault_is_write_only_and_stdlib_export_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'contacts.jsonl')
            vault = FileVault(path, 'offline-key', 'dedupe-key')
            token = vault.put('reader', 'person@example.test')
            self.assertFalse(hasattr(vault, 'decrypt'))
            with open(path, encoding='utf-8') as source:
                self.assertNotIn('person@example.test', source.read())
            self.assertEqual(export(path, 'offline-key')[0]['value'], 'person@example.test')
            self.assertTrue(vault.forget(token))

    def test_fake_oauth_rejects_non_loopback_requests(self):
        config = Config('x' * 32, ':memory:', '/tmp/vault', 'fake', '', '', '', 'test', False, False, 365)
        config.assert_request_allowed('127.0.0.1')
        with self.assertRaises(RuntimeError):
            config.assert_request_allowed('203.0.113.10')

    def test_production_requires_explicit_proxy_configuration(self):
        config = Config('x' * 32, 'db', '/vault', 'github', 'id', 'secret', 'dpo@example.test', 'prod', True, True, 365, '', True, 1, 'vault-key')
        with self.assertRaises(RuntimeError):
            config.check()

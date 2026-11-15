import io
import json
import os
import tempfile
import unittest
from datetime import date
from reviewtrail.auth import GitHubOAuth, OAuthError
from reviewtrail.reviews import has_sensitive, validate
from reviewtrail.vault import FileVault


class Response:
    def __init__(self, value):
        self.value = value
    def read(self):
        return json.dumps(self.value).encode()


class SecurityFindingsTest(unittest.TestCase):
    def test_github_error_and_timeout(self):
        calls = []
        def opener(request, timeout=None):
            calls.append(timeout)
            return Response({'error': 'bad_verification_code'})
        with self.assertRaises(OAuthError):
            GitHubOAuth('id', 'secret', opener=opener).exchange('bad', 'state')
        self.assertEqual(calls, [10])

    def test_sensitive_thresholds(self):
        self.assertTrue(has_sensitive('call 12345678'))
        self.assertTrue(has_sensitive('0123456789abcdef0123456789abcdef'))

    def test_other_harness_and_ascii_numbers(self):
        form = {'model_slug': 'm', 'model_version': 'version-1', 'task_category': 'coding', 'task_description': 'A repeatable task description long enough for validation.', 'stack': 'python', 'harness': 'other', 'harness_other': 'custom runner', 'outcome': '5', 'retries': '0', 'used_on': '2026-10-24'}
        review, errors = validate(form, date(2026, 10, 24), {'m'})
        self.assertFalse(errors)
        self.assertEqual(review.harness, 'other:custom runner')
        form['outcome'] = '\u0665'
        self.assertIn('outcome', validate(form, date(2026, 10, 24), {'m'})[1])

    def test_vault_encrypts_and_tombstones(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'contacts.jsonl')
            vault = FileVault(path, 'key')
            token = vault.put('reader', 'person@example.test')
            with open(path, encoding='utf-8') as source:
                self.assertNotIn('person@example.test', source.read())
            self.assertTrue(vault.forget(token))
            with open(path, encoding='utf-8') as source:
                self.assertIn('tombstone', source.read())

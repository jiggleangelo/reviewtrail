import io
import os
import tempfile
import unittest
import urllib.parse
from datetime import date

from reviewtrail.auth import FakeOAuth, unpack
from reviewtrail.config import Config
from reviewtrail.db import connect, init
from reviewtrail.vault import FileVault, MemoryVault
from reviewtrail.web import App
from tools.vault_export import export


class HighFindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'db')
        init(self.db_path)
        self.config = Config('x' * 32, self.db_path, self.tmp.name, 'fake', '', '', 'dpo@example.test', 'test', True, False, 365, 'X-Forwarded-For', True, 2)
        self.app = App(self.config, MemoryVault(), FakeOAuth())

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, path='/', method='GET', body='', cookie='', query='', forwarded=None, remote='127.0.0.1'):
        result = {}
        encoded = body.encode()
        environ = {
            'REQUEST_METHOD': method,
            'PATH_INFO': path,
            'QUERY_STRING': query,
            'wsgi.input': io.BytesIO(encoded),
            'CONTENT_LENGTH': str(len(encoded)),
            'REMOTE_ADDR': remote,
            'HTTP_COOKIE': cookie,
        }
        if forwarded is not None:
            environ['HTTP_X_FORWARDED_FOR'] = forwarded
        data = b''.join(self.app(environ, lambda status, headers: result.update(status=status, headers=headers)))
        return result, data

    def cookie_value(self, response):
        for name, value in response['headers']:
            if name.lower() == 'set-cookie' and value.startswith('rt_session='):
                return value.split(';', 1)[0]
        return ''

    def authenticate(self):
        login, _ = self.call('/login')
        cookie = self.cookie_value(login)
        location = dict(login['headers'])['Location']
        query = urllib.parse.urlsplit(location).query
        callback, _ = self.call('/oauth/callback', cookie=cookie, query=query)
        return login, callback, self.cookie_value(callback)

    def test_proxy_hops_are_counted_from_the_right(self):
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR': 'proxy', 'HTTP_X_FORWARDED_FOR': 'forged, client, edge'}), 'client')
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR': 'proxy', 'HTTP_X_FORWARDED_FOR': 'forged'}), 'proxy')
        self.config.trusted_proxy_header = ''
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR': 'direct', 'HTTP_X_FORWARDED_FOR': 'forged'}), 'direct')

    def test_login_redirects_with_real_schema(self):
        response, _ = self.call('/login')
        self.assertEqual(response['status'][:3], '303')
        self.assertTrue(self.cookie_value(response))
        with connect(self.db_path) as db:
            state = db.execute('SELECT session_nonce FROM oauth_states').fetchone()
            self.assertIsNotNone(state)
            self.assertIsNotNone(db.execute('SELECT 1 FROM sessions WHERE nonce=?', (state['session_nonce'],)).fetchone())

    def test_oauth_state_is_one_time_and_session_rotates(self):
        login, _ = self.call('/login')
        anonymous_cookie = self.cookie_value(login)
        anonymous = unpack(self.app.session_key, anonymous_cookie.split('=', 1)[1])
        location = dict(login['headers'])['Location']
        query = urllib.parse.urlsplit(location).query
        callback, _ = self.call('/oauth/callback', cookie=anonymous_cookie, query=query)
        self.assertEqual(callback['status'][:3], '303')
        authenticated_cookie = self.cookie_value(callback)
        authenticated = unpack(self.app.session_key, authenticated_cookie.split('=', 1)[1])
        self.assertNotEqual(anonymous['csrf'], authenticated['csrf'])
        self.assertNotEqual(anonymous.get('sid'), authenticated.get('sid'))
        self.assertTrue(authenticated['uid'])
        replay, _ = self.call('/oauth/callback', cookie=anonymous_cookie, query=query)
        self.assertEqual(replay['status'][:3], '403')

    def test_oauth_rejects_mismatched_state(self):
        login, _ = self.call('/login')
        cookie = self.cookie_value(login)
        rejected, _ = self.call('/oauth/callback', cookie=cookie, query='code=alice-old&state=attacker')
        self.assertEqual(rejected['status'][:3], '403')

    def test_model_fixture_accepts_required_columns_with_real_schema(self):
        with connect(self.db_path) as db:
            db.execute("INSERT INTO models(slug,name,vendor,kind) VALUES('m','M','V','model')")
            db.commit()
        with connect(self.db_path) as db:
            model = db.execute(
                'SELECT slug, name, vendor, kind FROM models WHERE slug=?',
                ('m',),
            ).fetchone()
            self.assertIsNotNone(model)
            self.assertEqual(tuple(model), ('m', 'M', 'V', 'model'))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_login_is_pseudonymous_until_review_opt_in(self):
        _, callback, cookie = self.authenticate()
        self.assertEqual(callback['status'][:3], '303')
        authenticated = unpack(self.app.session_key, cookie.split('=', 1)[1])
        self.assertNotIn('login', authenticated)
        with connect(self.db_path) as db:
            self.assertIsNone(db.execute('SELECT display_name FROM users WHERE id=?', (authenticated['uid'],)).fetchone()['display_name'])
            db.execute("INSERT INTO models(slug,name,vendor,kind) VALUES('m','M','V','model')")
            db.commit()

        form = {
            'csrf': authenticated['csrf'],
            'model_slug': 'm',
            'model_version': 'version-1',
            'task_category': 'coding',
            'task_description': 'A repeatable task description long enough for validation.',
            'stack': 'python',
            'harness': 'cli-agent',
            'outcome': '5',
            'retries': '0',
            'cost_usd': '1.00',
            'used_on': date.today().isoformat(),
            'proof_line': 'local run',
            'consent': 'on',
            'show_handle': 'on',
        }
        response, _ = self.call('/review', 'POST', urllib.parse.urlencode(form), cookie)
        self.assertEqual(response['status'][:3], '303')
        with connect(self.db_path) as db:
            self.assertEqual(db.execute('SELECT display_name FROM users WHERE id=?', (authenticated['uid'],)).fetchone()['display_name'], 'alice')

    def test_vault_uses_authenticated_encryption_and_erases_ciphertext(self):
        path = os.path.join(self.tmp.name, 'contacts.jsonl')
        vault = FileVault(path, 'stable-vault-key')
        token = vault.put('reader', 'person@example.test')
        with open(path, encoding='utf-8') as source:
            before = source.read()
        self.assertNotIn('person@example.test', before)
        self.assertFalse(hasattr(vault, 'decrypt'))
        self.assertEqual(export(path, 'stable-vault-key')[0]['value'], 'person@example.test')
        self.assertTrue(vault.forget(token))
        with open(path, encoding='utf-8') as source:
            after = source.read()
        self.assertNotIn('ciphertext', after)
        self.assertIn('tombstone', after)

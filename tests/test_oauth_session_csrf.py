import io
import os
import tempfile
import unittest
import urllib.parse
from contextlib import closing

from reviewtrail.auth import FakeOAuth, state_digest, unpack
from reviewtrail.config import Config
from reviewtrail.db import connect, init
from reviewtrail.vault import MemoryVault
from reviewtrail.web import App


class OAuthSessionCsrfTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = os.path.join(temporary.name, 'db')
        init(self.db_path)
        config = Config(
            'x' * 32, self.db_path, temporary.name, 'fake', '', '',
            'dpo@example.test', 'test', True, False, 365,
        )
        self.app = App(config, MemoryVault(), FakeOAuth())

    def request(self, path, cookie='', query='', method='GET', form=None):
        body = urllib.parse.urlencode(form or {}).encode()
        environ = {
            'REQUEST_METHOD': method,
            'PATH_INFO': path,
            'QUERY_STRING': query,
            'HTTP_COOKIE': cookie,
            'REMOTE_ADDR': '127.0.0.1',
            'CONTENT_LENGTH': str(len(body)),
            'wsgi.input': io.BytesIO(body),
        }
        response = {}
        b''.join(self.app(
            environ,
            lambda status, headers: response.update(status=status, headers=dict(headers)),
        ))
        return response

    def decode_cookie(self, response):
        cookie = response['headers']['Set-Cookie'].split(';', 1)[0]
        session = unpack(self.app.session_key, cookie.split('=', 1)[1])
        self.assertIsNotNone(session)
        return cookie, session

    def test_f1_login_and_callback_persist_cookie_csrf_with_real_schema(self):
        login = self.request('/login')
        self.assertEqual(login['status'][:3], '303')
        anonymous_cookie, anonymous = self.decode_cookie(login)
        self.assertTrue(anonymous['csrf'])
        query = urllib.parse.urlsplit(login['headers']['Location']).query
        state = urllib.parse.parse_qs(query)['state'][0]
        self.assertEqual(anonymous['oauth_state'], state)

        with closing(connect(self.db_path)) as db:
            row = db.execute(
                'SELECT user_id, csrf FROM sessions WHERE nonce=?',
                (anonymous['sid'],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row['user_id'])
            self.assertEqual(row['csrf'], anonymous['csrf'])
            pending = db.execute(
                'SELECT session_nonce FROM oauth_states WHERE state_digest=?',
                (state_digest(self.app.state_key, state),),
            ).fetchone()
            self.assertIsNotNone(pending)
            self.assertEqual(pending['session_nonce'], anonymous['sid'])

        callback = self.request('/oauth/callback', anonymous_cookie, query)
        self.assertEqual(callback['status'][:3], '303')
        self.assertEqual(callback['headers']['Location'], '/account')
        authenticated_cookie, authenticated = self.decode_cookie(callback)
        self.assertTrue(authenticated['uid'])
        self.assertTrue(authenticated['csrf'])
        self.assertNotEqual(authenticated['csrf'], anonymous['csrf'])
        self.assertNotEqual(authenticated['sid'], anonymous['sid'])

        with closing(connect(self.db_path)) as db:
            row = db.execute(
                'SELECT user_id, csrf FROM sessions WHERE nonce=?',
                (authenticated['sid'],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['user_id'], authenticated['uid'])
            self.assertEqual(row['csrf'], authenticated['csrf'])
            self.assertIsNone(db.execute(
                'SELECT 1 FROM sessions WHERE nonce=?', (anonymous['sid'],),
            ).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM oauth_states').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

        account = self.request('/account', authenticated_cookie)
        self.assertEqual(account['status'][:3], '200')
        replay = self.request('/oauth/callback', anonymous_cookie, query)
        self.assertEqual(replay['status'][:3], '403')
        rejected = self.request(
            '/logout', authenticated_cookie, method='POST',
            form={'csrf': anonymous['csrf']},
        )
        self.assertEqual(rejected['status'][:3], '403')
        logout = self.request(
            '/logout', authenticated_cookie, method='POST',
            form={'csrf': authenticated['csrf']},
        )
        self.assertEqual(logout['status'][:3], '303')
        with closing(connect(self.db_path)) as db:
            self.assertIsNone(db.execute(
                'SELECT 1 FROM sessions WHERE nonce=?', (authenticated['sid'],),
            ).fetchone())

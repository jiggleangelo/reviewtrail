"""Run with python -m unittest tests.test_e2e_demo -v; no sockets or HTTP."""
import html
import io
import json
import logging
import os
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from reviewtrail import auth
from reviewtrail.__main__ import main
from reviewtrail.config import Config
from reviewtrail.db import connect
from reviewtrail.reviews import proof_hash
from reviewtrail.timeseries import render_svg
from reviewtrail.vault import MemoryVault
from reviewtrail.web import App

ROOT = Path(__file__).resolve().parent.parent


class Browser:
    def __init__(self, test):
        self.test = test
        self.cookie = ''

    def request(self, target, method='GET', form=None, remote='127.0.0.1', forwarded=None):
        parsed = urllib.parse.urlsplit(target)
        body = urllib.parse.urlencode(form or {}).encode()
        env = {'REQUEST_METHOD': method, 'PATH_INFO': parsed.path, 'QUERY_STRING': parsed.query,
               'HTTP_COOKIE': self.cookie, 'REMOTE_ADDR': remote,
               'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body)}
        if forwarded:
            env['HTTP_X_FORWARDED_FOR'] = forwarded
        response = {}
        data = b''.join(self.test.app(env, lambda status, headers: response.update(
            status=int(status[:3]), headers=dict(headers)))).decode()
        cookie = response['headers'].get('Set-Cookie')
        if cookie:
            self.cookie = cookie.split(';', 1)[0]
        return response['status'], response['headers'], data

    def session(self):
        return auth.unpack(self.test.app.session_key, self.cookie.split('=', 1)[1])

    def post(self, path, **form):
        form.setdefault('csrf', self.session()['csrf'])
        return self.request(path, 'POST', form)

    def login(self, code='alice-old'):
        status, headers, _ = self.request('/login')
        self.test.assertEqual(status, 303)
        anonymous = self.session()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
        target = '/oauth/callback?' + urllib.parse.urlencode({'state': query['state'][0], 'code': code})
        status, _, _ = self.request(target)
        self.test.assertEqual(status, 303)
        self.test.assertNotEqual(anonymous['csrf'], self.session()['csrf'])
        self.test.assertNotEqual(anonymous['sid'], self.session()['sid'])
        self.test.assertNotIn('login', self.session())
        self.test.assertEqual(self.request(target)[0], 403)


class EndToEndDemoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = Config('e' * 48, str(self.directory / 'db'), str(self.directory / 'vault'),
                             'fake', '', '', 'dpo@example.test', 'test', True, False, 365)
        for target in ('socket.socket', 'socket.create_connection', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('Network access is forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.oauth = auth.FakeOAuth()
        self.oauth.users = dict(self.oauth.users)
        for i in range(5):
            self.oauth.users['reviewer-' + str(i)] = auth.Identity(str(2000 + i), 'developer-' + str(i), date(2020, 1, 1))
        self.vault = MemoryVault('demo-only-vault-key')
        self.logs = io.StringIO()
        self.logger = logging.Logger('demo')
        self.logger.addHandler(logging.StreamHandler(self.logs))
        self.cli('init-db')
        self.cli('seed')
        self.restart()

    def cli(self, *args):
        output = io.StringIO()
        with patch('reviewtrail.__main__.Config.from_env', return_value=self.config), redirect_stdout(output):
            main(list(args))
        return output.getvalue()

    def restart(self):
        self.app = App(self.config, self.vault, self.oauth, self.logger)

    def form(self, i=0, used=None, version='release-1', outcome='5', **extra):
        result = {'model_slug': 'claude-code', 'model_version': version,
                  'task_category': 'coding', 'task_description':
                  f'Case {i}: create a Python parser for a small CSV fixture, run its unit tests, and compare the resulting rows with the supplied expected values. <script>alert(1)</script>',
                  'stack': 'python/sqlite', 'harness': 'cli-agent', 'outcome': outcome,
                  'retries': '1', 'cost_usd': '1.25',
                  'used_on': (used or datetime.now(timezone.utc).date()).isoformat(),
                  'proof_line': 'local demo run completed', 'consent': 'on'}
        result.update(extra)
        return result

    def test_full_core_flow_with_fakes(self):
        visitor = Browser(self)
        status, _, directory = visitor.request('/models')
        self.assertEqual(status, 200)
        self.assertIn('Claude Code', directory)
        with closing(connect(self.config.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM models').fetchone()[0], 10)
        self.assertEqual(visitor.request('/review/new')[0], 303)
        for name in ('methodology', 'independence', 'privacy'):
            status, _, body = visitor.request('/' + name)
            self.assertEqual(status, 200)
            self.assertIn(html.escape((ROOT / 'content' / (name + '.md')).read_text(), quote=True), body)
            for link in ('/methodology', '/independence', '/privacy'):
                self.assertIn("href='" + link + "'", body)
        self.assertEqual(visitor.post('/signup', email='reader@example.test', consent='on', csrf='wrong')[0], 403)
        for _ in range(2):
            self.assertEqual(visitor.post('/signup', email='reader@example.test', consent='on')[0], 303)
        status, _, vendor = visitor.request('/waitlist/vendor')
        self.assertEqual(status, 200)
        for label in ('No payment is taken. We are measuring interest.', '$500 per report', '$1,500 per report', '$500/month subscription'):
            self.assertIn(label, vendor)
        self.assertNotIn('$3,000', vendor)
        for price in ('report_500', 'report_1500', 'sub_500_month'):
            self.assertEqual(visitor.post('/waitlist/vendor', email=price + '@example.test',
                                         company='Example', role='devrel', price_interest=price, consent='on')[0], 303)
        self.assertEqual(len(self.vault._dump()), 4)
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        browsers = []
        for i in range(5):
            browser = Browser(self)
            browser.login('reviewer-' + str(i))
            browsers.append(browser)
            self.assertEqual(browser.request('/review/new')[0], 200)
            if i == 0:
                self.assertEqual(browser.post('/review', **self.form(used=today + timedelta(days=1)))[0], 400)
            for offset, outcome in ((7, '5'), (0, '3')):
                form = self.form(i * 2 + offset, used=monday - timedelta(days=offset), outcome=outcome)
                self.assertEqual(browser.post('/review', **form)[0], 303)
                if i == 0 and offset == 7:
                    series = json.loads(visitor.request('/models/claude-code/series.json')[2])
                    self.assertEqual(series, [{'n': '<3', 'suppressed': True}])
            self.assertEqual(browser.post('/review', **form)[0], 409)
        status, _, model = visitor.request('/models/claude-code')
        self.assertEqual(status, 200)
        for text in ('Individual reviews', 'Community review', 'Pseudonymous reviewer',
                     'version-specific', 'proof attached (self-attested)', 'Possible shift', 'n=5',
                     'Self-reported, not a benchmark', '&lt;script&gt;'):
            self.assertIn(text, model)
        self.assertNotIn('<script>', model)
        self.assertNotIn('developer-0', model)
        self.assertNotIn('local demo run completed', model)
        series = json.loads(visitor.request('/models/claude-code/series.json')[2])
        self.assertEqual([r['n'] for r in series], [5, 5])
        self.assertEqual([r['mean_outcome'] for r in series], [5.0, 3.0])
        ET.fromstring(render_svg(series))
        with closing(connect(self.config.db_path)) as db:
            row = db.execute('SELECT proof_sha256 FROM reviews LIMIT 1').fetchone()
            self.assertEqual(row[0], proof_hash('local demo run completed', self.config.key('review-proof')))
            self.assertNotEqual(row[0], proof_hash('local demo run completed', 'reviewtrail-test-proof-key'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users WHERE display_name IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            dump = '\n'.join(db.iterdump())
            self.assertNotIn('reader@example.test', dump)
            self.assertNotIn('developer-0', dump)
            self.assertNotIn('local demo run completed', dump)
        self.restart()
        browser = browsers[0]
        self.assertEqual(browser.post('/review', **self.form(99, version='release-2', show_handle='on'))[0], 303)
        self.assertIn('developer-0', visitor.request('/models/claude-code')[2])
        exported = json.loads(browser.request('/account/export')[2])
        self.assertEqual(exported['account']['display_name'], 'developer-0')
        self.assertEqual(len(exported['reviews']), 3)
        report = self.cli('report', 'claude-code', '--weeks', '8')
        self.assertIn('Possible shift', report)
        self.assertNotIn('create a Python parser', report)
        self.assertNotIn('developer-0', report)
        self.assertIn('stranger_reviews', self.cli('metrics'))
        with closing(connect(self.config.db_path)) as db:
            hidden = db.execute("SELECT id FROM reviews WHERE model_version='release-2'").fetchone()[0]
        self.cli('hide', str(hidden))
        self.assertNotIn('<h3>release-2</h3>', visitor.request('/models/claude-code')[2])
        old_cookie = browser.cookie
        self.assertEqual(browser.post('/account/delete')[0], 303)
        browser.cookie = old_cookie
        self.assertEqual(browser.request('/account')[0], 303)
        with closing(connect(self.config.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 8)
        logs = self.logs.getvalue()
        self.assertIn('GET /models/{slug}', logs)
        for private in ('reader@example.test', 'developer-0', '127.0.0.1', 'local demo run completed', '?code=', 'rt_session='):
            self.assertNotIn(private, logs)

    def test_request_gate_restart_opt_out_and_source_badges(self):
        visitor = Browser(self)
        self.assertEqual(visitor.request('/', remote='203.0.113.10')[0], 403)
        self.config.trusted_proxy_header = 'X-Forwarded-For'
        self.assertEqual(visitor.request('/', forwarded='203.0.113.10')[0], 403)
        self.config.trusted_proxy_header = ''
        visitor.login()
        self.restart()
        self.assertFalse(hasattr(self.app, '_login_candidates'))
        self.assertEqual(visitor.post('/review', **self.form(show_handle='on'))[0], 303)
        self.assertIn('alice', visitor.request('/models/claude-code')[2])
        self.assertEqual(visitor.post('/review', **self.form(1, version='latest', proof_line=''))[0], 303)
        page = visitor.request('/models/claude-code')[2]
        article = page.split('<h3>latest</h3>', 1)[1].split('</article>', 1)[0]
        self.assertNotIn('version-specific', article)
        self.assertNotIn('proof attached', article)
        self.assertNotIn('alice', page)
        for source, option, label in (('founder', '--founder', 'Founder-written seed review'),
                                      ('import', '--import', 'Consented import')):
            path = self.directory / (source + '.json')
            item = self.form(version=source + '-1')
            item['outcome'], item['retries'] = 4, 0
            path.write_text(json.dumps([item]), encoding='utf-8')
            self.cli('seed', option, str(path))
            self.assertIn(label, visitor.request('/models/claude-code')[2])
        ET.fromstring(render_svg([{'week': '2026-W43', 'version': 'v1', 'n': 1, 'suppressed': True}]))
        old_cookie = visitor.cookie
        self.assertEqual(visitor.post('/logout')[0], 303)
        visitor.cookie = old_cookie
        self.assertEqual(visitor.request('/account')[0], 303)

    def test_serve_initializes_empty_database_without_network(self):
        self.config.db_path = str(self.directory / 'fresh-db')
        server = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=server)
        manager.__exit__ = Mock(return_value=False)
        with patch('reviewtrail.__main__.make_server', return_value=manager) as make, \
             patch('reviewtrail.__main__.FileVault', return_value=self.vault):
            self.cli('serve')
        make.assert_called_once()
        host, port, application = make.call_args.args
        self.assertEqual((host, port), ('127.0.0.1', 8000))
        self.assertIsInstance(application.oauth, auth.FakeOAuth)
        server.serve_forever.assert_called_once_with()
        self.app = application
        self.assertIn('Claude Code', Browser(self).request('/models')[2])


if __name__ == '__main__':
    unittest.main()

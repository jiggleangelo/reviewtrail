import http.client
import threading
import unittest
import urllib.parse
from datetime import date
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from demo import (
    BANNER,
    MAX_BODY,
    MAX_CONCURRENCY,
    MAX_REVIEWS,
    MAX_WAITLIST,
    POST_RATE_LIMIT,
    REVIEW_PAGE_SIZE,
    DemoHandler,
    create_server,
)


class Client:
    def __init__(self, test):
        self.test = test
        self.cookie = ''

    @property
    def visitor(self):
        return self.cookie.split('=', 1)[1] if self.cookie else ''

    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        if self.cookie:
            request_headers['Cookie'] = self.cookie
        connection = http.client.HTTPConnection('127.0.0.1', self.test.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read().decode('utf-8')
            response_headers = dict(response.getheaders())
            if 'Set-Cookie' in response_headers:
                self.cookie = response_headers['Set-Cookie'].split(';', 1)[0]
            return response.status, response_headers, data
        finally:
            connection.close()

    def form_request(self, path, values):
        body = urllib.parse.urlencode(values).encode('utf-8')
        return self.request(
            'POST',
            path,
            body,
            {'Content-Type': 'application/x-www-form-urlencoded'},
        )


class HostedDemoTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server('127.0.0.1', 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def review_form(self, marker='ordinary marker', version='user-test-2026.11'):
        return {
            'model_slug': 'claude-code',
            'model_version': version,
            'task_category': 'coding',
            'task_description': 'Made-up repeatable task: update a parser, run its tests, and inspect this harmless marker: ' + marker,
            'stack': 'python/sqlite <demo>',
            'harness': 'cli-agent',
            'outcome': '5',
            'retries': '0',
            'cost_usd': '2.50',
            'used_on': date.today().isoformat(),
            'proof_line': 'Made-up local run',
        }

    def stored_review(self, number, version='page-2026.11'):
        used = date.today()
        return SimpleNamespace(
            model_slug='claude-code',
            model_version=version,
            task_category='coding',
            task_description=f'Made-up pagination review number {number}, with enough repeatable detail for display testing.',
            stack='python/sqlite',
            harness='cli-agent',
            outcome=4,
            retries=1,
            cost_usd_cents=100,
            used_on=used.isoformat(),
            iso_week=f'{used.isocalendar().year}-W{used.isocalendar().week:02d}',
            proof_sha256=None,
            show_handle=False,
            content_hash=f'hash-{number}',
        )

    def test_healthz_and_banner(self):
        status, _, body = self.client.request('GET', '/healthz')
        self.assertEqual((status, body), (200, 'ok'))
        for path in ('/', '/review/new', '/models/claude-code', '/waitlist/vendor', '/missing'):
            status, _, body = self.client.request('GET', path)
            self.assertIn(status, (200, 404))
            self.assertIn(BANNER, body)

    def test_core_flow_over_http_escapes_input(self):
        status, _, form = self.client.request('GET', '/review/new?model=claude-code')
        self.assertEqual(status, 200)
        self.assertIn('Made-up task', form)
        self.assertIn('demo-release-2026.10', form)
        marker = "<script>alert('demo')</script>"
        status, headers, _ = self.client.form_request('/review', self.review_form(marker))
        self.assertEqual(status, 303)
        self.assertTrue(headers['Location'].startswith('/models/claude-code'))
        status, _, page = self.client.request('GET', headers['Location'])
        self.assertEqual(status, 200)
        self.assertIn('user-test-2026.11', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('python/sqlite &lt;demo&gt;', page)
        self.assertNotIn(marker, page)
        self.assertIn('proof attached (self-attested)', page)
        self.assertIn(BANNER, page)

    def test_stranger_text_is_visible_only_to_submitting_browser(self):
        marker = 'private-to-this-demo-browser-marker'
        self.client.request('GET', '/')
        status, headers, _ = self.client.form_request('/review', self.review_form(marker, 'private-2026.11'))
        self.assertEqual(status, 303)
        self.assertIn(marker, self.client.request('GET', headers['Location'])[2])
        stranger = Client(self)
        page = stranger.request('GET', '/models/claude-code')[2]
        self.assertNotIn(marker, page)
        self.assertNotIn('private-2026.11', page)
        self.assertIn('Sample reviewer A', page)

    def test_reviews_are_capped_and_oldest_visitor_entry_is_evicted(self):
        self.client.request('GET', '/')
        for number in range(MAX_REVIEWS + 10):
            self.server.state.add_review(self.stored_review(number), self.client.visitor)
        self.assertEqual(len(self.server.state.reviews), MAX_REVIEWS)
        descriptions = [item['task_description'] for item in self.server.state.reviews]
        self.assertNotIn('Made-up pagination review number 0, with enough repeatable detail for display testing.', descriptions)
        self.assertIn(f'Made-up pagination review number {MAX_REVIEWS + 9}, with enough repeatable detail for display testing.', descriptions)

    def test_waitlist_is_deduplicated_and_capped(self):
        self.server.state.add_waitlist('duplicate@example.test')
        self.server.state.add_waitlist('duplicate@example.test')
        self.assertEqual(len(self.server.state.vault._dump()), 1)
        for number in range(MAX_WAITLIST + 5):
            self.server.state.add_waitlist(f'vendor{number}@example.test')
        self.assertEqual(len(self.server.state.waitlist), MAX_WAITLIST)
        self.assertEqual(len(self.server.state.vault._dump()), MAX_WAITLIST)

    def test_post_routes_have_per_ip_rate_limits(self):
        values = {
            'email': 'demo.vendor@example.test',
            'company': 'Example Robots Ltd',
            'role': 'devrel',
            'price_interest': 'report_500',
        }
        for _ in range(POST_RATE_LIMIT):
            self.assertEqual(self.client.form_request('/waitlist/vendor', values)[0], 303)
        status, headers, page = self.client.form_request('/waitlist/vendor', values)
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '120')
        self.assertIn(BANNER, page)
        self.assertEqual(len(self.server.state.vault._dump()), 1)

    def test_proxy_clients_have_independent_buckets_on_both_routes(self):
        self.server.trusted_proxy_header = 'X-Forwarded-For'
        self.server.trusted_proxy_hops = 2
        forms = {
            '/review': self.review_form(),
            '/waitlist/vendor': {
                'email': 'demo.vendor@example.test',
                'role': 'devrel',
                'price_interest': 'report_500',
            },
        }
        for route, form in forms.items():
            with self.subTest(route=route):
                body = urllib.parse.urlencode(form).encode()

                def submit(chain):
                    return self.client.request('POST', route, body, {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-Forwarded-For': chain,
                    })

                for _ in range(POST_RATE_LIMIT):
                    self.assertEqual(submit('forged, 203.0.113.10, 10.0.0.1')[0], 303)
                self.assertEqual(submit('different-forgery, 203.0.113.10, 10.0.0.1')[0], 429)
                self.assertEqual(submit('forged, 203.0.113.11, 10.0.0.1')[0], 303)

    def test_unconfigured_proxy_header_cannot_bypass_rate_limit(self):
        body = urllib.parse.urlencode(self.review_form()).encode()
        for number in range(POST_RATE_LIMIT):
            status, _, _ = self.client.request('POST', '/review', body, {
                'X-Forwarded-For': f'203.0.113.{number + 1}',
            })
            self.assertEqual(status, 303)
        self.assertEqual(self.client.request('POST', '/review', body, {
            'X-Forwarded-For': '203.0.113.100',
        })[0], 429)

    def test_configured_proxy_rejects_missing_or_invalid_chain_before_submission(self):
        self.server.trusted_proxy_header = 'X-Forwarded-For'
        self.server.trusted_proxy_hops = 2
        body = urllib.parse.urlencode(self.review_form()).encode()
        for chain in (None, '203.0.113.10', ', 10.0.0.1', 'invalid, 10.0.0.1'):
            with self.subTest(chain=chain):
                headers = {} if chain is None else {'X-Forwarded-For': chain}
                status, _, page = self.client.request('POST', '/review', body, headers)
                self.assertEqual(status, 400)
                self.assertIn(BANNER, page)
        self.assertEqual(len(self.server.state.reviews), 3)
        self.assertEqual(len(self.server.state.post_buckets['/review'].b), 0)

    def test_model_reviews_are_paginated(self):
        self.client.request('GET', '/')
        for number in range(REVIEW_PAGE_SIZE + 2):
            self.server.state.add_review(self.stored_review(number), self.client.visitor)
        first = self.client.request('GET', '/models/claude-code?page=1')[2]
        second = self.client.request('GET', '/models/claude-code?page=2')[2]
        self.assertIn('Page 1 of 2', first)
        self.assertIn(f'Made-up pagination review number {REVIEW_PAGE_SIZE + 1}', first)
        self.assertNotIn('Made-up pagination review number 0,', first)
        self.assertIn('Made-up pagination review number 0,', second)
        self.assertLessEqual(first.count('<article>'), REVIEW_PAGE_SIZE)

    def test_home_counts_without_copying_model_review_lists(self):
        self.client.request('GET', '/')
        with patch.object(self.server.state, 'review_page', side_effect=AssertionError('home must not page or copy review lists')):
            status, _, page = self.client.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Reviews visible to you', page)

    def test_version_week_series_and_per_cell_suppression_are_rendered(self):
        self.client.request('GET', '/')
        self.server.state.add_review(self.stored_review(1, version='single-2026.12'), self.client.visitor)
        page = self.client.request('GET', '/models/claude-code')[2]
        self.assertIn('Weekly review volume and mean outcome by version', page)
        self.assertIn('demo-2026.10', page)
        self.assertIn('n=3', page)
        self.assertIn('single-2026.12', page)
        self.assertIn('&lt;3; suppressed', page)
        self.assertIn('&lt;3: suppressed', page)

    def test_handler_timeout_and_server_concurrency_cap(self):
        self.assertEqual(DemoHandler.timeout, 10)
        self.assertEqual(self.server.max_concurrency, MAX_CONCURRENCY)
        for _ in range(MAX_CONCURRENCY):
            self.assertTrue(self.server._request_slots.acquire(blocking=False))
        request = Mock()
        with patch.object(ThreadingHTTPServer, 'process_request') as parent, \
             patch.object(self.server, 'shutdown_request') as rejected:
            self.server.process_request(request, ('127.0.0.1', 1))
        parent.assert_not_called()
        rejected.assert_called_once_with(request)
        for _ in range(MAX_CONCURRENCY):
            self.server._request_slots.release()

    def test_default_method_error_uses_branded_page(self):
        status, _, page = self.client.request('PUT', '/review', b'')
        self.assertEqual(status, 501)
        self.assertIn(BANNER, page)
        self.assertIn('Request problem', page)

    def test_head_matches_get_headers_and_has_no_body(self):
        for path in ('/healthz', '/', '/review/new', '/models/claude-code', '/waitlist/vendor', '/missing', '/models/missing'):
            with self.subTest(path=path):
                get_status, get_headers, get_body = self.client.request('GET', path)
                head_status, head_headers, head_body = self.client.request('HEAD', path)
                self.assertEqual(head_status, get_status)
                self.assertEqual(head_body, '')
                self.assertEqual(int(head_headers['Content-Length']), len(get_body.encode('utf-8')))
                for name in ('Content-Type', 'Content-Length', 'Content-Security-Policy', 'X-Content-Type-Options', 'Referrer-Policy'):
                    self.assertEqual(head_headers[name], get_headers[name])
        self.assertEqual(len(self.server.state.reviews), 3)
        self.assertEqual(len(self.server.state.waitlist), 0)

    def test_vendor_fake_door_uses_memory_vault(self):
        before = len(self.server.state.vault._dump())
        status, headers, _ = self.client.form_request('/waitlist/vendor', {
            'email': 'demo.vendor@example.test',
            'company': 'Example Robots Ltd',
            'role': 'devrel',
            'price_interest': 'report_500',
        })
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/waitlist/vendor?joined=1')
        self.assertEqual(len(self.server.state.vault._dump()), before + 1)
        status, _, page = self.client.request('GET', headers['Location'])
        self.assertEqual(status, 200)
        self.assertIn('No payment was taken', page)
        self.assertIn('$500 per report', page)
        self.assertIn('$1,500 per report', page)
        self.assertIn('$500/month subscription', page)

    def test_rejects_bodies_over_8_kb(self):
        self.assertEqual(MAX_BODY, 8 * 1024)
        body = b'x' * (MAX_BODY + 1)
        status, _, page = self.client.request(
            'POST', '/review', body,
            {'Content-Type': 'application/x-www-form-urlencoded'},
        )
        self.assertEqual(status, 413)
        self.assertIn('8 KB', page)
        self.assertIn(BANNER, page)


if __name__ == '__main__':
    unittest.main()

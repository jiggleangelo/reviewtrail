#!/usr/bin/env python3
'''Hosted, disposable ReviewTrail user-test demo.

Run with ``python3 demo.py``. All state is bounded, held in memory and disappears
when the process exits. The demo performs no outbound network requests.

Behind an ingress, explicitly set TRUSTED_PROXY_HEADER and TRUSTED_PROXY_HOPS.
The ingress must sanitize that header and be the only path to this server.
See DEMO_DEPLOYMENT.md for the trust boundary and deployment checks.
'''

import ipaddress
import os
import re
import secrets
import socket
import threading
import urllib.parse
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reviewtrail.antifraud import IPBucket
from reviewtrail.auth import FakeOAuth
from reviewtrail.directory import load_models
from reviewtrail.reviews import specific, validate
from reviewtrail.templates import e
from reviewtrail.timeseries import render_svg
from reviewtrail.vault import MemoryVault

ROOT = Path(__file__).resolve().parent
BANNER = 'Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged.'
MAX_BODY = 8 * 1024
MAX_REVIEWS = 50
MAX_WAITLIST = 50
REVIEW_PAGE_SIZE = 10
POST_RATE_LIMIT = 10
MAX_CONCURRENCY = 20
REQUEST_DEADLINE = 10
PRICES = {
    'report_500': '$500 per report',
    'report_1500': '$1,500 per report',
    'sub_500_month': '$500/month subscription',
    'none': 'Just exploring',
}


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def proxy_settings(header, hops):
    '''Validate explicit deployment configuration before opening a socket.

    Header values are comma-separated IP literals, as in X-Forwarded-For,
    not the RFC Forwarded header syntax. HTTP_ names are accepted for parity
    with the WSGI application's configuration.
    '''
    if not isinstance(header, str):
        raise ValueError('TRUSTED_PROXY_HEADER must be a header name')
    if header.startswith('HTTP_'):
        header = header[5:].replace('_', '-')
    if header and not re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*', header):
        raise ValueError('TRUSTED_PROXY_HEADER must be a header name')
    if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
        raise ValueError('TRUSTED_PROXY_HOPS must be a positive integer')
    return header, hops


class DemoState:
    def __init__(self):
        self.models = load_models(ROOT / 'seed' / 'models.json')
        self.model_by_slug = {model['slug']: model for model in self.models}
        self.vault = MemoryVault('reviewtrail-disposable-demo')
        self.identity = FakeOAuth().exchange('alice-old', 'demo-state')
        self.lock = threading.Lock()
        self.waitlist = OrderedDict()
        self.post_buckets = {
            '/review': IPBucket('reviewtrail-demo-review-rate', capacity=POST_RATE_LIMIT),
            '/waitlist/vendor': IPBucket('reviewtrail-demo-waitlist-rate', capacity=POST_RATE_LIMIT),
        }
        today = datetime.now(timezone.utc).date()
        week = f'{today.isocalendar().year}-W{today.isocalendar().week:02d}'
        self.reviews = [
            {
                'model_slug': 'claude-code',
                'model_version': 'demo-2026.10',
                'task_category': 'coding',
                'task_description': 'Made-up task: add CSV validation to a small Python command-line tool and update its unit tests.',
                'stack': 'python/sqlite',
                'harness': 'cli-agent',
                'outcome': 5,
                'retries': 1,
                'cost_usd_cents': 125,
                'used_on': today.isoformat(),
                'iso_week': week,
                'proof_sha256': 'made-up-proof',
                'author': 'Sample reviewer A',
                'owner': None,
            },
            {
                'model_slug': 'claude-code',
                'model_version': 'demo-2026.10',
                'task_category': 'coding',
                'task_description': 'Made-up task: diagnose a failing pagination test and prepare a minimal patch with a regression test.',
                'stack': 'python/postgres',
                'harness': 'ide-plugin',
                'outcome': 4,
                'retries': 2,
                'cost_usd_cents': 210,
                'used_on': today.isoformat(),
                'iso_week': week,
                'proof_sha256': None,
                'author': 'Sample reviewer B',
                'owner': None,
            },
            {
                'model_slug': 'claude-code',
                'model_version': 'demo-2026.10',
                'task_category': 'agents',
                'task_description': 'Made-up task: trace a flaky worker shutdown and explain the smallest safe code change.',
                'stack': 'python/redis',
                'harness': 'cli-agent',
                'outcome': 3,
                'retries': 2,
                'cost_usd_cents': 175,
                'used_on': today.isoformat(),
                'iso_week': week,
                'proof_sha256': None,
                'author': 'Sample reviewer C',
                'owner': None,
            },
        ]

    @staticmethod
    def _visible(review, owner):
        return review.get('owner') is None or review.get('owner') == owner

    def allow_post(self, route, address):
        with self.lock:
            return self.post_buckets[route].allow(address)

    def counts_for(self, owner):
        with self.lock:
            counts = {slug: 0 for slug in self.model_by_slug}
            for review in self.reviews:
                if self._visible(review, owner):
                    counts[review['model_slug']] += 1
            return counts

    def review_page(self, slug, owner, page):
        with self.lock:
            matching = [
                dict(review) for review in reversed(self.reviews)
                if review['model_slug'] == slug and self._visible(review, owner)
            ]
        total = len(matching)
        pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * REVIEW_PAGE_SIZE
        return matching[start:start + REVIEW_PAGE_SIZE], total, page, pages

    def aggregate_for(self, slug, owner):
        with self.lock:
            groups = defaultdict(list)
            for review in self.reviews:
                if review['model_slug'] == slug and self._visible(review, owner):
                    groups[(review['model_version'], review['iso_week'])].append(review)
        rows = []
        for (version, week), reviews in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
            count = len(reviews)
            rows.append({
                'version': version,
                'week': week,
                'n': count,
                'suppressed': count < 3,
                'mean_outcome': None if count < 3 else round(sum(item['outcome'] for item in reviews) / count, 1),
                'mean_retries': None if count < 3 else round(sum(item['retries'] for item in reviews) / count, 1),
                'median_cost': None,
                'pct_version_specific': None if count < 3 else (100.0 if specific(version) else 0.0),
            })
        return rows

    def add_review(self, review, owner):
        item = review.__dict__.copy()
        item['author'] = 'You (fake account)'
        item['owner'] = owner
        with self.lock:
            self.reviews.append(item)
            while len(self.reviews) > MAX_REVIEWS:
                removable = next(
                    (index for index, existing in enumerate(self.reviews) if existing.get('owner') is not None),
                    0,
                )
                self.reviews.pop(removable)

    def add_waitlist(self, email):
        digest = self.vault.digest(email)
        with self.lock:
            existing = self.waitlist.get(digest)
            if existing:
                return existing
            token = self.vault.put('vendor', email)
            self.waitlist[digest] = token
            while len(self.waitlist) > MAX_WAITLIST:
                _, expired = self.waitlist.popitem(last=False)
                self.vault.forget(expired)
            return token


class CappedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address, handler_class, max_concurrency=MAX_CONCURRENCY):
        self.max_concurrency = max_concurrency
        self._request_slots = threading.BoundedSemaphore(max_concurrency)
        super().__init__(server_address, handler_class)

    def handle_error(self, request, client_address):
        # socketserver's default traceback includes the visitor's IP address.
        pass

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class DemoHandler(BaseHTTPRequestHandler):
    server_version = 'ReviewTrailDemo/1.1'
    protocol_version = 'HTTP/1.0'
    timeout = 10
    request_deadline = REQUEST_DEADLINE

    def log_message(self, format, *args):
        '''Disable BaseHTTPRequestHandler request logging.'''

    def handle(self):
        # HTTP/1.0 closes each connection after its request. This timer starts
        # before reading the request line and never resets when bytes arrive.
        # Shutdown interrupts buffered readline/read and blocked response writes;
        # a per-recv timeout alone cannot stop a continuously trickling client.
        deadline = threading.Timer(self.request_deadline, self._expire_request)
        deadline.daemon = True
        deadline.start()
        try:
            super().handle()
        except OSError:
            self.close_connection = True
        finally:
            deadline.cancel()
            # Finish the callback before BaseRequestHandler closes the socket.
            deadline.join()

    def _expire_request(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def state(self):
        return self.server.state

    def client_ip(self):
        header = getattr(self.server, 'trusted_proxy_header', '')
        if not header:
            return self.client_address[0]
        # Configuration is an operator trust assertion: the sanitized ingress
        # must be the only route to this listener. Never trust headers by default.
        headers = self.headers.get_all(header, [])
        hops = self.server.trusted_proxy_hops
        if len(headers) != 1:
            raise RequestError(400, 'Missing or ambiguous proxy client address.')
        values = [value.strip() for value in headers[0].split(',')]
        if len(values) < hops or not values[-hops]:
            raise RequestError(400, 'Incomplete proxy client address chain.')
        try:
            # Only the fixed trusted suffix matters; a forged prefix cannot
            # change the selected address. Canonicalize IPv6 spellings as well.
            address = values[-hops]
            if '%' in address:
                raise ValueError('Scoped addresses are not client identities')
            parsed = ipaddress.ip_address(address)
            if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
                return str(parsed.ipv4_mapped)
            return str(parsed)
        except ValueError as error:
            raise RequestError(400, 'Invalid proxy client address.') from error

    def _visitor_id(self):
        existing = getattr(self, '_demo_visitor', None)
        if existing:
            return existing
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie['rt_demo_visitor'].value if 'rt_demo_visitor' in cookie else ''
        except Exception:
            value = ''
        if not 16 <= len(value) <= 64 or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for character in value):
            value = secrets.token_urlsafe(24)
            self._new_demo_visitor = value
        self._demo_visitor = value
        return value

    def _send(self, status, body, content_type='text/html; charset=utf-8', headers=None):
        payload = body if isinstance(body, bytes) else body.encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if getattr(self, '_new_demo_visitor', None):
                self.send_header('Set-Cookie', 'rt_demo_visitor=' + self._new_demo_visitor + '; Path=/; HttpOnly; SameSite=Lax')
            for name, value in headers or []:
                self.send_header(name, value)
            self.end_headers()
            if getattr(self, 'command', '') != 'HEAD':
                self.wfile.write(payload)
        except OSError:
            self.close_connection = True

    def _page(self, title, body):
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            '<title>' + e(title) + ' · ReviewTrail demo</title>'
            "<style>body{font:16px system-ui;max-width:900px;margin:auto;padding:1rem;color:#182235}"
            ".banner{background:#fff1c7;border:2px solid #9a6b00;padding:.8rem;font-weight:700}"
            "nav{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}label{display:block;margin:.8rem 0}"
            "input,select,textarea,button{font:inherit;padding:.5rem;max-width:100%}textarea{width:100%;min-height:7rem}"
            "article{border-top:1px solid #ccd3df;padding:1rem 0}.badge{background:#e8eef9;padding:.2rem .4rem;margin-right:.3rem}"
            ".error{color:#9b1c1c}.success{background:#e4f7e8;padding:.7rem}.series{overflow:auto}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #ccd3df;padding:.5rem;text-align:left}</style></head><body>"
            "<div class='banner'>" + e(BANNER) + '</div>'
            "<nav><a href='/'>Models</a><a href='/review/new'>Try the review flow</a>"
            "<a href='/waitlist/vendor'>Vendor report fake-door</a></nav>"
            '<main><h1>' + e(title) + '</h1>' + body + '</main></body></html>'
        )

    def _error(self, status, message):
        self._send(status, self._page('Request problem', "<p class='error'>" + e(message) + '</p>'))

    def send_error(self, code, message=None, explain=None):
        short = self.responses.get(code, ('Request error', ''))[0]
        self._error(code, message or short)

    def _redirect(self, location):
        self._send(303, b'', headers=[('Location', location)])

    def _read_form(self):
        if self.headers.get('Transfer-Encoding'):
            raise RequestError(400, 'Transfer-encoded request bodies are not supported.')
        raw_length = self.headers.get('Content-Length')
        if raw_length is None:
            raise RequestError(400, 'Content-Length is required.')
        try:
            length = int(raw_length)
        except ValueError as error:
            raise RequestError(400, 'Invalid Content-Length.') from error
        if length < 0:
            raise RequestError(400, 'Invalid Content-Length.')
        if length > MAX_BODY:
            raise RequestError(413, 'Request body exceeds the 8 KB demo limit.')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, 'Incomplete request body.')
        try:
            parsed = urllib.parse.parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=50)
        except (UnicodeDecodeError, ValueError) as error:
            raise RequestError(400, 'Invalid form body.') from error
        return {key: values[-1] for key, values in parsed.items()}

    @staticmethod
    def _page_number(query):
        try:
            return max(1, int(urllib.parse.parse_qs(query).get('page', ['1'])[-1]))
        except ValueError:
            return 1

    def do_HEAD(self):
        # _send computes the GET representation's headers and omits its body.
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == '/healthz':
            self._send(200, 'ok', 'text/plain; charset=utf-8')
        elif path == '/':
            self._home(parsed.query)
        elif path == '/review/new':
            query = urllib.parse.parse_qs(parsed.query)
            self._review_form({'model_slug': query.get('model', ['claude-code'])[-1]})
        elif path == '/waitlist/vendor':
            self._waitlist(parsed.query)
        elif path.startswith('/models/'):
            self._model(urllib.parse.unquote(path[len('/models/'):]), parsed.query)
        else:
            self._error(404, 'Page not found.')

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path not in {'/review', '/waitlist/vendor'}:
                self._error(404, 'Page not found.')
                return
            if not self.state.allow_post(path, self.client_ip()):
                self._send(429, self._page('Too many requests', "<p class='error'>Please wait before submitting again.</p>"), headers=[('Retry-After', '120')])
                return
            form = self._read_form()
            if path == '/review':
                self._submit_review(form)
            else:
                self._submit_waitlist(form)
        except RequestError as error:
            self._error(error.status, error.message)

    def _home(self, query):
        counts = self.state.counts_for(self._visitor_id())
        rows = ''.join(
            "<tr><td><a href='/models/" + urllib.parse.quote(model['slug'], safe='') + "'>" + e(model['name']) + '</a></td>'
            '<td>' + e(model['vendor']) + '</td><td>' + e(counts[model['slug']]) + '</td></tr>'
            for model in self.state.models
        )
        published = urllib.parse.parse_qs(query).get('published') == ['1']
        notice = "<p class='success'>Your made-up review was published in this temporary demo.</p>" if published else ''
        body = (
            notice
            + '<p>Explore made-up sample reviews, then publish one as the pre-authenticated fake user '
            + e(self.state.identity.login)
            + '. No sign-up is needed and everything disappears on restart.</p>'
            + '<p>Visitor submissions are visible only in the browser that submitted them. The shared demo cannot be defaced for other visitors.</p>'
            + "<p><a href='/review/new'><strong>Start the two-minute review flow →</strong></a></p>"
            + '<table><tr><th>Model or agent</th><th>Vendor</th><th>Reviews visible to you</th></tr>'
            + rows + '</table>'
        )
        self._send(200, self._page('Developer-written model reviews', body))

    def _model(self, slug, query):
        model = self.state.model_by_slug.get(slug)
        if not model:
            self._error(404, 'Unknown model.')
            return
        owner = self._visitor_id()
        reviews, total, page_number, pages = self.state.review_page(slug, owner, self._page_number(query))
        series = self.state.aggregate_for(slug, owner)
        table_rows = ''.join(
            '<tr><td>' + e(row['week']) + '</td><td>' + e(row['version']) + '</td><td>'
            + (e(row['n']) if not row['suppressed'] else '&lt;3; suppressed') + '</td><td>'
            + (e(row['mean_outcome']) if not row['suppressed'] else 'suppressed') + '</td></tr>'
            for row in series
        )
        chart = render_svg(series) if series else '<p>No aggregate cells yet.</p>'
        articles = []
        for review in reviews:
            badges = []
            if specific(review['model_version']):
                badges.append('version-specific')
            if review.get('proof_sha256'):
                badges.append('proof attached (self-attested)')
            badge_html = ''.join("<span class='badge'>" + e(badge) + '</span>' for badge in badges)
            cost = 'Not supplied' if review.get('cost_usd_cents') is None else f"${review['cost_usd_cents'] / 100:.2f}"
            articles.append(
                '<article><h2>' + e(review['model_version']) + '</h2><p>' + badge_html + '</p>'
                '<p>' + e(review['author']) + ' · ' + e(review['used_on']) + '</p>'
                '<p><strong>' + e(review['task_category']) + '</strong> · ' + e(review['stack']) + ' · '
                + e(review['harness']) + ' · outcome ' + e(review['outcome']) + '/5 · retries '
                + e(review['retries']) + ' · ' + e(cost) + '</p><p>' + e(review['task_description']) + '</p></article>'
            )
        navigation = ''
        if pages > 1:
            links = []
            if page_number > 1:
                links.append("<a href='?page=" + e(page_number - 1) + "'>← Newer</a>")
            links.append('Page ' + e(page_number) + ' of ' + e(pages))
            if page_number < pages:
                links.append("<a href='?page=" + e(page_number + 1) + "'>Older →</a>")
            navigation = '<nav>' + ' · '.join(links) + '</nav>'
        published = urllib.parse.parse_qs(query).get('published') == ['1']
        success = "<p class='success'>Published for this browser only.</p>" if published else ''
        body = (
            success + '<p>' + e(model['vendor']) + '. Outcomes are made-up and self-reported, not a benchmark.</p>'
            + '<p>Each version/ISO-week cell with fewer than three visible reviews is suppressed.</p>'
            + "<div class='series'>" + chart + '</div>'
            + '<table><tr><th>ISO week</th><th>Version</th><th>n</th><th>Mean outcome</th></tr>' + table_rows + '</table>'
            + "<p><a href='/review/new?model=" + urllib.parse.quote(slug, safe='') + "'>Add a demo review</a></p>"
            + '<h2>Individual reviews</h2><p>Showing ' + e(len(reviews)) + ' of ' + e(total) + ' reviews visible to this browser.</p>'
            + ''.join(articles) + navigation
        )
        self._send(200, self._page(model['name'], body))

    def _review_form(self, values=None, errors=None):
        values = values or {}
        errors = errors or {}
        today = datetime.now(timezone.utc).date().isoformat()

        def value(name, default=''):
            return e(values.get(name, default))

        def error(name):
            return "<p class='error'>" + e(errors[name]) + '</p>' if name in errors else ''

        options = ''.join(
            "<option value='" + e(model['slug']) + "'" + (' selected' if values.get('model_slug', 'claude-code') == model['slug'] else '') + '>'
            + e(model['name']) + '</option>' for model in self.state.models
        )
        description = values.get('task_description', 'Made-up task: add validation to a small CSV import command and write a regression test for malformed rows.')
        body = (
            '<p>The form is pre-filled with made-up data. Edit anything, but do not enter real personal data or secrets.</p>'
            "<form method='post' action='/review'>"
            "<label>Model<select name='model_slug'>" + options + '</select></label>' + error('model_slug')
            + "<label>Version<input name='model_version' value='" + value('model_version', 'demo-release-2026.10') + "' required></label>" + error('model_version')
            + "<label>Task category<select name='task_category'>"
            + ''.join("<option value='" + category + "'" + (' selected' if values.get('task_category', 'coding') == category else '') + '>' + category + '</option>' for category in ('coding', 'extraction', 'rag', 'agents'))
            + '</select></label>' + error('task_category')
            + "<label>Repeatable task description<textarea name='task_description' minlength='40' maxlength='1000' required>" + e(description) + '</textarea></label>' + error('task_description')
            + "<label>Stack<input name='stack' value='" + value('stack', 'python/sqlite') + "' required></label>" + error('stack')
            + "<label>Harness<select name='harness'>"
            + ''.join("<option value='" + harness + "'" + (' selected' if values.get('harness', 'cli-agent') == harness else '') + '>' + harness + '</option>' for harness in ('api-direct', 'openrouter', 'ide-plugin', 'cli-agent'))
            + '</select></label>' + error('harness')
            + "<label>Outcome (1 failed, 5 solved first try)<input type='number' name='outcome' min='1' max='5' value='" + value('outcome', '4') + "' required></label>" + error('outcome')
            + "<label>Retries<input type='number' name='retries' min='0' max='50' value='" + value('retries', '1') + "' required></label>" + error('retries')
            + "<label>Cost USD (optional)<input type='number' name='cost_usd' min='0' max='10000' step='.01' value='" + value('cost_usd', '1.25') + "'></label>" + error('cost_usd')
            + "<label>Date used<input type='date' name='used_on' value='" + value('used_on', today) + "' required></label>" + error('used_on')
            + "<label>Self-attested proof line (optional)<input name='proof_line' maxlength='300' value='" + value('proof_line', 'Made-up local demo run completed') + "'></label>" + error('proof_line')
            + '<button>Publish made-up review</button></form>'
        )
        self._send(400 if errors else 200, self._page('Try the review flow', body))

    def _submit_review(self, form):
        review, errors = validate(
            form,
            datetime.now(timezone.utc).date(),
            set(self.state.model_by_slug),
            proof_key='reviewtrail-disposable-demo-proof',
        )
        if errors:
            self._review_form(form, errors)
            return
        self.state.add_review(review, self._visitor_id())
        location = '/models/' + urllib.parse.quote(review.model_slug, safe='') + '?published=1'
        self._redirect(location)

    def _waitlist(self, query):
        joined = urllib.parse.parse_qs(query).get('joined') == ['1']
        success = "<p class='success'>Interest recorded only in bounded memory. No payment was taken.</p>" if joined else ''
        options = ''.join("<option value='" + e(key) + "'>" + e(label) + '</option>' for key, label in PRICES.items())
        body = (
            success
            + '<p><strong>No payment is taken. We are measuring interest.</strong></p>'
            '<p>This is a fake-door for an aggregate-only vendor drift report. Use only the made-up address supplied below.</p>'
            "<form method='post' action='/waitlist/vendor'>"
            "<label>Made-up contact<input type='email' name='email' value='demo.vendor@example.test' required></label>"
            "<label>Company<input name='company' value='Example Robots Ltd' maxlength='100'></label>"
            "<label>Role<select name='role'><option value='devrel'>DevRel</option><option value='pmm'>PMM</option><option value='product'>Product</option><option value='eng'>Engineering</option><option value='other'>Other</option></select></label>"
            "<label>Price interest<select name='price_interest'>" + options + '</select></label>'
            '<button>Record demo interest</button></form>'
        )
        self._send(200, self._page('Vendor report fake-door', body))

    def _submit_waitlist(self, form):
        email = form.get('email', '').strip().lower()
        role = form.get('role', '')
        price = form.get('price_interest', '')
        if not email.endswith('@example.test') or len(email) > 254:
            raise RequestError(400, 'Use a made-up @example.test address in this demo.')
        if role not in {'devrel', 'pmm', 'product', 'eng', 'other'} or price not in PRICES:
            raise RequestError(400, 'Choose a listed role and price option.')
        self.state.add_waitlist(email)
        self._redirect('/waitlist/vendor?joined=1')


def create_server(host='0.0.0.0', port=8080, state=None, *, trusted_proxy_header='', trusted_proxy_hops=1):
    header, hops = proxy_settings(trusted_proxy_header, trusted_proxy_hops)
    server = CappedThreadingHTTPServer((host, port), DemoHandler)
    server.state = state or DemoState()
    server.trusted_proxy_header = header
    server.trusted_proxy_hops = hops
    return server


def main():
    try:
        port = int(os.environ.get('PORT', '8080'))
    except ValueError as error:
        raise SystemExit('PORT must be an integer') from error
    if not 1 <= port <= 65535:
        raise SystemExit('PORT must be between 1 and 65535')
    header = os.environ.get('TRUSTED_PROXY_HEADER', '')
    raw_hops = os.environ.get('TRUSTED_PROXY_HOPS', '')
    if bool(header) != bool(raw_hops):
        raise SystemExit('Set TRUSTED_PROXY_HEADER and TRUSTED_PROXY_HOPS together')
    try:
        hops = int(raw_hops) if raw_hops else 1
        header, hops = proxy_settings(header, hops)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    server = create_server('0.0.0.0', port, trusted_proxy_header=header, trusted_proxy_hops=hops)
    print(f'ReviewTrail demo: http://0.0.0.0:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

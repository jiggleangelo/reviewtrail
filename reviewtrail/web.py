import hmac
import json
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import auth
from .antifraud import IPBucket, account_old_enough, review_error
from .db import connect, now
from .directory import list_models
from .logsafe import SafeLogger
from .presentation import open_handle, policy, review_list, seal_handle
from .reviews import insert, validate
from .templates import e, page, review_form
from .timeseries import aggregate, json_series, render_svg, shifts

ROOT = Path(__file__).resolve().parent.parent
EMAIL = __import__('re').compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
ROLES = {'devrel', 'pmm', 'product', 'eng', 'other'}
PRICE_LABELS = {'report_500': '$500 per report', 'report_1500': '$1,500 per report',
                'sub_500_month': '$500/month subscription', 'none': 'No price selected'}
PRICES = set(PRICE_LABELS)


class BodyTooLarge(Exception):
    pass


class BadBody(Exception):
    pass


class App:
    def __init__(self, config, vault, oauth, logger=None):
        self.c, self.vault, self.oauth = config, vault, oauth
        self.log = SafeLogger(logger)
        self.session_key = config.key('session')
        self.subject_key = config.key('subject')
        self.state_key = config.key('oauth-state')
        self.proof_key = config.key('review-proof')
        self.handle_key = config.key('handle-claim')
        self.bucket = IPBucket(config.key('ip-throttle'))

    def __call__(self, environ, start_response):
        started = time.monotonic()
        method = environ.get('REQUEST_METHOD', 'GET')
        path = environ.get('PATH_INFO', '/')
        route = self.route_template(path)
        try:
            status, headers, body = self.dispatch(environ, method, path)
        except BodyTooLarge:
            status, headers, body = self.text('413 Payload Too Large', 'Request body too large')
        except BadBody:
            status, headers, body = self.text('400 Bad Request', 'Invalid request body')
        except Exception as error:
            self.log.exception(error)
            status, headers, body = self.text('500 Internal Server Error', 'Internal server error')
        if not any(name.lower() == 'content-type' for name, _ in headers):
            headers.insert(0, ('Content-Type', 'text/html; charset=utf-8'))
        headers.extend([
            ('Content-Security-Policy', "default-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
            ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
            ('Referrer-Policy', 'same-origin'),
        ])
        start_response(status, headers)
        self.log.request(method, route, int(status[:3]), started)
        return [body if isinstance(body, bytes) else body.encode()]

    def route_template(self, path):
        parts = path.strip('/').split('/')
        if len(parts) == 2 and parts[0] == 'models':
            return '/models/{slug}'
        if len(parts) == 3 and parts[0] == 'models' and parts[2] == 'series.json':
            return '/models/{slug}/series.json'
        if len(parts) == 2 and parts[0] == 'static':
            return '/static/{asset}'
        known = {'/', '/models', '/review/new', '/review', '/login', '/oauth/callback', '/logout',
                 '/signup', '/waitlist/vendor', '/account', '/account/export', '/account/delete',
                 '/methodology', '/independence', '/privacy'}
        return path if path in known else '/not-found'

    def form(self, environ):
        try:
            length = int(environ.get('CONTENT_LENGTH') or 0)
        except ValueError as error:
            raise BadBody() from error
        if length < 0:
            raise BadBody()
        if length > 65536:
            raise BodyTooLarge()
        raw = environ['wsgi.input'].read(length)
        if len(raw) != length:
            raise BadBody()
        return {key: values[-1] for key, values in urllib.parse.parse_qs(
            raw.decode('utf-8', 'replace'), keep_blank_values=True).items()}

    def cookies(self, environ):
        result = {}
        for item in environ.get('HTTP_COOKIE', '').split(';'):
            if '=' in item:
                key, value = item.strip().split('=', 1)
                result[key] = value
        return result

    def client_ip(self, environ):
        header = getattr(self.c, 'trusted_proxy_header', '')
        if header:
            key = header if header.startswith('HTTP_') else 'HTTP_' + header.upper().replace('-', '_')
            values = [value.strip() for value in environ.get(key, '').split(',') if value.strip()]
            hops = getattr(self.c, 'trusted_proxy_hops', 1)
            if len(values) >= hops:
                return values[-hops]
        return environ.get('REMOTE_ADDR', '')

    def load_session(self, db, environ):
        session = auth.unpack(self.session_key, self.cookies(environ).get('rt_session', ''))
        if not session:
            return None
        sid = session.get('sid', '')
        if not sid:
            return None if session.get('uid') else session
        live = db.execute(
            'SELECT user_id,csrf FROM sessions WHERE nonce=? AND expires_at>?', (sid, now()),
        ).fetchone()
        if not live or (live['user_id'] or 0) != session.get('uid', 0):
            return None
        return session if hmac.compare_digest(live['csrf'], session.get('csrf', '')) else None

    def html(self, title, body, session=None, headers=None, status='200 OK'):
        return status, headers or [], page(title, body, session['csrf'] if session else '',
                                           bool(session and session.get('uid')))

    def text(self, status, body, headers=None):
        return status, [('Content-Type', 'text/plain; charset=utf-8')] + (headers or []), body.encode()

    def redirect(self, location, headers=None):
        return '303 See Other', [('Location', location)] + (headers or []), b''

    def dispatch(self, environ, method, path):
        try:
            self.c.assert_request_allowed(environ.get('REMOTE_ADDR', ''))
            self.c.assert_request_allowed(self.client_ip(environ))
        except RuntimeError:
            return self.text('403 Forbidden', 'Fake OAuth accepts loopback requests only')
        db = connect(self.c.db_path)
        try:
            return self._dispatch(db, environ, method, path)
        finally:
            db.close()

    def _dispatch(self, db, environ, method, path):
        session = self.load_session(db, environ)
        session_headers = []
        if not session:
            token = auth.session(self.session_key)
            session = auth.unpack(self.session_key, token)
            session_headers = [('Set-Cookie', auth.cookie('rt_session', token, self.c.https, 86400))]
        if method == 'POST':
            if not self.bucket.allow(self.client_ip(environ)):
                return self.text('429 Too Many Requests', 'Too many requests')
            form = self.form(environ)
            if not hmac.compare_digest(form.get('csrf', ''), session.get('csrf', '')):
                return self.text('403 Forbidden', 'CSRF check failed')
        else:
            form = {}
        csrf_input = "<input type='hidden' name='csrf' value='" + e(session['csrf']) + "'>"

        if path == '/' and method == 'GET':
            cards = ''.join(f"<li><a href='/models/{e(m['slug'])}'>{e(m['name'])}</a> — {e(m['vendor'])}, n={m['reviews']}</li>" for m in list_models(db)[:10])
            body = "<p>Task-tagged, version-stamped reviews written by developers. Self-reported, not a benchmark.</p><ul>" + cards + "</ul><form method='post' action='/signup'>" + csrf_input + "<label>Email<input type='email' name='email' required></label><label><input type='checkbox' name='consent' value='on' required> I consent to updates.</label><button>Email me new data</button></form>"
            return self.html('Developer-written model reviews', body, session, session_headers)

        if path == '/models' and method == 'GET':
            rows = ''.join(f"<tr><td><a href='/models/{e(m['slug'])}'>{e(m['name'])}</a></td><td>{e(m['vendor'])}</td><td>{m['reviews']}</td><td>{e(m['last_week'] or '—')}</td></tr>" for m in list_models(db))
            return self.html('Model directory', '<p>Counts include published, self-reported reviews.</p><table><tr><th>Model</th><th>Vendor</th><th>Reviews</th><th>Latest week</th></tr>' + rows + '</table>', session, session_headers)

        parts = path.strip('/').split('/')
        if method == 'GET' and len(parts) in {2, 3} and parts[0] == 'models' and (len(parts) == 2 or parts[2] == 'series.json'):
            slug = parts[1]
            model = db.execute('SELECT * FROM models WHERE slug=?', (slug,)).fetchone()
            if not model:
                return self.text('404 Not Found', 'Not found')
            rows = aggregate(db, slug)
            if len(parts) == 3:
                return '200 OK', [('Content-Type', 'application/json; charset=utf-8')], json_series(rows).encode()
            table = ''.join(f"<tr><td>{e(r['week'])}</td><td>{e(r['version'])}</td><td>{r['n'] if not r['suppressed'] else '&lt; 3 reviews'}</td><td>{e(r['mean_outcome'] if not r['suppressed'] else 'suppressed')}</td></tr>" for r in rows)
            flags = ''.join(f"<p class='warning'>Possible shift in reported outcomes for {e(f['version'])}: {e(f['from'])} n={f['n_from']}, {e(f['to'])} n={f['n_to']}. Self-reported, not a benchmark.</p>" for f in shifts(db, slug))
            body = f"<p>{e(model['vendor'])}</p><p>Self-reported, not a benchmark. Aggregate cells with fewer than 3 reviews are suppressed. <a href='/methodology'>Methodology</a></p>{flags}{render_svg(rows)}<table><tr><th>Week</th><th>Version</th><th>n</th><th>Mean outcome</th></tr>{table}</table>" + review_list(db, slug)
            return self.html(model['name'], body, session, session_headers)

        if path == '/login' and method == 'GET':
            state = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            sid = secrets.token_urlsafe(32)
            token = auth.session(self.session_key, oauth_state=state, nonce=sid)
            csrf = auth.unpack(self.session_key, token)['csrf']
            db.execute('DELETE FROM oauth_states WHERE expires_at<=?', (now(),))
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (now(),))
            db.execute('INSERT INTO sessions(nonce,user_id,csrf,expires_at,created_at) VALUES(?,NULL,?,?,?)', (sid, csrf, expires, now()))
            db.execute('INSERT INTO oauth_states(state_digest,session_nonce,expires_at,created_at) VALUES(?,?,?,?)', (auth.state_digest(self.state_key, state), sid, expires, now()))
            db.commit()
            return self.redirect(self.oauth.authorize_url(state), [('Set-Cookie', auth.cookie('rt_session', token, self.c.https, 600))])

        if path == '/oauth/callback' and method == 'GET':
            query = urllib.parse.parse_qs(environ.get('QUERY_STRING', ''), keep_blank_values=True)
            supplied, expected = query.get('state', [''])[-1], session.get('oauth_state', '')
            anonymous_sid = session.get('sid', '')
            if not supplied or not expected or not anonymous_sid or not hmac.compare_digest(supplied, expected):
                return self.text('403 Forbidden', 'OAuth state check failed')
            cursor = db.execute('DELETE FROM oauth_states WHERE state_digest=? AND session_nonce=? AND expires_at>?', (auth.state_digest(self.state_key, supplied), anonymous_sid, now()))
            if cursor.rowcount != 1:
                db.rollback()
                return self.text('403 Forbidden', 'OAuth state has expired or was already used')
            try:
                identity = self.oauth.exchange(query.get('code', [''])[-1], supplied)
            except auth.OAuthError:
                db.commit()
                return self.text('403 Forbidden', 'OAuth authentication failed')
            subject = auth.subject_hash(self.subject_key, identity.subject)
            db.execute('INSERT OR IGNORE INTO users(subject_hash,account_month,display_name,created_at) VALUES(?,?,NULL,?)', (subject, identity.created_at.strftime('%Y-%m'), now()))
            uid = db.execute('SELECT id FROM users WHERE subject_hash=?', (subject,)).fetchone()['id']
            sid = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            payload = auth.unpack(self.session_key, auth.session(self.session_key, uid, nonce=sid))
            payload['handle_claim'] = seal_handle(self.handle_key, identity.login)
            token = auth.pack(self.session_key, payload)
            db.execute('DELETE FROM sessions WHERE nonce=?', (anonymous_sid,))
            db.execute('INSERT INTO sessions(nonce,user_id,csrf,expires_at,created_at) VALUES(?,?,?,?,?)', (sid, uid, payload['csrf'], expires, now()))
            db.commit()
            return self.redirect('/account', [('Set-Cookie', auth.cookie('rt_session', token, self.c.https, 86400))])

        if path == '/logout' and method == 'POST':
            if session.get('sid'):
                db.execute('DELETE FROM sessions WHERE nonce=?', (session['sid'],))
                db.commit()
            return self.redirect('/', [('Set-Cookie', auth.cookie('rt_session', '', self.c.https, 0))])

        if path == '/review/new' and method == 'GET':
            if not session.get('uid'):
                return self.redirect('/login')
            return self.html('Write a review', review_form(list_models(db), session['csrf']), session, session_headers)

        if path == '/review' and method == 'POST':
            if not session.get('uid'):
                return self.text('401 Unauthorized', 'Log in first')
            review, errors = validate(form, datetime.now(timezone.utc).date(), {r['slug'] for r in list_models(db)}, proof_key=self.proof_key)
            if form.get('consent') != 'on':
                errors['consent'] = 'Consent is required before publication.'
            display_name = None
            if form.get('show_handle') == 'on':
                display_name = open_handle(self.handle_key, session.get('handle_claim'))
                if not display_name:
                    return self.text('409 Conflict', 'Log in again before opting in to show your GitHub handle.')
            if errors:
                return self.html('Write a review', review_form(list_models(db), session['csrf'], form, errors), session, status='400 Bad Request')
            user = db.execute('SELECT account_month FROM users WHERE id=?', (session['uid'],)).fetchone()
            if not user or not account_old_enough(user['account_month']):
                return self.text('403 Forbidden', "New accounts can't review yet.")
            error = review_error(db, session['uid'], review, datetime.now(timezone.utc))
            if error:
                return self.text('409 Conflict', error)
            try:
                insert(db, session['uid'], review)
                db.execute('UPDATE users SET display_name=? WHERE id=?', (display_name, session['uid']))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                return self.text('409 Conflict', 'This review conflicts with an existing review')
            return self.redirect('/models/' + urllib.parse.quote(review.model_slug))

        if path == '/waitlist/vendor' and method == 'GET':
            prices = ''.join("<option value='" + key + "'>" + e(label) + '</option>' for key, label in PRICE_LABELS.items())
            roles = ''.join("<option value='" + role + "'>" + role + '</option>' for role in sorted(ROLES))
            body = "<p>No payment is taken. We are measuring interest.</p><p>Join the vendor drift-report waitlist. Reports contain aggregates only and confer no editorial control.</p><form method='post'>" + csrf_input + "<label>Contact email<input type='email' name='email' required></label><label>Company<input name='company' maxlength='100'></label><label>Role<select name='role'>" + roles + "</select></label><label>Price interest<select name='price_interest'>" + prices + "</select></label><label><input type='checkbox' name='consent' value='on' required> I consent to contact.</label><button>Join waitlist</button></form>"
            return self.html('Vendor reports', body, session, session_headers)

        if path in {'/signup', '/waitlist/vendor'} and method == 'POST':
            kind = 'reader' if path == '/signup' else 'vendor'
            email = form.get('email', '').strip().lower()
            role, price = form.get('role', ''), form.get('price_interest', '')
            if not EMAIL.fullmatch(email) or form.get('consent') != 'on':
                return self.text('400 Bad Request', 'A valid email and consent are required')
            if kind == 'vendor' and (role not in ROLES or price not in PRICES):
                return self.text('400 Bad Request', 'Invalid vendor signup')
            digest = self.vault.digest(email)
            if not db.execute('SELECT 1 FROM waitlist WHERE kind=? AND contact_digest=?', (kind, digest)).fetchone():
                token = self.vault.put(kind, email)
                try:
                    db.execute('INSERT INTO waitlist(kind,vault_token,contact_digest,company,role,price_interest,created_at) VALUES(?,?,?,?,?,?,?)', (kind, token, digest, form.get('company', '')[:100] if kind == 'vendor' else None, role if kind == 'vendor' else None, price if kind == 'vendor' else None, now()))
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    self.vault.forget(token)
            return self.redirect('/' if kind == 'reader' else '/waitlist/vendor')

        if path == '/account' and method == 'GET':
            if not session.get('uid'):
                return self.redirect('/login')
            user = db.execute('SELECT display_name FROM users WHERE id=?', (session['uid'],)).fetchone()
            count = db.execute('SELECT COUNT(*) FROM reviews WHERE user_id=?', (session['uid'],)).fetchone()[0]
            body = f"<p>Signed in as {e(user['display_name'] or 'user')}. Reviews: {count}.</p><p>Handle visibility applies to your account and all its reviews. Leaving the checkbox off on your next review restores pseudonymous display.</p><p><a href='/account/export'>Export my data</a></p><form method='post' action='/account/delete'>" + csrf_input + '<button>Delete account and reviews</button></form>'
            return self.html('Account', body, session, session_headers)

        if path == '/account/export' and method == 'GET':
            if not session.get('uid'):
                return self.text('401 Unauthorized', 'Log in first')
            user = db.execute('SELECT account_month,display_name,created_at FROM users WHERE id=?', (session['uid'],)).fetchone()
            reviews = [dict(r) for r in db.execute('SELECT model_slug,model_version,task_category,task_description,stack,harness,outcome,retries,cost_usd_cents,used_on,created_at FROM reviews WHERE user_id=? ORDER BY id', (session['uid'],))]
            return '200 OK', [('Content-Type', 'application/json; charset=utf-8'), ('Content-Disposition', 'attachment; filename="reviewtrail-export.json"')], json.dumps({'account': dict(user), 'reviews': reviews}).encode()

        if path == '/account/delete' and method == 'POST':
            if not session.get('uid'):
                return self.text('401 Unauthorized', 'Log in first')
            db.execute('DELETE FROM users WHERE id=?', (session['uid'],))
            db.commit()
            return self.redirect('/', [('Set-Cookie', auth.cookie('rt_session', '', self.c.https, 0))])

        if path in {'/methodology', '/independence', '/privacy'} and method == 'GET':
            name = path[1:]
            return self.html(name.title(), policy(name), session, session_headers)

        if path.startswith('/static/') and method == 'GET':
            asset = path[len('/static/'):]
            types = {'style.css': 'text/css', 'form.js': 'application/javascript'}
            if asset not in types:
                return self.text('404 Not Found', 'Not found')
            try:
                body = (ROOT / 'static' / asset).read_bytes()
            except FileNotFoundError:
                return self.text('404 Not Found', 'Not found')
            return '200 OK', [('Content-Type', types[asset] + '; charset=utf-8')], body
        return self.text('404 Not Found', 'Not found')

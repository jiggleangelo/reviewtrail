#!/usr/bin/env python3
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reviewtrail.directory import load_models
from reviewtrail.reviews import validate, has_sensitive, content_hash, PREFIX_SECRET, CONTEXT_SECRET
from reviewtrail.templates import e
from reviewtrail.timeseries import render_svg
from reviewtrail.workbench import aggregate, compare, report_md, shifts

ROOT = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024
MAX_IMPORT_BODY = 1024 * 1024
MAX_IMPORT_REQUEST = MAX_IMPORT_BODY + 128 * 1024
MAX_RUNS = 5000
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024
MAX_CUSTOM_MODELS = 50
MAX_SAMPLE_WORKSPACES = 100
MAX_CONCURRENCY = 20
MAX_ACTIVE_DATA_REQUESTS = 2
REQUEST_DEADLINE = 10
IMPORT_DEADLINE = 120
IMPORT_BUDGET = 5.0
GLOBAL_CREATIONS_PER_HOUR = 30
GLOBAL_WRITES_PER_HOUR = 3000
GLOBAL_GROWTH_PER_HOUR = 20 * 1024 * 1024
ENTRY_OVERHEAD = 4096
NOTICE = "Operated by AI agents. Early version: keep your own copy of anything important. Do not enter API keys or other secrets, other people's personal data, or confidential client code. Nothing is charged."
COLUMNS = ['model', 'version', 'category', 'task', 'stack', 'harness', 'outcome', 'retries', 'cost_usd', 'date']
GIT_SHA = re.compile(r'(?<![A-Za-z0-9_+/=-])[a-fA-F0-9]{40}(?![A-Za-z0-9_+/=-])')


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.strip().lower()).strip('-')[:60] or 'custom-model'


def encoded_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def atomic_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run_identity(run):
    return tuple(run.get(key) for key in ('model', 'version', 'category', 'task', 'stack', 'harness', 'outcome', 'retries', 'cost_cents', 'date'))


def validate_run(form, models):
    # The legacy validator remains unchanged. Only workbench task descriptions
    # recognize isolated SHA-1 spellings; explicit secret contexts still fail.
    original = str(form.get('task_description', '')).strip()
    candidate = dict(form)
    if len(original) <= 1000 and not PREFIX_SECRET.search(original) and not CONTEXT_SECRET.search(original):
        candidate['task_description'] = GIT_SHA.sub('a-' * 20, original)
    review, errors = validate(candidate, date.today(), set(models), max_age_days=730)
    if review is not None:
        review.task_description = original
        review.content_hash = content_hash(original, review.stack, review.model_version)
    return review, errors


class State:
    def __init__(self, data_dir, base_url, data_cap_mb=200, https_terminated=False):
        self.root = Path(data_dir).resolve()
        self.base = base_url.rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('PUBLIC_BASE_URL must be an absolute HTTP(S) URL')
        self.origin = parsed.scheme + '://' + parsed.netloc
        self.cap = int(data_cap_mb) * 1024 * 1024
        self.https_terminated = bool(https_terminated)
        self.models = load_models(ROOT / 'seed' / 'models.json')
        self.seed = {item['slug']: item['name'] for item in self.models}
        # Fixed stripes never disappear while a holder or waiter references one.
        self.locks = tuple(threading.Lock() for _ in range(256))
        self.locks_guard = threading.Lock()
        self.storage_guard = threading.RLock()
        self.creation_guard = threading.Lock()
        self.write_budgets = OrderedDict()
        self.samples = set()
        for name in ('ws', 'share', 'tombstones', 'meta'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._usage = self._scan_usage()
        self.budget_path = self.root / 'meta' / 'budgets.json'
        try:
            self.budgets = json.loads(self.budget_path.read_text(encoding='utf-8'))
            if not isinstance(self.budgets, dict) or not all(isinstance(self.budgets.get(k), int) and self.budgets[k] >= 0 for k in ('hour', 'creations', 'writes', 'growth')):
                raise ValueError('Invalid counters')
        except FileNotFoundError:
            self.budgets = self._empty_budget()
        except (ValueError, TypeError) as error:
            raise ValueError('Invalid meta/budgets.json; refusing to reset budgets silently') from error
        self.sweep()

    def _empty_budget(self):
        return {'hour': int(time.time() // 3600), 'creations': 0, 'writes': 0, 'growth': 0}

    def _charge(self, path):
        if not path.exists():
            return 0
        if path.is_dir():
            return ENTRY_OVERHEAD + sum(self._charge(child) for child in path.iterdir())
        return ENTRY_OVERHEAD + path.stat().st_size

    def _scan_usage(self):
        # Logical payload plus an explicit reserve for each directory/file entry.
        return self._charge(self.root)

    def usage(self):
        with self.storage_guard:
            return self._usage

    def _consume(self, creations=0, writes=0, growth=0):
        with self.storage_guard:
            current = int(time.time() // 3600)
            budget = dict(self.budgets)
            # A backwards clock adjustment must not reset allowances.
            if current > budget['hour']:
                budget = self._empty_budget()
            for key, amount, limit in (('creations', creations, GLOBAL_CREATIONS_PER_HOUR), ('writes', writes, GLOBAL_WRITES_PER_HOUR), ('growth', growth, GLOBAL_GROWTH_PER_HOUR)):
                if budget[key] + amount > limit:
                    raise RequestError(503, 'Global hourly ' + key + ' limit reached. Please try later.')
                budget[key] += amount
            # Persist reservations before the associated mutation. Failed writes
            # consume reservations; restart cannot replenish the hourly budget.
            self._write(self.budget_path, encoded_json(budget), allow_growth=False)
            self.budgets = budget

    def _write(self, path, raw, allow_growth=True):
        with self.storage_guard:
            old = self._charge(path)
            new = ENTRY_OVERHEAD + len(raw)
            growth = max(0, new - old)
            if allow_growth and growth and self._usage + growth > self.cap:
                raise RequestError(507, 'Storage is full. Export or delete old data.')
            if allow_growth and growth:
                self._consume(growth=growth)
                if self._usage + growth > self.cap:
                    raise RequestError(507, 'Storage is full. Export or delete old data.')
            atomic_bytes(path, raw)
            self._usage += new - old

    def _remove_tree(self, path):
        with self.storage_guard:
            before = self._charge(path)
            if path.exists():
                shutil.rmtree(path)
            self._usage -= before

    def unlink(self, path):
        with self.storage_guard:
            before = self._charge(path)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            self._usage -= before

    def lock(self, whash):
        return self.locks[int(digest(whash)[:8], 16) % len(self.locks)]

    def allow_write(self, whash):
        current = time.time()
        with self.locks_guard:
            values = [stamp for stamp in self.write_budgets.pop(whash, []) if stamp >= current - 3600]
            if len(values) >= 300:
                self.write_budgets[whash] = values
                return False
            try:
                self._consume(writes=1)
            except RequestError:
                return False
            values.append(current)
            self.write_budgets[whash] = values
            while len(self.write_budgets) > 4096:
                self.write_budgets.popitem(last=False)
            return True

    def create(self, sample=False):
        # Creation and sample-index updates are serialized. Requests never scan
        # workspace contents to discover the number of samples.
        with self.creation_guard:
            if sample and len(self.samples) >= MAX_SAMPLE_WORKSPACES:
                raise RequestError(503, 'The sample-workspace limit is busy. Start an empty workspace instead.')
            self._consume(creations=1, writes=1)
            token = secrets.token_urlsafe(24)
            whash = digest(token)
            stamp = now()
            workspace = {'v': 1, 'created': stamp, 'last_active': stamp, 'custom_models': [], 'share_hash': None, 'runs': self.sample_runs() if sample else [], 'sample_workspace': sample}
            directory = self.root / 'ws' / whash
            with self.storage_guard:
                if self._usage + ENTRY_OVERHEAD > self.cap:
                    raise RequestError(507, 'Storage is full. Export or delete old data.')
                directory.mkdir(mode=0o700)
                self._usage += ENTRY_OVERHEAD
                try:
                    self._write(directory / 'workspace.json', encoded_json(workspace))
                except Exception:
                    self._remove_tree(directory)
                    raise
                if sample:
                    self.samples.add(whash)
            return token

    def sample_runs(self):
        runs = []
        today = date.today()
        for offset in range(6):
            used = today - timedelta(days=(5 - offset) * 7)
            for model, version, baseline in [('claude-code', 'sample-1', 4), ('openai-codex', 'sample-2', 4), ('gemini-cli', 'sample-1', 3)]:
                for index in range(5 if model == 'claude-code' and offset in (3, 4) else 2):
                    number = len(runs) + 1
                    outcome = (5 if offset == 3 else 3) if model == 'claude-code' and offset in (3, 4) else baseline
                    task = f'Made-up sample task {number}: update a parser and verify regression tests against representative inputs.'
                    runs.append({'id': number, 'model': model, 'version': version, 'category': ('coding', 'agents')[number % 2], 'task': task, 'stack': 'python/sqlite', 'harness': 'cli-agent', 'outcome': outcome, 'retries': index % 3, 'cost_cents': 100 + index * 25, 'date': used.isoformat(), 'week': f'{used.isocalendar().year}-W{used.isocalendar().week:02d}', 'hash': content_hash(task, 'python/sqlite', version), 'sample': True})
        return runs

    def load_hash(self, whash):
        if not re.fullmatch(r'[a-f0-9]{64}', whash):
            return None
        try:
            path = self.root / 'ws' / whash / 'workspace.json'
            # Check before allocating or decoding the persisted document.
            with path.open('rb') as source:
                if os.fstat(source.fileno()).st_size > MAX_WORKSPACE_BYTES:
                    return None
                value = json.loads(source.read(MAX_WORKSPACE_BYTES + 1))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def load_token(self, token):
        whash = digest(token)
        return whash, self.load_hash(whash)

    def load_share(self, token):
        shash = digest(token)
        try:
            whash = (self.root / 'share' / shash).read_text(encoding='ascii').strip()
        except OSError:
            return None, None
        workspace = self.load_hash(whash)
        if not workspace or workspace.get('share_hash') != shash:
            return None, None
        return whash, workspace

    def save(self, whash, workspace):
        directory = self.root / 'ws' / whash
        if (self.root / 'tombstones' / whash).exists() or not directory.is_dir():
            raise RequestError(404, 'Workspace not found.')
        workspace['last_active'] = now()
        raw = encoded_json(workspace)
        if len(raw) > MAX_WORKSPACE_BYTES:
            raise RequestError(413, 'This write would push the workspace over 5 MB.')
        self._write(directory / 'workspace.json', raw)

    def write_share(self, shash, whash):
        self._write(self.root / 'share' / shash, whash.encode('ascii'))

    def delete(self, whash, workspace):
        with self.storage_guard:
            self._write(self.root / 'tombstones' / whash, now().encode('ascii'), allow_growth=False)
            if workspace.get('share_hash'):
                self.unlink(self.root / 'share' / workspace['share_hash'])
            self._remove_tree(self.root / 'ws' / whash)
            self.samples.discard(whash)

    def sweep(self):
        current = datetime.now(timezone.utc)
        # Startup/maintenance alone scans workspaces. Lock before loading to
        # avoid deleting or indexing stale snapshots during concurrent writes.
        for directory in list((self.root / 'ws').iterdir()):
            with self.lock(directory.name):
                workspace = self.load_hash(directory.name)
                if not workspace or not workspace.get('sample_workspace'):
                    continue
                with self.storage_guard:
                    self.samples.add(directory.name)
                try:
                    expired = current - datetime.fromisoformat(workspace['created']) > timedelta(hours=24)
                except (KeyError, ValueError, TypeError):
                    continue
                if expired:
                    self.delete(directory.name, workspace)
        for tombstone in list((self.root / 'tombstones').iterdir()):
            try:
                if current - datetime.fromisoformat(tombstone.read_text(encoding='ascii')) > timedelta(days=7):
                    self.unlink(tombstone)
            except (OSError, ValueError, TypeError):
                pass


class CappedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler, state):
        self.state = state
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self._data_slots = threading.BoundedSemaphore(MAX_ACTIVE_DATA_REQUESTS)
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'ReviewTrail/1.2'
    timeout = 10
    request_deadline = REQUEST_DEADLINE
    import_deadline = IMPORT_DEADLINE

    def log_message(self, format, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def _arm_deadline(self, seconds):
        old = getattr(self, '_timer', None)
        if old:
            old.cancel()
            old.join()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def handle(self):
        self._arm_deadline(self.request_deadline)
        try:
            super().handle()
        except OSError:
            self.close_connection = True
        finally:
            self._timer.cancel()
            self._timer.join()

    def _expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _url(self, path):
        return self.state.base + path

    def _send(self, status, body, content_type='text/html; charset=utf-8', headers=None):
        payload = body if isinstance(body, bytes) else body.encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            if self.state.https_terminated:
                self.send_header('Strict-Transport-Security', 'max-age=31536000')
            if urllib.parse.urlsplit(self.path).path.startswith(('/w/', '/s/')):
                self.send_header('Cache-Control', 'no-store')
            for name, value in headers or []:
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)
        except OSError:
            self.close_connection = True

    def _page(self, title, body, shared=False):
        style = 'body{font:16px system-ui;max-width:1100px;margin:auto;padding:1rem;color:#172033}nav{display:flex;gap:1rem;flex-wrap:wrap}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #ccd3df;padding:.5rem;text-align:left}label{display:block;margin:.7rem 0}input,select,textarea,button{font:inherit;padding:.45rem;max-width:100%}textarea{width:100%;min-height:10rem}.notice,.warning{background:#fff1c7;padding:.8rem}.error{color:#991b1b}.success,.shared{background:#e6f5ea;padding:.7rem}.low{font-weight:bold;color:#7a4d00}.scroll{overflow:auto}'
        return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>" + e(title) + ' · ReviewTrail</title><style>' + style + "</style></head><body><header><a href='" + e(self._url('/')) + "'>ReviewTrail</a></header><div class='notice'>" + e(NOTICE) + " <a href='https://manykiln.com/privacy'>Privacy</a></div>" + ("<p class='shared'>Read-only shared view</p>" if shared else '') + '<main><h1>' + e(title) + '</h1>' + body + '</main></body></html>'

    def _error(self, status, message):
        self._send(status, self._page('Request problem', '<p class="error">' + e(message) + '</p>'))

    def _redirect(self, path):
        self._send(303, b'', headers=[('Location', self._url(path))])

    def _security(self):
        if self.command != 'POST':
            return
        origins = self.headers.get_all('Origin', [])
        referers = self.headers.get_all('Referer', [])
        if len(origins) > 1 or len(referers) > 1:
            raise RequestError(403, 'Same-origin form submission required.')
        origin = self.headers.get('Origin')
        parsed = urllib.parse.urlsplit(self.headers.get('Referer', ''))
        referer_origin = parsed.scheme + '://' + parsed.netloc if parsed.scheme and parsed.netloc else ''
        if not ((origin and origin == self.state.origin) or (not origin and referer_origin == self.state.origin)):
            raise RequestError(403, 'Same-origin form submission required.')

    def _raw(self, limit):
        if self.headers.get('Transfer-Encoding'):
            raise RequestError(400, 'Transfer-Encoding is not supported.')
        values = self.headers.get_all('Content-Length', [])
        if len(values) != 1:
            raise RequestError(400, 'Exactly one Content-Length is required.')
        try:
            length = int(values[0])
        except ValueError as error:
            raise RequestError(400, 'Invalid Content-Length.') from error
        if length < 0 or length > limit:
            raise RequestError(413, 'Request body is too large.')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, 'Incomplete request body.')
        return raw

    def _form(self, limit=MAX_BODY):
        raw = self._raw(limit)
        content_type = self.headers.get('Content-Type', '')
        if content_type.startswith('multipart/form-data'):
            try:
                message = BytesParser(policy=default).parsebytes(('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
                if not message.is_multipart():
                    raise RequestError(400, 'Invalid multipart form.')
                result = {}
                for part in message.iter_parts():
                    name = part.get_param('name', header='content-disposition')
                    if name:
                        payload = part.get_payload(decode=True)
                        if payload is None:
                            raise RequestError(400, 'Invalid multipart field.')
                        result[name] = payload.decode('utf-8')
                return result
            except UnicodeDecodeError as error:
                raise RequestError(400, 'Uploads must be UTF-8.') from error
        try:
            parsed = urllib.parse.parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=100)
        except (UnicodeDecodeError, ValueError) as error:
            raise RequestError(400, 'Invalid UTF-8 form body.') from error
        return {key: values[-1] for key, values in parsed.items()}

    def _models(self, workspace):
        return dict(self.state.seed, **{item['slug']: item['name'] for item in workspace.get('custom_models', [])})

    def _write_guard(self, whash):
        if not self.state.allow_write(whash):
            raise RequestError(503, 'Workspace or global hourly write limit reached. Please try later.')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, post):
        acquired = False
        try:
            path = urllib.parse.urlsplit(self.path).path
            if not post and path == '/healthz':
                self._send(200, 'ok', 'text/plain; charset=utf-8')
                return
            self._security()
            # Admission precedes all body parsing and workspace loading, so
            # excess clients cannot each retain a full parsed workspace.
            acquired = self.server._data_slots.acquire(blocking=False)
            if not acquired:
                raise RequestError(503, 'The workbench is busy. Please retry shortly.')
            if post and re.fullmatch(r'/w/[A-Za-z0-9_-]+/import', path):
                self._arm_deadline(self.import_deadline)
                self.connection.settimeout(self.import_deadline)
            self._post() if post else self._get()
        except RequestError as error:
            self._error(error.status, error.message)
        except Exception:
            self._error(500, 'Internal server error.')
        finally:
            if acquired:
                self.server._data_slots.release()

    def _get(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/':
            body = '<p>Keep a private structured log of coding-model runs and inspect arithmetic weekly outcomes, retries, cost and possible shifts.</p><p>No AI is used. Nothing is published site-wide.</p>'
            if urllib.parse.parse_qs(parsed.query).get('deleted') == ['1']:
                body += '<p class="success">Workspace deleted.</p>'
            for path, label in [('/w', 'Start a workspace'), ('/w/sample', 'Try with sample data')]:
                body += '<form method="post" action="' + e(self._url(path)) + '"><button>' + label + '</button></form>'
            self._send(200, self._page('Private model-run workbench', body))
            return
        match = re.fullmatch(r'/(w|s)/([A-Za-z0-9_-]+)(.*)', parsed.path)
        if not match:
            raise RequestError(404, 'Page not found.')
        kind, token, rest = match.groups()
        _, workspace = self.state.load_token(token) if kind == 'w' else self.state.load_share(token)
        if not workspace:
            raise RequestError(404, 'Workspace or shared view not found.')
        prefix = '/' + kind + '/' + token
        shared = kind == 's'
        if rest in ('', '/'):
            self._dashboard(prefix, workspace, not shared, parsed.query)
        elif rest == '/compare':
            self._compare(prefix, workspace, parsed.query, shared)
        elif not shared and rest == '/runs/new':
            self._run_form(prefix, workspace)
        elif not shared and rest == '/runs':
            self._runs(prefix, workspace, parsed.query)
        elif not shared and rest == '/import':
            self._import_form(prefix, workspace)
        elif not shared and rest == '/import/template.csv':
            text = ','.join(COLUMNS) + '\nclaude-code,release-2026.10,coding,"Add validation and regression tests to the CSV import command",python/sqlite,cli-agent,4,1,1.25,' + date.today().isoformat() + '\n'
            self._send(200, text, 'text/csv; charset=utf-8', [('Content-Disposition', 'attachment; filename="reviewtrail-template.csv"')])
        elif not shared and rest == '/export':
            exported = dict(workspace)
            exported['share_status'] = bool(exported.pop('share_hash', None))
            self._send(200, json.dumps(exported, ensure_ascii=False, indent=2), 'application/json; charset=utf-8', [('Content-Disposition', 'attachment; filename="reviewtrail-export.json"')])
        else:
            model = re.fullmatch(r'/models/([^/]+)(/report\.md)?', rest)
            if not model or (shared and model.group(2)):
                raise RequestError(404, 'Page not found.')
            slug = urllib.parse.unquote(model.group(1))
            if slug not in self._models(workspace):
                raise RequestError(404, 'Model not found.')
            if model.group(2):
                self._send(200, report_md(workspace['runs'], slug, 8), 'text/markdown; charset=utf-8', [('Content-Disposition', 'attachment; filename="reviewtrail-drift-report.md"')])
            else:
                self._model(prefix, workspace, slug, shared)

    def _post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/w', '/w/sample'):
            self._form()
            self._redirect('/w/' + self.state.create(path.endswith('/sample')) + '?new=1')
            return
        match = re.fullmatch(r'/w/([A-Za-z0-9_-]+)(.*)', path)
        if not match:
            raise RequestError(404, 'Page not found.')
        token, rest = match.groups()
        whash = digest(token)
        with self.state.lock(whash):
            workspace = self.state.load_hash(whash)
            if not workspace:
                raise RequestError(404, 'Workspace not found.')
            if rest == '/runs':
                self._write_guard(whash)
                self._add_run(token, whash, workspace, self._form())
            elif rest == '/import':
                self._write_guard(whash)
                self._import(token, whash, workspace, self._form(MAX_IMPORT_REQUEST))
            elif re.fullmatch(r'/runs/\d+/delete', rest):
                self._form()
                run_id = int(rest.split('/')[2])
                workspace['runs'] = [run for run in workspace['runs'] if run['id'] != run_id]
                self.state.save(whash, workspace)
                self._redirect('/w/' + token + '/runs')
            elif rest == '/share':
                form = self._form()
                if form.get('action') != 'revoke':
                    self._write_guard(whash)
                self._share(token, whash, workspace, form)
            elif rest == '/delete':
                if self._form().get('confirm') != 'on':
                    raise RequestError(400, 'Check the confirmation box to delete this workspace.')
                self.state.delete(whash, workspace)
                self._redirect('/?deleted=1')
            else:
                raise RequestError(404, 'Page not found.')

    def _nav(self, prefix, writable=True):
        links = [(prefix, 'Dashboard'), (prefix + '/compare', 'Compare')]
        if writable:
            links += [(prefix + '/runs', 'Runs'), (prefix + '/runs/new', 'Add run'), (prefix + '/import', 'Import')]
        return '<nav>' + ''.join('<a href="' + e(self._url(path)) + '">' + e(label) + '</a>' for path, label in links) + '</nav>'

    def _table(self, headings, rows):
        return '<div class="scroll"><table><tr>' + ''.join('<th>' + e(label) + '</th>' for label in headings) + '</tr>' + ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows) + '</table></div>'

    def _cost(self, value):
        return '—' if value is None else f'${value / 100:.2f}'

    def _dashboard(self, prefix, workspace, writable, query=''):
        names = self._models(workspace)
        rows = []
        for item in compare(workspace['runs'], None):
            slug = item['model']
            runs = [run for run in workspace['runs'] if run['model'] == slug]
            latest = max(runs, key=lambda run: (run['date'], run['id']))
            rows.append(['<a href="' + e(self._url(prefix + '/models/' + slug)) + '">' + e(names.get(slug, slug)) + '</a>', e(item['n']), e(latest['version']), e(item['mean_outcome']), e(item['mean_retries']), e(self._cost(item['median_cost'])), e(latest['week']), 'possible shift' if shifts(workspace['runs'], slug) else '—'])
        body = self._nav(prefix, writable)
        if writable and urllib.parse.parse_qs(query).get('new') == ['1']:
            body += '<p class="warning">Bookmark this link. It is the only key and cannot be recovered.</p>'
        if workspace.get('sample_workspace'):
            body += '<p class="warning">This is sample data. Sample workspaces expire after 24 hours; export anything you want to keep.</p>'
        body += '<p>Possible shift is a self-reported arithmetic heuristic, not a benchmark or confirmed regression.</p>'
        body += self._table(['Model', 'Runs', 'Latest version', 'Mean outcome', 'Mean retries', 'Median cost', 'Last week', 'Signal'], rows)
        if writable:
            body += '<p><a href="' + e(self._url(prefix + '/export')) + '">Export everything (JSON)</a></p><form method="post" action="' + e(self._url(prefix + '/share')) + '"><button name="action" value="create">Create share link</button> <button name="action" value="rotate">Rotate</button> <button name="action" value="revoke">Revoke</button></form><form method="post" action="' + e(self._url(prefix + '/delete')) + '"><label><input type="checkbox" name="confirm"> Confirm permanent deletion</label><button>Delete this workspace now</button></form>'
        self._send(200, self._page('Workspace dashboard', body, not writable))

    def _run_form(self, prefix, workspace, values=None, errors=None):
        values, errors = values or {}, errors or {}
        body = self._nav(prefix)
        body += '<ul class="error">' + ''.join('<li>' + e(field) + ': ' + e(message) + '</li>' for field, message in errors.items()) + '</ul>'
        body += '<form method="post" action="' + e(self._url(prefix + '/runs')) + '">'
        choices = [('model_slug', 'Model', self._models(workspace)), ('task_category', 'Category', {x: x for x in ('coding', 'extraction', 'rag', 'agents')}), ('harness', 'Harness', {x: x for x in ('cli-agent', 'api-direct', 'openrouter', 'ide-plugin', 'other')})]
        for field, label, options in choices:
            body += '<label>' + label + '<select name="' + field + '">'
            body += ''.join('<option value="' + e(key) + '"' + (' selected' if values.get(field) == key else '') + '>' + e(name) + '</option>' for key, name in options.items()) + '</select></label>'
        for field, label, default_value in [('custom_model', 'Or add custom model', ''), ('model_version', 'Version', ''), ('stack', 'Stack', ''), ('harness_other', 'Other harness details', ''), ('outcome', 'Outcome (1–5)', ''), ('retries', 'Retries (0–50)', '0'), ('cost_usd', 'Cost USD', ''), ('used_on', 'Date', date.today().isoformat())]:
            body += '<label>' + label + '<input name="' + field + '" value="' + e(values.get(field, default_value)) + '"></label>'
        body += '<label>Task<textarea name="task_description" minlength="40" maxlength="1000" required>' + e(values.get('task_description', '')) + '</textarea></label><button>Add run</button></form>'
        self._send(400 if errors else 200, self._page('Add a run', body))

    def _custom_model(self, workspace, name, errors):
        name = str(name).strip()
        if not name:
            return None
        if len(name) > 80:
            errors['custom_model'] = 'Use at most 80 characters for a model name.'
            return None
        if has_sensitive(name, phone=False):
            errors['custom_model'] = 'Remove personal data or secrets from the model name. Dated model names are allowed.'
            return None
        existing = self._models(workspace)
        for slug, existing_name in existing.items():
            if existing_name.casefold() == name.casefold():
                return slug
        if len(workspace['custom_models']) >= MAX_CUSTOM_MODELS:
            errors['custom_model'] = 'Use at most 50 custom models.'
            return None
        slug = safe_slug(name)
        if slug in existing:
            slug = slug[:53] + '-' + digest(name)[:6]
        if slug in existing:
            errors['custom_model'] = 'This model name conflicts with an existing model.'
            return None
        workspace['custom_models'].append({'slug': slug, 'name': name})
        return slug

    def _add_run(self, token, whash, workspace, form):
        errors = {}
        custom = self._custom_model(workspace, form.get('custom_model', ''), errors)
        if custom:
            form['model_slug'] = custom
        if not errors:
            review, errors = validate_run(form, self._models(workspace))
        if errors:
            self._run_form('/w/' + token, workspace, form, errors)
            return
        if len(workspace['runs']) >= MAX_RUNS:
            raise RequestError(413, 'This workspace already has 5,000 runs. Delete runs before adding more.')
        workspace['runs'].append(self._stored(review, max((run['id'] for run in workspace['runs']), default=0) + 1, False))
        self.state.save(whash, workspace)
        self._redirect('/w/' + token + '/models/' + review.model_slug)

    def _stored(self, review, run_id, sample):
        return {'id': run_id, 'model': review.model_slug, 'version': review.model_version, 'category': review.task_category, 'task': review.task_description, 'stack': review.stack, 'harness': review.harness, 'outcome': review.outcome, 'retries': review.retries, 'cost_cents': review.cost_usd_cents, 'date': review.used_on, 'week': review.iso_week, 'hash': review.content_hash, 'sample': sample}

    def _runs(self, prefix, workspace, query):
        try:
            page = max(1, int(urllib.parse.parse_qs(query).get('page', ['1'])[-1]))
        except ValueError:
            page = 1
        items = sorted(workspace['runs'], key=lambda run: (run['date'], run['id']), reverse=True)
        pages = max(1, (len(items) + 49) // 50)
        page = min(page, pages)
        names = self._models(workspace)
        rows = []
        for run in items[(page - 1) * 50:page * 50]:
            rows.append([e(run['date']), e(names.get(run['model'], run['model'])), e(run['version']), e(run['category']), e(run['task']), e(run['outcome']), '<form method="post" action="' + e(self._url(prefix + '/runs/' + str(run['id']) + '/delete')) + '"><button>Delete</button></form>'])
        navigation = ('<a href="?page=' + str(page - 1) + '">Previous</a> ' if page > 1 else '') + 'Page ' + str(page) + ' of ' + str(pages)
        if page < pages:
            navigation += ' <a href="?page=' + str(page + 1) + '">Next</a>'
        self._send(200, self._page('Runs', self._nav(prefix) + self._table(['Date', 'Model', 'Version', 'Category', 'Task', 'Outcome', ''], rows) + '<nav>' + navigation + '</nav>'))

    def _import_form(self, prefix, workspace):
        body = self._nav(prefix)
        if len(workspace['runs']) >= MAX_RUNS:
            body += '<p class="warning">Workspace has reached 5,000 runs. Export or delete runs before importing more.</p>'
        else:
            body += '<p>UTF-8 CSV with the documented header; at most 1 MB and 2,000 rows. Unknown model names become workspace custom models.</p><p><a href="' + e(self._url(prefix + '/import/template.csv')) + '">Download template CSV</a></p><p>You may also paste or upload a previous ReviewTrail JSON export.</p><form method="post" enctype="multipart/form-data" action="' + e(self._url(prefix + '/import')) + '"><label>Paste CSV or JSON<textarea name="data"></textarea></label><label>Or upload<input type="file" name="file" accept=".csv,.json,text/csv,application/json"></label><button>Import</button></form>'
        self._send(200, self._page('Import runs', body))

    def _restore_models(self, workspace, entries):
        if not isinstance(entries, list) or len(entries) > MAX_CUSTOM_MODELS:
            raise RequestError(400, 'Invalid custom models in JSON export.')
        aliases = {}
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('slug'), str):
                raise RequestError(400, 'Invalid custom model in JSON export.')
            name, desired = item['name'].strip(), item['slug'].strip()
            if not name or not re.fullmatch(r'[a-z0-9-]{1,60}', desired) or desired in aliases:
                raise RequestError(400, 'Invalid or duplicate custom model slug in JSON export.')
            existing = self._models(workspace)
            if desired in existing and existing[desired].casefold() != name.casefold():
                raise RequestError(400, 'An imported model slug conflicts with an existing model.')
            errors = {}
            before = len(workspace['custom_models'])
            slug = self._custom_model(workspace, name, errors)
            if errors or not slug:
                raise RequestError(400, next(iter(errors.values()), 'Invalid custom model name.'))
            if len(workspace['custom_models']) > before:
                # Only the entry created by this operation may be renamed.
                created = next(entry for entry in workspace['custom_models'] if entry['slug'] == slug)
                if desired not in existing:
                    created['slug'] = desired
                    slug = desired
            aliases[desired] = slug
        return aliases

    def _import(self, token, whash, workspace, form):
        text = form.get('file') or form.get('data', '')
        if len(text.encode('utf-8')) > MAX_IMPORT_BODY:
            raise RequestError(413, 'Import exceeds 1 MB.')
        prefix = '/w/' + token
        restored = []
        if text.lstrip().startswith(('{', '[')):
            try:
                source = json.loads(text)
                incoming = source if isinstance(source, list) else source['runs']
                restored = source.get('custom_models', []) if isinstance(source, dict) else []
                if not isinstance(incoming, list) or not all(isinstance(item, dict) for item in incoming):
                    raise ValueError
                rows = [{'model': item.get('model'), 'version': item.get('version'), 'category': item.get('category'), 'task': item.get('task'), 'stack': item.get('stack'), 'harness': item.get('harness'), 'outcome': item.get('outcome'), 'retries': item.get('retries'), 'cost_usd': '' if item.get('cost_cents') is None else f"{item['cost_cents'] / 100:.2f}", 'date': item.get('date')} for item in incoming]
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                raise RequestError(400, 'Invalid JSON export.') from error
        else:
            try:
                reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
                if reader.fieldnames != COLUMNS:
                    raise RequestError(400, 'CSV header must exactly match the documented columns.')
                rows = list(reader)
                if any(None in row for row in rows):
                    raise RequestError(400, 'CSV rows must not contain extra columns.')
            except csv.Error as error:
                raise RequestError(400, 'Invalid CSV.') from error
        if len(rows) > 2000:
            raise RequestError(413, 'Import contains more than 2,000 rows.')
        aliases = self._restore_models(workspace, restored)
        for row in rows:
            model = str(row.get('model') or '').strip()
            row['model'] = aliases.get(model, model)
        known = self._models(workspace)
        names = {name.casefold(): slug for slug, name in known.items()}
        identities = {run_identity(run) for run in workspace['runs']}
        hashes = {run['hash'] for run in workspace['runs']}
        added = duplicates = similar = 0
        errors, remainder = [], []
        started = time.monotonic()
        next_id = max((run['id'] for run in workspace['runs']), default=0) + 1
        full = False
        for index, row in enumerate(rows):
            if len(workspace['runs']) >= MAX_RUNS:
                full = True
                errors.append((index + 2, 'Workspace has reached 5,000 runs. Remaining rows were not imported. Export or delete runs before retrying the original file.'))
                break
            if time.monotonic() - started >= IMPORT_BUDGET:
                remainder = rows[index:]
                break
            model = row['model']
            resolved = model if model in known else names.get(model.casefold())
            if not resolved:
                custom_errors = {}
                resolved = self._custom_model(workspace, model, custom_errors)
                if custom_errors or not resolved:
                    errors.append((index + 2, custom_errors.get('custom_model', 'Model is required.')))
                    continue
                known = self._models(workspace)
                names = {name.casefold(): slug for slug, name in known.items()}
            mapping = {'model_version': 'version', 'task_category': 'category', 'task_description': 'task', 'stack': 'stack', 'harness': 'harness', 'outcome': 'outcome', 'retries': 'retries', 'cost_usd': 'cost_usd', 'used_on': 'date'}
            candidate = {key: '' if row.get(column) is None else str(row[column]) for key, column in mapping.items()}
            candidate['model_slug'] = resolved
            review, row_errors = validate_run(candidate, known)
            if row_errors:
                errors.append((index + 2, '; '.join(row_errors.values())))
                continue
            stored = self._stored(review, next_id, False)
            identity = run_identity(stored)
            if identity in identities:
                duplicates += 1
                continue
            if review.content_hash in hashes:
                similar += 1
            workspace['runs'].append(stored)
            identities.add(identity)
            hashes.add(review.content_hash)
            next_id += 1
            added += 1
        self.state.save(whash, workspace)
        continuation = ''
        if remainder and not full and len(workspace['runs']) < MAX_RUNS:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator='\n', extrasaction='ignore')
            writer.writeheader()
            writer.writerows(remainder)
            continuation = '<p class="warning">The five-second import budget was reached. Saved completed rows.</p><form method="post" enctype="multipart/form-data" action="' + e(self._url(prefix + '/import')) + '"><textarea name="data" hidden>' + e(output.getvalue()) + '</textarea><button>Continue</button></form>'
        summary = f'<p>{added} added; {duplicates} exact duplicates skipped; {similar} repeated-task runs retained; {len(errors)} invalid.</p>'
        error_html = '<ul class="error">' + ''.join('<li>Line ' + e(line) + ': ' + e(message) + '</li>' for line, message in errors) + '</ul>'
        self._send(200, self._page('Import results', self._nav(prefix) + summary + error_html + continuation))

    def _model(self, prefix, workspace, slug, shared):
        names = self._models(workspace)
        rows = aggregate(workspace['runs'], slug)
        body = self._nav(prefix, not shared)
        for flag in shifts(workspace['runs'], slug):
            body += '<p class="warning">Possible shift for ' + e(flag['version']) + ': ' + e(flag['from']) + ' to ' + e(flag['to']) + ', outcome dropped ' + e(flag['drop']) + '. Self-reported heuristic; not a benchmark or confirmed regression.</p>'
        body += '<div class="scroll">' + (render_svg(rows) if rows else '<p>No runs yet.</p>') + '</div><h2>Version-change markers</h2><ul>'
        first = {}
        for row in rows:
            first.setdefault(row['version'], row['week'])
        body += ''.join('<li>' + e(version) + ': first appears ' + e(week) + '</li>' for version, week in sorted(first.items(), key=lambda item: item[1])) + '</ul>'
        body += self._table(['ISO week', 'Version', 'n', 'Mean outcome', 'Mean retries', 'Median cost'], [[e(row['week']), e(row['version']), e(row['n']) + (' <span class="low">low n</span>' if row['low_n'] else ''), e(row['mean_outcome']), e(row['mean_retries']), e(self._cost(row['median_cost']))] for row in rows])
        if not shared:
            body += '<p><a href="' + e(self._url(prefix + '/models/' + slug + '/report.md')) + '">Download Markdown drift report</a></p>'
        self._send(200, self._page(names[slug], body, shared))

    def _compare(self, prefix, workspace, query, shared):
        category = urllib.parse.parse_qs(query).get('category', [''])[-1]
        if category not in ('', 'coding', 'extraction', 'rag', 'agents'):
            category = ''
        names = self._models(workspace)
        options = ''.join('<option value="' + value + '"' + (' selected' if category == value else '') + '>' + (value or 'all') + '</option>' for value in ('', 'coding', 'extraction', 'rag', 'agents'))
        body = self._nav(prefix, not shared) + '<form method="get"><label>Category<select name="category">' + options + '</select></label><button>Filter</button></form>'
        body += self._table(['Model', 'n', 'Mean outcome', 'Mean retries', 'Median cost'], [[e(names.get(row['model'], row['model'])), e(row['n']), e(row['mean_outcome']), e(row['mean_retries']), e(self._cost(row['median_cost']))] for row in compare(workspace['runs'], category or None)])
        self._send(200, self._page('Cross-model comparison', body, shared))

    def _share(self, token, whash, workspace, form):
        action = form.get('action')
        old = workspace.get('share_hash')
        share_token = new_hash = None
        if action in ('create', 'rotate'):
            share_token = secrets.token_urlsafe(24)
            new_hash = digest(share_token)
            self.state.write_share(new_hash, whash)
            workspace['share_hash'] = new_hash
        elif action == 'revoke':
            workspace['share_hash'] = None
        else:
            raise RequestError(400, 'Invalid share action.')
        try:
            self.state.save(whash, workspace)
        except Exception:
            if new_hash:
                self.state.unlink(self.state.root / 'share' / new_hash)
            raise
        if old and old != new_hash:
            self.state.unlink(self.state.root / 'share' / old)
        if share_token:
            link = self._url('/s/' + share_token)
            self._send(200, self._page('Share link', '<p>Copy this read-only link now:</p><p><a href="' + e(link) + '">' + e(link) + '</a></p>'))
        else:
            self._redirect('/w/' + token)


def create_server(host='127.0.0.1', port=8080, data_dir=None, public_base_url=None, data_cap_mb=200, https_terminated=False):
    state = State(data_dir or os.environ.get('DATA_DIR', './reviewtrail-data'), public_base_url or f'http://{host}:{port}', data_cap_mb, https_terminated)
    server = CappedServer((host, port), Handler, state)
    if public_base_url is None and port == 0:
        state.base = f'http://{host}:{server.server_address[1]}'
        state.origin = state.base
    return server


def sweep_loop(state):
    while True:
        time.sleep(86400)
        state.sweep()


def main():
    host = os.environ.get('APP_HOST', '127.0.0.1')
    try:
        port = int(os.environ.get('PORT', '8080'))
        cap = int(os.environ.get('APP_DATA_CAP_MB', '200'))
    except ValueError as error:
        raise SystemExit('PORT and APP_DATA_CAP_MB must be integers') from error
    base = os.environ.get('PUBLIC_BASE_URL', f'http://{host}:{port}')
    server = create_server(host, port, os.environ.get('DATA_DIR', './reviewtrail-data'), base, cap, os.environ.get('APP_HTTPS_TERMINATED', '').lower() in ('1', 'true', 'yes'))
    threading.Thread(target=sweep_loop, args=(server.state,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

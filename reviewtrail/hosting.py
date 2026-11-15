"""Hosting-container safeguards for the private workbench HTTP server.

The workbench deliberately uses the standard-library HTTP server. Two details
need handling at the BaseHTTPRequestHandler boundary, before app.py dispatches
requests:

* Host must match PUBLIC_BASE_URL, except for /healthz probes.
* Rejected request bodies must be drained before the HTTP/1.0 socket closes.
  Otherwise a close with unread client data can become a TCP reset, causing
  clients to report URLError instead of receiving the intended 413 response.

The installation is idempotent and applies only to app.py's ReviewTrail/*
handler. The historical ReviewTrailDemo handler is not changed.
"""

import urllib.parse
from http.server import BaseHTTPRequestHandler

_INSTALLED = False


class _CountingReader:
    def __init__(self, source):
        self.source = source
        self.count = 0

    def _record(self, value):
        if value is not None:
            self.count += len(value)
        return value

    def read(self, size=-1):
        return self._record(self.source.read(size))

    def read1(self, size=-1):
        return self._record(self.source.read1(size))

    def readline(self, size=-1):
        return self._record(self.source.readline(size))

    def readinto(self, buffer):
        result = self.source.readinto(buffer)
        if result is not None:
            self.count += result
        return result

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self.source)
        self.count += len(value)
        return value

    def __getattr__(self, name):
        return getattr(self.source, name)


def _is_workbench(handler):
    return str(getattr(handler, 'server_version', '')).startswith('ReviewTrail/')


def _authority(value, scheme):
    if not isinstance(value, str) or not value or any(character in value for character in '/?#@'):
        return None
    try:
        parsed = urllib.parse.urlsplit(scheme + '://' + value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.path or parsed.query or parsed.fragment:
        return None
    if port is None:
        port = 443 if scheme == 'https' else 80
    return hostname.rstrip('.').lower(), port


def _host_allowed(handler):
    # Platform health probes are intentionally allowed to use an internal Host.
    if urllib.parse.urlsplit(getattr(handler, 'path', '')).path == '/healthz':
        return True
    state = getattr(getattr(handler, 'server', None), 'state', None)
    base = getattr(state, 'base', '')
    try:
        public = urllib.parse.urlsplit(base)
    except ValueError:
        return False
    hosts = handler.headers.get_all('Host', [])
    if len(hosts) != 1 or public.scheme not in ('http', 'https'):
        return False
    return _authority(hosts[0].strip(), public.scheme) == _authority(public.netloc, public.scheme)


def _remaining_body(handler):
    start = getattr(handler, '_reviewtrail_body_start', None)
    reader = getattr(handler, 'rfile', None)
    if start is None or not isinstance(reader, _CountingReader):
        return 0
    values = handler.headers.get_all('Content-Length', []) if getattr(handler, 'headers', None) else []
    if len(values) != 1:
        return 0
    try:
        declared = int(values[0])
    except (TypeError, ValueError):
        return 0
    if declared <= 0:
        return 0
    consumed = max(0, reader.count - start)
    return max(0, declared - consumed)


def _drain_body(handler):
    remaining = _remaining_body(handler)
    while remaining:
        chunk = handler.rfile.read(min(65536, remaining))
        if not chunk:
            break
        remaining -= len(chunk)


def install_http_hardening():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_setup = BaseHTTPRequestHandler.setup
    original_parse_request = BaseHTTPRequestHandler.parse_request
    original_finish = BaseHTTPRequestHandler.finish

    def setup(handler):
        original_setup(handler)
        if _is_workbench(handler) and not isinstance(handler.rfile, _CountingReader):
            handler.rfile = _CountingReader(handler.rfile)

    def parse_request(handler):
        accepted = original_parse_request(handler)
        if not accepted:
            return False
        if not _is_workbench(handler):
            return True
        if isinstance(handler.rfile, _CountingReader):
            handler._reviewtrail_body_start = handler.rfile.count
        if _host_allowed(handler):
            return True
        handler.send_error(421, 'Host does not match PUBLIC_BASE_URL')
        handler.close_connection = True
        return False

    def finish(handler):
        if _is_workbench(handler):
            try:
                # Put the response on the wire before waiting for any part of an
                # already-announced request body that the application rejected.
                if not handler.wfile.closed:
                    handler.wfile.flush()
                _drain_body(handler)
            except (OSError, ValueError):
                handler.close_connection = True
        original_finish(handler)

    BaseHTTPRequestHandler.setup = setup
    BaseHTTPRequestHandler.parse_request = parse_request
    BaseHTTPRequestHandler.finish = finish

'''Regression tests for public-demo error privacy and request deadlines.'''
import http.client
import io
import socket
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from demo import (
    CappedThreadingHTTPServer,
    DemoHandler,
    DemoState,
    MAX_CONCURRENCY,
    REQUEST_DEADLINE,
)


class ErrorPrivacyTest(unittest.TestCase):
    def test_uncaught_handler_errors_do_not_log_client_or_exception(self):
        # Exercise socketserver's actual exception path, including its finally
        # block, without relying on OS-specific TCP reset timing.
        server = CappedThreadingHTTPServer(
            ('127.0.0.1', 0), DemoHandler, max_concurrency=1,
        )
        self.addCleanup(server.server_close)
        for error in (ConnectionResetError('private reset detail'),
                      OSError('private read detail'),
                      RuntimeError('private application detail')):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(server._request_slots.acquire(blocking=False))
                stderr = io.StringIO()
                request = Mock()
                with redirect_stderr(stderr), \
                     patch.object(server, 'finish_request', side_effect=error), \
                     patch.object(server, 'shutdown_request') as shutdown:
                    server.process_request_thread(request, ('203.0.113.123', 45678))
                self.assertEqual(stderr.getvalue(), '')
                shutdown.assert_called_once_with(request)
                self.assertTrue(server._request_slots.acquire(blocking=False))
                server._request_slots.release()

    def test_send_catches_oserror_during_headers_and_body(self):
        for stage in ('send_response', 'send_header', 'end_headers', 'body'):
            for error_class in (BrokenPipeError, ConnectionResetError, OSError):
                with self.subTest(stage=stage, error=error_class.__name__):
                    handler = object.__new__(DemoHandler)
                    handler.command = 'GET'
                    handler.close_connection = False
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock()
                    handler.wfile = Mock()
                    failing = handler.wfile.write if stage == 'body' else getattr(handler, stage)
                    failing.side_effect = error_class('private transport detail')
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        handler._send(200, 'hello')
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(stderr.getvalue(), '')
                    if stage != 'body':
                        handler.wfile.write.assert_not_called()


class DeadlineTest(unittest.TestCase):
    def start_server(self, handler_class, concurrency=1):
        server = CappedThreadingHTTPServer(
            ('127.0.0.1', 0), handler_class, max_concurrency=concurrency,
        )
        server.state = DemoState()
        server.trusted_proxy_header = ''
        server.trusted_proxy_hops = 1
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True,
        )
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.addCleanup(cleanup)
        return server

    def health(self, server):
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.request('GET', '/healthz')
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b'ok'))
        finally:
            connection.close()

    def wait_for_slots(self, server, count, timeout=2):
        acquired = 0
        until = time.monotonic() + timeout
        try:
            while acquired < count and time.monotonic() < until:
                if server._request_slots.acquire(timeout=0.02):
                    acquired += 1
            self.assertEqual(acquired, count, 'request deadline did not release every slot')
        finally:
            for _ in range(acquired):
                server._request_slots.release()

    def test_deadline_cuts_off_trickling_request_line_headers_and_bodies(self):
        self.assertEqual(REQUEST_DEADLINE, 10)
        phases = {
            'request line': b'GET /',
            'headers': b'GET /healthz HTTP/1.1\r\nHost: demo\r\nX-Slow: ',
            'review body': b'POST /review HTTP/1.1\r\nHost: demo\r\nContent-Length: 8192\r\n\r\nx=',
            'waitlist body': b'POST /waitlist/vendor HTTP/1.1\r\nHost: demo\r\nContent-Length: 8192\r\n\r\nx=',
        }
        for phase, prefix in phases.items():
            with self.subTest(phase=phase):
                entered = threading.Event()

                class ShortDeadlineHandler(DemoHandler):
                    request_deadline = 0.25
                    timeout = 5

                    def handle(self):
                        entered.set()
                        super().handle()

                server = self.start_server(ShortDeadlineHandler)
                client = socket.create_connection(server.server_address, timeout=2)
                stop = threading.Event()
                sends = []

                def drip():
                    while not stop.wait(0.02):
                        try:
                            client.sendall(b'a')
                            sends.append(time.monotonic())
                        except OSError:
                            return

                writer = threading.Thread(target=drip, daemon=True)
                try:
                    client.sendall(prefix)
                    self.assertTrue(entered.wait(2))
                    writer.start()
                    # This must finish well before the five-second inactivity
                    # timeout despite bytes arriving every twenty milliseconds.
                    self.wait_for_slots(server, 1)
                    self.assertGreaterEqual(len(sends), 2)
                    self.assertEqual(len(server.state.reviews), 3)
                    self.assertEqual(len(server.state.waitlist), 0)
                    self.health(server)
                finally:
                    stop.set()
                    if writer.ident is not None:
                        writer.join(timeout=2)
                    client.close()

    def test_all_twenty_slots_recover_from_incomplete_requests(self):
        entered = threading.Condition()
        count = [0]

        class ShortDeadlineHandler(DemoHandler):
            request_deadline = 0.5
            timeout = 5

            def handle(self):
                with entered:
                    count[0] += 1
                    entered.notify_all()
                super().handle()

        server = self.start_server(ShortDeadlineHandler, MAX_CONCURRENCY)
        clients = []
        stop = threading.Event()
        writer = None
        try:
            for _ in range(MAX_CONCURRENCY):
                client = socket.create_connection(server.server_address, timeout=2)
                clients.append(client)
                client.sendall(b'GET /healthz HTTP/1.1\r\nX-Slow: ')
            with entered:
                self.assertTrue(entered.wait_for(lambda: count[0] == MAX_CONCURRENCY, timeout=2))

            def drip():
                while not stop.wait(0.02):
                    for client in clients:
                        try:
                            client.sendall(b'a')
                        except OSError:
                            pass

            writer = threading.Thread(target=drip, daemon=True)
            writer.start()
            self.wait_for_slots(server, MAX_CONCURRENCY)
            self.health(server)
        finally:
            stop.set()
            if writer is not None:
                writer.join(timeout=2)
            for client in clients:
                client.close()

    def test_deadline_is_shared_across_header_and_body_reads(self):
        # A deterministic timer checks that advancing to the body does not
        # allocate a new deadline or reset the existing one.
        handler = object.__new__(DemoHandler)
        handler.connection = Mock()
        handler.close_connection = False
        timer = Mock()

        def receive_request():
            timer.start.assert_called_once_with()
            handler._expire_request()

        with patch('demo.threading.Timer', return_value=timer) as make_timer, \
             patch('http.server.BaseHTTPRequestHandler.handle', side_effect=receive_request):
            handler.handle()
        make_timer.assert_called_once_with(REQUEST_DEADLINE, handler._expire_request)
        handler.connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        timer.cancel.assert_called_once_with()
        timer.join.assert_called_once_with()

    def test_completed_request_cancels_and_joins_deadline(self):
        handler = object.__new__(DemoHandler)
        handler.connection = Mock()
        timer = Mock()
        with patch('demo.threading.Timer', return_value=timer), \
             patch('http.server.BaseHTTPRequestHandler.handle'):
            handler.handle()
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        timer.join.assert_called_once_with()
        handler.connection.shutdown.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import http.client
import tempfile
import threading
import urllib.error
import urllib.request
import unittest

from app import MAX_BODY, create_server


class HostingContainerSmokeRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server('127.0.0.1', 0, self.temp.name)
        self.port = self.server.server_address[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_oversized_body_returns_413_instead_of_transport_urlerror(self):
        request = urllib.request.Request(
            self.base + '/w',
            data=b'x' * (MAX_BODY + 1),
            method='POST',
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': self.base,
                'Referer': self.base + '/',
            },
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 413)
            self.assertIn(b'Request body is too large', error.read())
        except urllib.error.URLError as error:
            self.fail(f'oversized request lost its HTTP response: {error}')
        else:
            self.fail('oversized request unexpectedly succeeded')

    def test_wrong_host_is_rejected_but_health_probe_host_is_allowed(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request('GET', '/', headers={'Host': 'wrong.example.test'})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 421)
        connection.close()

        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request('GET', '/healthz', headers={'Host': '10.0.0.4'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'ok')
        connection.close()

    def test_normal_public_host_still_serves_index(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request('GET', '/', headers={'Host': f'127.0.0.1:{self.port}'})
        response = connection.getresponse()
        page = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b'Operated by AI agents', page)
        self.assertIn(b'https://manykiln.com/privacy', page)
        connection.close()


if __name__ == '__main__':
    unittest.main()

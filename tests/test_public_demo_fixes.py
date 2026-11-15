import io
import os
import time
import unittest
from datetime import date
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

import demo
from reviewtrail.reviews import has_sensitive, validate


class ReviewScreenBoundsTest(unittest.TestCase):
    def form(self):
        return {
            'model_slug': 'm',
            'model_version': 'release-1',
            'task_category': 'coding',
            'task_description': 'A repeatable task description long enough for validation.',
            'stack': 'python/sqlite',
            'harness': 'cli-agent',
            'outcome': '5',
            'retries': '0',
            'cost_usd': '1.25',
            'used_on': '2026-10-24',
            'proof_line': 'local run',
        }

    def test_64_kb_adversarial_fields_are_rejected_without_screening_them(self):
        payload = 'a-' * (32 * 1024)
        for field in ('model_version', 'task_description', 'stack', 'harness', 'harness_other', 'proof_line'):
            with self.subTest(field=field):
                form = self.form()
                form[field] = payload
                error_field = 'harness' if field == 'harness_other' else field
                if field == 'harness_other':
                    form['harness'] = 'other'

                def bounded_screen(value, phone=True):
                    # Deterministic regression guard, independent of CPU speed.
                    self.assertLessEqual(len(value), 1000)
                    return has_sensitive(value, phone=phone)

                with patch('reviewtrail.reviews.has_sensitive', side_effect=bounded_screen):
                    started = time.perf_counter()
                    review, errors = validate(form, date(2026, 10, 24), {'m'})
                    elapsed = time.perf_counter() - started
                self.assertIsNone(review)
                self.assertIn(error_field, errors)
                self.assertLess(elapsed, 1.0)

    def test_real_screen_timing_for_64_kb_attack(self):
        # The preceding deterministic test identifies accidental unbounded calls;
        # this exercises the actual validator and PII screen without mocks.
        form = self.form()
        payload = 'a-' * (32 * 1024)
        for field in ('model_version', 'task_description', 'stack', 'harness', 'proof_line'):
            form[field] = payload
        started = time.perf_counter()
        review, errors = validate(form, date(2026, 10, 24), {'m'})
        elapsed = time.perf_counter() - started
        self.assertIsNone(review)
        self.assertEqual(set(errors), {'model_version', 'task_description', 'stack', 'harness', 'proof_line'})
        self.assertLess(elapsed, 1.0)

    def test_length_errors_are_not_overwritten_by_sensitive_content(self):
        cases = {
            'task_description': ('person@example.test ' * 100, 'Use 40–1,000 characters.'),
            'stack': ('person@example.test ' * 10, 'Use 2–80 characters.'),
            'proof_line': ('person@example.test ' * 30, 'Use at most 300 characters.'),
            'model_version': ('person@example.test' * 10, 'Use 3–80 version characters.'),
        }
        for field, (value, expected) in cases.items():
            with self.subTest(field=field):
                form = self.form()
                form[field] = value
                review, errors = validate(form, date(2026, 10, 24), {'m'})
                self.assertIsNone(review)
                self.assertEqual(errors[field], expected)

    def test_bounded_fields_still_reject_sensitive_content(self):
        cases = {
            'model_version': 'person@example.test',
            'task_description': 'Repeat this parser validation task and contact person@example.test.',
            'stack': 'person@example.test',
            'harness_other': 'person@example.test',
            'proof_line': 'person@example.test',
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                form = self.form()
                form[field] = value
                if field == 'harness_other':
                    form['harness'] = 'other'
                review, errors = validate(form, date(2026, 10, 24), {'m'})
                self.assertIsNone(review)
                self.assertIn('Remove personal data', errors['harness' if field == 'harness_other' else field])

    def test_valid_maximum_length_fields_remain_accepted(self):
        form = self.form()
        form.update(
            model_version='v1' + '-a' * 39,
            task_description='a-' * 500,
            stack='a-' * 40,
            harness='other',
            harness_other='a-' * 20,
            proof_line='a-' * 150,
        )
        review, errors = validate(form, date(2026, 10, 24), {'m'})
        self.assertEqual(errors, {})
        self.assertIsNotNone(review)


class DemoRequestBoundsTest(unittest.TestCase):
    def test_oversized_body_is_rejected_before_read_or_parse(self):
        handler = object.__new__(demo.DemoHandler)
        for length in (8193, 65536):
            with self.subTest(length=length):
                handler.headers = Message()
                handler.headers['Content-Length'] = str(length)
                handler.rfile = Mock()
                with self.assertRaises(demo.RequestError) as raised:
                    handler._read_form()
                self.assertEqual(raised.exception.status, 413)
                handler.rfile.read.assert_not_called()

    def test_body_at_8_kb_limit_is_accepted(self):
        handler = object.__new__(demo.DemoHandler)
        body = b'x=' + b'a' * (8192 - 2)
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        self.assertEqual(handler._read_form(), {'x': 'a' * (8192 - 2)})


class DemoProxyConfigurationTest(unittest.TestCase):
    def handler(self, values, hops=1):
        handler = object.__new__(demo.DemoHandler)
        handler.server = SimpleNamespace(trusted_proxy_header='X-Forwarded-For', trusted_proxy_hops=hops)
        handler.client_address = ('10.0.0.2', 1234)
        handler.headers = Message()
        for value in values:
            handler.headers['X-Forwarded-For'] = value
        return handler

    def test_duplicate_proxy_headers_are_rejected(self):
        handler = self.handler(['203.0.113.1', '203.0.113.2'])
        with self.assertRaises(demo.RequestError) as raised:
            handler.client_ip()
        self.assertEqual(raised.exception.status, 400)

    def test_fixed_hops_preserve_empty_positions(self):
        handler = self.handler(['forged, , 10.0.0.1'], hops=2)
        with self.assertRaises(demo.RequestError):
            handler.client_ip()

    def test_ip_spellings_cannot_create_extra_buckets(self):
        self.assertEqual(self.handler(['2001:0db8:0000:0000:0000:0000:0000:0001']).client_ip(), '2001:db8::1')
        self.assertEqual(self.handler(['::ffff:203.0.113.10']).client_ip(), '203.0.113.10')
        with self.assertRaises(demo.RequestError):
            self.handler(['fe80::1%arbitrary']).client_ip()

    def test_invalid_configuration_fails_before_socket_creation(self):
        with patch('demo.CappedThreadingHTTPServer') as server:
            for header, hops in (('X-Forwarded-For', 0), ('X-Forwarded-For', -1), ('Bad\r\nHeader', 1), ('X-Forwarded-For', True)):
                with self.subTest(header=header, hops=hops):
                    with self.assertRaises(ValueError):
                        demo.create_server(trusted_proxy_header=header, trusted_proxy_hops=hops)
            server.assert_not_called()

    def test_main_requires_explicit_header_and_hop_pair(self):
        environments = (
            {'TRUSTED_PROXY_HEADER': 'X-Forwarded-For'},
            {'TRUSTED_PROXY_HOPS': '2'},
            {'TRUSTED_PROXY_HEADER': 'X-Forwarded-For', 'TRUSTED_PROXY_HOPS': '0'},
            {'TRUSTED_PROXY_HEADER': 'X-Forwarded-For', 'TRUSTED_PROXY_HOPS': 'invalid'},
        )
        for environment in environments:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), patch('demo.create_server') as create:
                with self.assertRaises(SystemExit):
                    demo.main()
                create.assert_not_called()

    def test_main_passes_normalized_proxy_settings_to_server(self):
        environment = {'PORT': '8081', 'TRUSTED_PROXY_HEADER': 'HTTP_X_FORWARDED_FOR', 'TRUSTED_PROXY_HOPS': '2'}
        with patch.dict(os.environ, environment, clear=True), patch('demo.create_server') as create, patch('builtins.print'):
            demo.main()
        create.assert_called_once_with('0.0.0.0', 8081, trusted_proxy_header='X-FORWARDED-FOR', trusted_proxy_hops=2)
        create.return_value.serve_forever.assert_called_once_with()
        create.return_value.server_close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

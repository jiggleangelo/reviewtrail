'''Behavior and adversarial-cost regressions for the review PII email screen.'''
import itertools
import re
import time
import unittest
from datetime import date

from reviewtrail.reviews import _looks_like_email, has_sensitive, validate


class EmailScreeningTest(unittest.TestCase):
    def test_email_detection_preserves_existing_boundary_semantics(self):
        legacy = re.compile(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b', re.I)
        # Exhaustively cover short combinations of word characters, punctuation,
        # separators and multiple @ signs. Only short inputs reach the old regex.
        for length in range(7):
            for characters in itertools.product('a.@ ', repeat=length):
                value = ''.join(characters)
                self.assertEqual(_looks_like_email(value), bool(legacy.search(value)), repr(value))
        for value in (
            'Contact alice+tag@example.test, please.',
            '(élise@例子.测试)',
            '_@_.__',
            'a@.b',
            'a@..b',
            'a@b..',
            '...@example.test',
            'a@@example.test',
            'a@b c.d',
            'a@b.c@d.e',
            'name\t@example.test',
            'name@example.\ncom',
            '!a!@!..!z!',
        ):
            with self.subTest(value=value):
                self.assertEqual(_looks_like_email(value), bool(legacy.search(value)))

    def test_adversarial_email_screen_has_bounded_cost_at_field_limit(self):
        # Sweep the local-part/domain split of the reported 1,000-character
        # worst-case family, with many candidate boundaries and trailing dots.
        started = time.perf_counter()
        for pairs in range(1, 500, 7):
            value = 'a.' * pairs + '@' + '.' * (999 - 2 * pairs)
            self.assertEqual(len(value), 1000)
            self.assertFalse(has_sensitive(value))
            # A word after the dots makes the permissive screen match again.
            self.assertTrue(has_sensitive(value[:-1] + 'z'))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_adversarial_description_passes_validation_without_losing_pii_rejection(self):
        today = date(2026, 10, 24)
        form = {
            'model_slug': 'claude-code',
            'model_version': 'release-1',
            'task_category': 'coding',
            'task_description': 'a.' * 166 + '@' + '.' * 667,
            'stack': 'python/sqlite',
            'harness': 'cli-agent',
            'outcome': '4',
            'retries': '1',
            'used_on': today.isoformat(),
        }
        review, errors = validate(form, today, {'claude-code'})
        self.assertEqual(errors, {})
        self.assertIsNotNone(review)
        for field, value in (
            ('model_version', 'alice@example.test'),
            ('task_description', 'Repeatable task: contact alice@example.test and inspect the parser output.'),
            ('stack', 'alice@example.test'),
            ('proof_line', 'alice@example.test'),
        ):
            with self.subTest(field=field):
                review, errors = validate(dict(form, **{field: value}), today, {'claude-code'})
                self.assertIsNone(review)
                self.assertIn('Remove personal data', errors[field])
        review, errors = validate(
            dict(form, harness='other', harness_other='alice@example.test'),
            today,
            {'claude-code'},
        )
        self.assertIsNone(review)
        self.assertIn('Remove personal data', errors['harness'])


if __name__ == '__main__':
    unittest.main()

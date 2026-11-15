import tempfile,unittest
from datetime import date,datetime,timezone
from reviewtrail.antifraud import account_old_enough,IPBucket,review_error
from reviewtrail.db import init,connect

class R:model_slug='m';model_version='v1';iso_week='2026-W43';content_hash='h'
class FraudTest(unittest.TestCase):
    def test_age(self):
        self.assertFalse(account_old_enough('2026-08',date(2026,10,29)));self.assertTrue(account_old_enough('2026-07',date(2026,10,29)))
    def test_bucket_refill(self):
        t=[0];b=IPBucket('s',lambda:t[0]);self.assertTrue(all(b.allow('1.2.3.4') for _ in range(30)));self.assertFalse(b.allow('1.2.3.4'));t[0]=120;self.assertTrue(b.allow('1.2.3.4'))

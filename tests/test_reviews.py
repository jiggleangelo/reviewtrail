import unittest
from datetime import date,timedelta
from reviewtrail.reviews import validate,specific,iso_week

class ReviewsTest(unittest.TestCase):
    def form(self):return {'model_slug':'m','model_version':'x-4.5-2026-09','task_category':'coding','task_description':'A repeatable task description long enough for validation.','stack':'python/sqlite','harness':'cli-agent','outcome':'5','retries':'0','cost_usd':'1.25','used_on':'2026-10-24','proof_line':'local run'}
    def test_valid(self):
        r,e=validate(self.form(),date(2026,10,24),{'m'});self.assertFalse(e);self.assertEqual(r.cost_usd_cents,125)
    def test_dates_and_pii(self):
        for used in ('2026-10-25','2026-07-25'):
            f=self.form();f['used_on']=used;self.assertIn('used_on',validate(f,date(2026,10,24),{'m'})[1])
        f=self.form();f['task_description']='Contact somebody@example.com while completing this task.';self.assertIn('task_description',validate(f,date(2026,10,24),{'m'})[1])
        f=self.form();f['proof_line']='sk-supersecretvalue';self.assertIn('proof_line',validate(f,date(2026,10,24),{'m'})[1])
    def test_version_and_week(self):
        self.assertFalse(specific('latest'));self.assertTrue(specific('x-4.5-2026-09'));self.assertEqual(iso_week(date(2026,12,31)),'2026-W53')

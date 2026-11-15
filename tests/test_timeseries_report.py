import tempfile,unittest
from reviewtrail.db import init,connect
from reviewtrail.timeseries import aggregate,render_svg
from reviewtrail.report import generate

class SeriesTest(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.NamedTemporaryFile();init(self.t.name);self.db=connect(self.t.name);self.db.execute("INSERT INTO models VALUES('m','M','V','model',NULL)")
        for i,outcome in enumerate((5,4,3)):
            self.db.execute("INSERT INTO reviews VALUES(NULL,NULL,'m','v1','coding',?,'py','api-direct',?,0,NULL,'2026-10-20','2026-W43',NULL,'founder_seed','published',?,?)",('secret review text '+str(i),outcome,'h'+str(i),'2026-10-20T00:00:00+00:00'))
        self.db.commit()
    def test_aggregate_svg_report(self):
        rows=aggregate(self.db,'m');self.assertEqual(rows[0]['n'],3);self.assertEqual(render_svg(rows),render_svg(rows));report=generate(self.db,'m');self.assertIn('n | Outcome',report);self.assertIn('not a benchmark',report);self.assertNotIn('secret review text',report)
    def test_suppression_hidden(self):
        self.db.execute("UPDATE reviews SET status='hidden' WHERE id>1");self.db.commit();self.assertTrue(aggregate(self.db,'m')[0]['suppressed'])

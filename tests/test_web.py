import io,tempfile,unittest
from reviewtrail.auth import FakeOAuth
from reviewtrail.config import Config
from reviewtrail.db import init,connect
from reviewtrail.directory import seed_models
from reviewtrail.vault import MemoryVault
from reviewtrail.web import App

class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();path=self.tmp.name+'/db';init(path);db=connect(path);seed_models(db,'seed/models.json');db.commit();self.c=Config('secret',path,self.tmp.name,'fake','','','dpo@example.test','test',True,False,365);self.v=MemoryVault();self.app=App(self.c,self.v,FakeOAuth())
    def call(self,path='/',method='GET',body='',cookie=''):
        result={};env={'REQUEST_METHOD':method,'PATH_INFO':path,'QUERY_STRING':'','wsgi.input':io.BytesIO(body.encode()),'CONTENT_LENGTH':str(len(body)),'REMOTE_ADDR':'127.0.0.1','HTTP_COOKIE':cookie}
        data=b''.join(self.app(env,lambda s,h:result.update(status=s,headers=dict(h))));return result,data
    def test_directory_headers(self):
        r,b=self.call('/models');self.assertIn(b'Claude Code',b);self.assertIn('Content-Security-Policy',r['headers'])
    def test_csrf(self):self.assertEqual(self.call('/signup','POST','email=a%40b.test')[0]['status'][:3],'403')
    def test_prod_gate(self):
        with self.assertRaises(RuntimeError):Config('s','d','v','fake','','','','prod',False,True,365).check()

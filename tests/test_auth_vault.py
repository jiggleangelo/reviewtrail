import os,tempfile,unittest
from datetime import datetime,timezone
from reviewtrail.auth import FakeOAuth,pack,unpack,session
from reviewtrail.vault import FileVault,Vault

class AuthVaultTest(unittest.TestCase):
    def test_fake_and_session(self):
        self.assertEqual(FakeOAuth().exchange('alice-old','x').login,'alice');token=session('s',1);self.assertEqual(unpack('s',token)['uid'],1);self.assertIsNone(unpack('x',token));expired=pack('s',{'exp':0});self.assertIsNone(unpack('s',expired))
    def test_vault(self):
        with tempfile.TemporaryDirectory() as d:
            p=d+'/v';v=FileVault(p,'key');token=v.put('reader','a@example.test');self.assertNotIn('a@example.test',token);self.assertEqual(os.stat(p).st_mode&0o777,0o600);self.assertTrue(v.forget(token));self.assertFalse(hasattr(Vault,'get'))

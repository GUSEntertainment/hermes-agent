import concurrent.futures
import hashlib
import pathlib
import tempfile
import types
import unittest
from plugins.platforms.photon.josh_onboarding.transport_receipts import TransportReceipts, RequestRegistry, Denied
from plugins.platforms.photon.secure_intake.trusted_intake import capture_event

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=pathlib.Path(self.tmp.name).resolve()/'registry.sqlite';self.path.touch(mode=0o600)
        self.registry=RequestRegistry(self.path);self.enabled=True
        self.auth={'principal':'joshgarcia','profile':'joshgarcia','endpoint':'http://127.0.0.1:8642/p/joshgarcia/v1/chat/completions','digest':hashlib.sha256(b'new request').hexdigest(),'session':'native-session','run':'native-run'}
        self.receipts=TransportReceipts(self.registry,enrollment=self.enrollment,verifier=lambda:self.auth.copy())
        self.capture=self.capture_for('sender-josh','message-one')
    def tearDown(self):self.tmp.cleanup()
    def capture_for(self,sender,message):
        event=types.SimpleNamespace(source=types.SimpleNamespace(platform=types.SimpleNamespace(value='photon'),chat_type='dm',chat_id='thread-'+sender,user_id=sender),message_id=message,internal=False)
        capture_event(event,{'type':'text','text':'new request'})
        return event._secure_intake_capture
    def enrollment(self,t):
        owner={'sender-josh':'joshgarcia','sender-connor':'connor'}[t[3]]
        return {'owner':owner,'profile':owner,'generation':'a'*64,'endpoint':f'http://127.0.0.1:8642/p/{owner}/v1/chat/completions','enabled':self.enabled,'revoked':False}
    def test_native_runs_endpoint_exact_and_not_interchangeable(self):
        original=self.enrollment
        endpoint='http://127.0.0.1:8642/p/joshgarcia/v1/runs'
        self.receipts.enrollment=lambda t:dict(original(t),endpoint=endpoint)
        self.auth['endpoint']=endpoint
        h=self.receipts.publish(self.capture)
        self.assertEqual(self.receipts.claim(h)['run'],'native-run')
        self.auth['endpoint']=endpoint.replace('/runs','/chat/completions')
        with self.assertRaises(Denied):self.receipts.claim(h)

    def test_native_runs_endpoint_rejects_normalization_and_wrong_profile(self):
        original=self.enrollment
        for endpoint in ('http://127.0.0.1:8642/p/connor/v1/runs','http://127.0.0.1:8642/p/joshgarcia/v1/runs?','http://127.0.0.1:8642/p/joshgarcia/v1/runs/','http://localhost:8642/p/joshgarcia/v1/runs'):
            self.receipts.enrollment=lambda t:dict(original(t),endpoint=endpoint)
            with self.assertRaises(Denied):self.receipts.publish(self.capture)

    def test_restart_idempotent_and_no_body_storage(self):
        h=self.receipts.publish(self.capture);a=self.receipts.claim(h)
        again=TransportReceipts(RequestRegistry(self.path),enrollment=self.enrollment,verifier=lambda:self.auth.copy())
        self.assertEqual(again.publish(self.capture),h);self.assertEqual(again.claim(h),a)
        self.assertNotIn(b'new request',self.path.read_bytes())
        self.assertEqual(a['identity']['session'],'native-session')
    def test_unsealed_and_wrong_owner_body_run(self):
        with self.assertRaises(Denied):self.receipts.publish(types.SimpleNamespace(**vars(self.capture)))
        h=self.receipts.publish(self.capture)
        for key,value in [('principal','connor'),('digest','b'*64),('endpoint','http://127.0.0.1:8642/v1/chat/completions')]:
            old=self.auth[key];self.auth[key]=value
            with self.assertRaises(Denied):self.receipts.claim(h)
            self.auth[key]=old
        self.receipts.claim(h);self.auth['run']='another-run'
        with self.assertRaises(Denied):self.receipts.claim(h)
    def test_expiry_and_revoke_and_enrollment_change(self):
        h=self.receipts.publish(self.capture);self.enabled=False
        with self.assertRaises(Denied):self.receipts.claim(h)
        self.enabled=True;self.receipts.clock=lambda:self.capture.captured+301
        with self.assertRaises(Denied):self.receipts.claim(h)
        self.receipts.clock=lambda:self.capture.captured+1;self.receipts.revoke(h)
        with self.assertRaises(Denied):self.receipts.claim(h)
        with self.assertRaises(Denied):self.receipts.publish(self.capture)
    def test_concurrent_claim_only_one_run(self):
        h=self.receipts.publish(self.capture)
        def claim(n):
            a=dict(self.auth,run=str(n));r=TransportReceipts(RequestRegistry(self.path),enrollment=self.enrollment,verifier=lambda:a)
            try:return r.claim(h)['run']
            except Denied:return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(claim,[1,2]))
        self.assertEqual(sum(v is not None for v in out),1)
    def test_concurrent_revoke_and_claim_remains_revoked(self):
        h=self.receipts.publish(self.capture)
        def invoke(which):
            r=TransportReceipts(RequestRegistry(self.path),enrollment=self.enrollment,verifier=lambda:self.auth.copy())
            try:return r.revoke(h) if which else r.claim(h)
            except Denied:return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(invoke,[True,False]))
        with self.assertRaises(Denied):self.receipts.claim(h)

    def test_source_replay_and_owner_isolation(self):
        self.receipts.publish(self.capture)
        with self.assertRaises(Denied):self.receipts.publish(self.capture_for('sender-connor','message-one'))
        h=self.receipts.publish(self.capture_for('sender-connor','message-two'))
        with self.assertRaises(Denied):self.receipts.claim(h)
    def test_replacement_after_commit_withholds_result(self):
        import os
        from contextlib import contextmanager
        handle=self.receipts.publish(self.capture)
        original=self.registry._db
        @contextmanager
        def replace_after_close():
            with original() as db:
                yield db
            replacement=self.path.with_suffix('.replacement')
            replacement.touch(mode=0o600)
            os.replace(replacement,self.path)
        self.registry._db=replace_after_close
        with self.assertRaises(Denied):self.receipts.claim(handle)

    def test_replacement_during_final_claim_lookup_denies(self):
        self._replace_during_final_lookup(claim=True)

    def test_replacement_during_final_publication_lookup_denies(self):
        self._replace_during_final_lookup(claim=False)

    def _replace_during_final_lookup(self,claim):
        import os
        handle=self.receipts.publish(self.capture) if claim else None
        original=self.enrollment;calls=0
        def replace(t):
            nonlocal calls
            calls+=1
            result=original(t)
            if calls==2:
                replacement=self.path.with_suffix('.replacement')
                replacement.touch(mode=0o600)
                os.replace(replacement,self.path)
            return result
        self.receipts.enrollment=replace
        with self.assertRaises(Denied):
            self.receipts.claim(handle) if claim else self.receipts.publish(self.capture)
        self.assertEqual(calls,2)
        self.assertEqual(self.path.stat().st_size,0)

    def test_final_authority_revalidation_rolls_back_claim(self):
        h=self.receipts.publish(self.capture);calls=0
        def verify():
            nonlocal calls
            calls+=1
            if calls==2:self.enabled=False
            return self.auth.copy()
        self.receipts.verifier=verify
        with self.assertRaises(Denied):self.receipts.claim(h)
        with self.registry._db() as db:self.assertIsNone(db.execute('SELECT session FROM transport_receipts').fetchone()[0])

if __name__=='__main__':unittest.main()

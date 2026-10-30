import asyncio
import hashlib
import pathlib
import types
import unittest
import sys
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]))
from plugins.platforms.photon.josh_onboarding.native_runs_client import NativeRunsOwnerHandler
from plugins.platforms.photon.josh_onboarding.photon_owner_handler import Route
import test_transport_receipts as fixtures
from plugins.platforms.photon.secure_intake.trusted_intake import capture_event

class NativeTests(unittest.TestCase):
    setUpFixture=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown
    enrollment=fixtures.Tests.enrollment
    capture_for=fixtures.Tests.capture_for
    def setUp(self):
        self.setUpFixture();self.calls=[]
        endpoint='http://127.0.0.1:8642/p/joshgarcia/v1/runs'
        self.receipts.enrollment=lambda t:dict(self.enrollment(t),endpoint=endpoint)
        sender='sender-josh';destination='any;-;'+sender
        self.route=Route(hashlib.sha256(sender.encode()).hexdigest(),endpoint,1,True,'fixture')
        self.event=types.SimpleNamespace(source=types.SimpleNamespace(platform=types.SimpleNamespace(value='photon'),chat_type='dm',user_id=sender,chat_id=destination),message_id='one',message_type=types.SimpleNamespace(value='text'),text='new request',internal=False)
        capture_event(self.event,{'type':'text','text':'new request'})
        self.handler=NativeRunsOwnerHandler(self.path.parent/'reply.db',lambda:self.route,lambda:'fixture-key',self.transport,enrolled_phone_digest=self.route.phone_digest,enrolled_destination=destination,receipts=self.receipts)
    def transport(self,url,headers,body,timeout):
        if body is None:self.assertNotIn('X-Hermes-Intake-Receipt',headers)
        else:self.assertIn('X-Hermes-Intake-Receipt',headers)
        self.calls.append(body)
        return {'run_id':'native-one','status':'queued'} if body else {'run_id':'native-one','status':'completed','output':'Your secure form is ready.'}
    def test_sealed_ingress_native_run_same_ledger_and_duplicate(self):
        self.assertEqual(asyncio.run(self.handler.handle(self.event))['state'],'complete')
        self.assertTrue(asyncio.run(self.handler.handle(self.event))['duplicate'])
        self.assertEqual(sum(v is not None for v in self.calls),1)
    def test_uncertain_post_never_recreated(self):
        def uncertain(*args):self.calls.append('post');raise TimeoutError()
        self.handler.transport=uncertain
        self.assertEqual(asyncio.run(self.handler.handle(self.event))['state'],'uncertain')
        self.assertEqual(asyncio.run(self.handler.handle(self.event))['state'],'uncertain')
        self.assertEqual(len(self.calls),1)
    def test_existing_reconnect_hook_delivers_delayed_run_once(self):
        from plugins.platforms.photon.josh_onboarding.adapter_hook import recover_owner_replies_after_connect
        async def scenario():
            self.handler.transport=lambda *args:{'run_id':'native-one','status':'queued'}
            self.handler.budget=.05
            await self.handler.handle(self.event)
            self.receipts.clock=lambda:self.event._secure_intake_capture.captured+600
            self.handler.transport=self.transport;self.handler.budget=1
            sent=[]
            async def send(destination,text,metadata):
                sent.append((destination,text));return types.SimpleNamespace(success=True,message_id='receipt-one')
            adapter=types.SimpleNamespace(send=send)
            await recover_owner_replies_after_connect(self.handler,adapter)
            await recover_owner_replies_after_connect(self.handler,adapter)
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0][0],self.event.source.chat_id)
        asyncio.run(scenario())

    def test_pending_restart_resumes_get_only(self):
        def pending(url,headers,body,timeout):
            self.calls.append(body)
            return {'run_id':'native-one','status':'queued'}
        self.handler.transport=pending;self.handler.budget=.03
        self.assertEqual(asyncio.run(self.handler.handle(self.event))['state'],'pending')
        self.handler=NativeRunsOwnerHandler(self.path.parent/'reply.db',lambda:self.route,lambda:'fixture-key',self.transport,enrolled_phone_digest=self.route.phone_digest,enrolled_destination=self.event.source.chat_id,receipts=self.receipts,budget=1)
        self.assertEqual(asyncio.run(self.handler.handle(self.event))['state'],'complete')
        self.assertEqual(sum(v is not None for v in self.calls),1)

if __name__=='__main__':unittest.main()

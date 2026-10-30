import asyncio, types
from test_native_runs_client import NativeTests

class RecoveryTests(NativeTests):
    def exercise(self, claimed=False, revoked=False, fresh=False, failed=False):
        async def scenario():
            posts=[];sent=[];gets=[]
            def uncertain(url,headers,body,timeout):
                posts.append(body)
                if claimed:
                    self.auth.update(endpoint=self.route.endpoint,run='native-one')
                    self.receipts.claim(headers['X-Hermes-Intake-Receipt'])
                raise TimeoutError()
            self.handler.transport=uncertain
            await self.handler.handle(self.event)
            if not fresh:self.receipts.clock=lambda:self.event._secure_intake_capture.captured+600
            if revoked:self.enabled=False
            def recover(url,headers,body,timeout):
                gets.append(body)
                return {'run_id':'native-one','status':'failed' if failed else 'completed','output':'Recovered result'}
            self.handler.transport=recover
            async def send(destination,text,metadata):
                sent.append((destination,text));return types.SimpleNamespace(success=True,message_id='recovery-receipt')
            adapter=types.SimpleNamespace(send=send)
            await self.handler.recover_pending(adapter)
            await self.handler.recover_pending(adapter)
            self.assertEqual(len(posts),1)
            self.assertTrue(all(x is None for x in gets))
            if revoked or fresh:self.assertEqual(sent,[])
            else:
                self.assertEqual(len(sent),1)
                self.assertEqual(sent[0][0],self.event.source.chat_id)
                self.assertIn('can’t confirm' if failed else 'Recovered result' if claimed else 'Nothing was done yet',sent[0][1])
        asyncio.run(scenario())
    def test_expired_no_run_failure_once(self):self.exercise()
    def test_claimed_missing_local_id(self):self.exercise(claimed=True)
    def test_revoked_blocks_delivery(self):self.exercise(revoked=True)
    def test_fresh_waits(self):self.exercise(fresh=True)
    def test_failed_run_truthful_once(self):self.exercise(claimed=True,failed=True)

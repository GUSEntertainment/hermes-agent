"""One adapter-owned GET/reply recovery task. No cron, POST, or new controller."""
import asyncio

class NativeRunLifecycle:
    def __init__(self,handler,adapter,*,minimum=.5,maximum=8):
        if not 0<minimum<=maximum<=30:raise ValueError('Bounded polling interval required')
        self.handler,self.adapter=handler,adapter
        self.minimum,self.maximum=minimum,maximum
        self.task=None
    def start(self):
        if self.task is None or self.task.done():
            self.task=asyncio.create_task(self._run(),name='photon-owner-native-return')
        return self
    async def _run(self):
        delay=self.minimum
        while True:
            try:
                # A durable cursor rotates past unavailable rows. At most one
                # native GET + one return claim per sweep; no batch starvation.
                result=await self.handler.recover_pending(self.adapter,limit=1)
                delay=self.minimum if result.get('delivered') else min(self.maximum,delay*2)
            except asyncio.CancelledError:raise
            except Exception:delay=min(self.maximum,delay*2)
            await asyncio.sleep(delay)
    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
            self.task=None


def start_for_adapter(adapter,handler):
    current=getattr(adapter,'_owner_native_run_lifecycle',None)
    if current is not None:
        if current.handler is not handler:raise RuntimeError('Stop old owner lifecycle before replacing handler')
        return current.start()
    current=NativeRunLifecycle(handler,adapter)
    adapter._owner_native_run_lifecycle=current
    return current.start()


async def stop_for_adapter(adapter):
    current=getattr(adapter,'_owner_native_run_lifecycle',None)
    if current is not None:await current.stop()

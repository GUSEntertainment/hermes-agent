"""Owner ingress wrapper; never fall through or send ambiguous fallback bubbles."""
if __package__:
    from .photon_owner_handler import Unavailable
else:
    from photon_owner_handler import Unavailable

async def dispatch_owner_event(handler, adapter, event):
    if not handler.matches(event):return False
    try:
        result=await handler.handle(event)
        if result is None:return False
        await handler.deliver(adapter,event)
    except Exception:
        # Backend/send errors are handled here; no root fallback or extra bubble.
        return True
    return True

async def recover_owner_replies_after_connect(handler,adapter,limit=25):
    """Call after successful existing adapter connect/reconnect, not a timer."""
    try:
        return await handler.recover_pending(adapter,limit=limit)
    except Exception:
        return {'examined':0,'delivered':0,'withheld':0,'unavailable':True}

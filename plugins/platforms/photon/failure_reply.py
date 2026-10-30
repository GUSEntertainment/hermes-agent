"""Persist Photon processing failures using the existing delivery machinery."""
import asyncio
from gateway import delivery_ledger as ledger

FAILURE_TEXT = (
    "I hit an error before I could deliver the result. "
    "I can’t confirm whether any actions finished, so I won’t repeat them automatically."
)

async def send_processing_failure(adapter, event, session_key, metadata=None):
    # Source is the already-admitted gateway event, never a model-selected recipient.
    metadata = dict(metadata or {})
    obligation = ledger.compute_obligation_id(
        session_key, str(getattr(event, 'message_id', '') or ''), FAILURE_TEXT)
    await asyncio.to_thread(
        ledger.record_obligation, obligation_id=obligation,
        session_key=session_key, platform='photon', chat_id=event.source.chat_id,
        thread_id=getattr(event.source, 'thread_id', None), content=FAILURE_TEXT,
        adapter_profile=getattr(adapter, '_owner_profile', None))
    metadata.update(delivery_obligation_id=obligation, logical_message_id=obligation)
    await asyncio.to_thread(ledger.mark_attempting, obligation)
    try:
        result = await adapter.send(chat_id=event.source.chat_id,
                                    content=FAILURE_TEXT, metadata=metadata)
    except BaseException:
        # An interrupted/uncertain send must never be blindly sent again.
        await asyncio.to_thread(ledger.mark_delivery_unknown, obligation,
                                'processing_failure_send_unknown')
        raise
    if getattr(result, 'success', False):
        await asyncio.to_thread(ledger.mark_delivered, obligation)
    else:
        await asyncio.to_thread(ledger.mark_failed, obligation,
                                str(getattr(result, 'error', '') or 'send_failed')[:200])
    return result

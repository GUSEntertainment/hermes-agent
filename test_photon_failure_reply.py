import types
from unittest.mock import AsyncMock,Mock
import pytest
from plugins.platforms.photon import failure_reply as f

@pytest.mark.asyncio
@pytest.mark.parametrize('owner',['chris','connor','joshgarcia','new-person'])
@pytest.mark.parametrize('outcome',['success','failed','unknown'])
async def test_owner_scoped_failure_delivery(monkeypatch,owner,outcome):
    calls={}
    for n in ['record_obligation','mark_attempting','mark_delivered','mark_failed','mark_delivery_unknown']:
        calls[n]=Mock();monkeypatch.setattr(f.ledger,n,calls[n])
    send=AsyncMock(return_value=types.SimpleNamespace(success=outcome=='success',error='send_path_degraded'))
    if outcome=='unknown':send.side_effect=TimeoutError()
    adapter=types.SimpleNamespace(send=send,_owner_profile=owner)
    event=types.SimpleNamespace(message_id='incoming-'+owner,source=types.SimpleNamespace(chat_id='private-'+owner,thread_id=None))
    if outcome=='unknown':
        with pytest.raises(TimeoutError):await f.send_processing_failure(adapter,event,'session-'+owner)
    else:await f.send_processing_failure(adapter,event,'session-'+owner)
    sent=send.call_args.kwargs;record=calls['record_obligation'].call_args.kwargs
    assert sent['chat_id']==record['chat_id']=='private-'+owner
    assert record['adapter_profile']==owner
    assert sent['metadata']['logical_message_id']==record['obligation_id']
    assert sent['content']==f.FAILURE_TEXT and '/reset' not in sent['content']
    send.assert_awaited_once()
    calls[{'success':'mark_delivered','failed':'mark_failed','unknown':'mark_delivery_unknown'}[outcome]].assert_called_once()
    if outcome=='unknown':calls['mark_failed'].assert_not_called()

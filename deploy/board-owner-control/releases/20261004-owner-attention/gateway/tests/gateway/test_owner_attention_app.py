"""Real isolated member board and receipt persistence; no provider calls."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pytest
from test_member_owner_delegation import enrolled,request,INTENT
from test_owner_attention import CONFIG,NOW,collect
from gateway import owner_attention as attention
from gateway import owner_attention_app as app
from gateway import owner_delegation as work
from hermes_cli import kanban_db as kb


def created():
    with request('newmember'):
        return json.loads(work.create(INTENT))['task_id']


def test_private_receipt_daily_replacement_resolution_and_revocation(enrolled):
    root,catalog,_=enrolled
    task=created()
    with kb.connect(work.database('newmember-home')) as conn:
        assert kb.block_task(conn,task,reason='Reconnect the existing account.',kind='needs_input')
        delivery=collect(conn)[0]
        receipt=app.persist(conn,delivery,attention._text(delivery['items']))
        assert receipt['accepted_locally']
        first=app.read('newmember');assert len(first)==1 and not first[0]['resolved']
        assert first[0]['sources'][0]['kind']=='native'
        daily={**delivery,'delivery_id':'d'*64,'created_at':NOW+86400}
        app.persist(conn,daily,'Still waiting for that same account.')
        second=app.read('newmember');assert len(second)==1
        assert second[0]['attentionId']==first[0]['attentionId']
        assert second[0]['summary']=='Still waiting for that same account.'
        conn.execute("UPDATE tasks SET status='ready',block_kind=NULL WHERE id=?",(task,));conn.commit()
        assert app.read('newmember')[0]['resolved']
        assert app.read('connor')==[]
        catalog.pop('newmember')
        with pytest.raises(PermissionError):app.read('newmember')


def test_changed_source_cannot_be_reused_or_rerouted(enrolled):
    task=created()
    with kb.connect(work.database('newmember-home')) as conn:
        kb.block_task(conn,task,reason='Existing access unavailable.',kind='needs_input')
        delivery=collect(conn)[0];app.persist(conn,delivery,'Reconnect the account.')
        conn.execute("UPDATE kanban_notify_subs SET chat_id='foreign' WHERE task_id=?",(task,));conn.commit()
        assert app.read('newmember')==[]
        with pytest.raises(PermissionError):app.persist(conn,delivery,'Wrong place')


def test_not_completion_or_fake_handoff(enrolled):
    task=created()
    with kb.connect(work.database('newmember-home')) as conn:
        kb.block_task(conn,task,reason='Decision needed.',kind='needs_input')
        delivery=collect(conn)[0]
        conn.execute('DELETE FROM task_return_intents WHERE task_id=?',(task,));conn.commit()
        with pytest.raises((PermissionError,ValueError)):app.persist(conn,delivery,'No native source')
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='completed'",(task,)).fetchone()

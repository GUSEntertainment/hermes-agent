"""No network/model calls: real temp board and recording transport boundary."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from hermes_cli import kanban_db as kb
from gateway import owner_attention as attention

CONFIG={'kanban':{'owner_attention':{'enabled':True,'default_timezone':'America/Toronto'}}}
NOW=int(datetime(2026,10,5,10,tzinfo=ZoneInfo('America/Toronto')).timestamp())

@pytest.fixture
def board(tmp_path):
    conn=kb.connect(tmp_path/'kanban.db')
    yield conn
    conn.close()


def hold(conn, *, chat='owner-chat', requester='chris', explicit=True, work_order=False, kind='needs_input'):
    tid=kb.create_task(conn,title='Login for existing task',assignee='riley',initial_status='blocked',session_id=f'agent:main:telegram:dm:{chat}')
    conn.execute('UPDATE tasks SET block_kind=?,current_run_id=NULL WHERE id=?',(kind,tid))
    conn.commit()
    if work_order:
        kb._append_event(conn,tid,'owner_work_requested',{'actor':'default' if requester=='chris' else requester,'version':1})
    kb._append_event(conn,tid,'blocked',{'kind':kind,'reason':'Please reconnect the authorized account.','owner_board_hold':explicit,'owner_attention_requested':explicit,'requester':requester})
    kb.add_notify_sub(conn,task_id=tid,platform='telegram',chat_id=chat,notifier_profile='default',delivery_mode='notify+wake',chat_type='dm')
    return tid


def subs(conn):return kb.list_notify_subs(conn,include_internal_metadata=True)
def collect(conn,when=NOW):return attention.collect(conn,subs(conn),CONFIG,now=when)

@pytest.fixture
def sender(monkeypatch):
    sent=[]
    async def send(adapter,chat,metadata,**kwargs):
        sent.append((chat,kwargs['text']))
        return SimpleNamespace(success=True,message_id=str(len(sent)))
    monkeypatch.setattr(attention,'send_once',send)
    import gateway.mirror
    monkeypatch.setattr(gateway.mirror,'find_owner_dm_session',lambda *a,**k:None)
    return sent,SimpleNamespace(_bot=object())


def test_grouped_daily_no_model_wake_and_preserves_board(board,sender):
    a=hold(board);b=hold(board)
    snapshot=[tuple(r) for r in board.execute('SELECT * FROM tasks')]
    # 2026-10-05: never an aggregate digest; one ask per genuine decision, once.
    due=collect(board);assert len(due)==1 and len(due[0]['items'])==1
    sent,adapter=sender
    assert asyncio.run(attention.deliver(board,due[0],adapter,CONFIG,now=NOW))
    assert len(sent)==1 and a not in sent[0][1] and b not in sent[0][1]
    assert collect(board,NOW+86399)==[]
    assert len(collect(board,NOW+86400))==1
    assert asyncio.run(attention.deliver(board,collect(board,NOW+86400)[0],adapter,CONFIG,now=NOW+86400))
    assert collect(board,NOW+3*86400)==[]
    assert snapshot==[tuple(r) for r in board.execute('SELECT * FROM tasks')]
    row=board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(a,)).fetchone()
    import json
    assert attention.owns_event(board,subs(board)[0],SimpleNamespace(kind='blocked',id=row['id'],payload=json.loads(row['payload'])),CONFIG)


def test_no_legacy_optin_and_disabled_service(board):
    hold(board,explicit=False)
    assert collect(board)==[]
    hold(board)
    assert attention.collect(board,subs(board),{},now=NOW)==[]
    assert attention.collect(board,subs(board),{'kanban':{'owner_attention':{'enabled':False}}},now=NOW)==[]


def test_future_native_worker_block_after_trusted_work_request(board):
    hold(board,explicit=False,work_order=True)
    assert len(collect(board))==1


def test_quiet_hours_and_unknown_timezone_fail_closed(board):
    hold(board)
    night=NOW+12*3600
    assert collect(board,night)==[]
    assert attention.collect(board,subs(board),{'kanban':{'owner_attention':{'enabled':True}}},now=NOW)==[]


@pytest.mark.parametrize('change',['resolved','revoked','rerouted','new_reason','new_status','active','member_without_contract'])
def test_revalidate_before_send_rejects_stale_or_foreign(board,sender,change):
    tid=hold(board);due=collect(board)[0]
    if change=='resolved':board.execute("UPDATE tasks SET status='ready' WHERE id=?",(tid,))
    elif change=='revoked':board.execute('DELETE FROM kanban_notify_subs WHERE task_id=?',(tid,))
    elif change=='rerouted':board.execute("UPDATE kanban_notify_subs SET chat_id='other' WHERE task_id=?",(tid,))
    elif change=='active':board.execute("UPDATE tasks SET claim_lock='busy' WHERE id=?",(tid,))
    elif change=='new_status':kb._append_event(board,tid,'triaged',{'reason':'Different current stop'})
    elif change=='new_reason':kb._append_event(board,tid,'blocked',{'kind':'needs_input','owner_board_hold':True,'owner_attention_requested':True,'requester':'chris','reason':'A different decision is needed'})
    elif change=='member_without_contract':
        import json
        row=board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(tid,)).fetchone();p=json.loads(row['payload']);p['requester']='connor';board.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps(p),row['id']))
    board.commit()
    sent,adapter=sender
    assert not asyncio.run(attention.deliver(board,due,adapter,CONFIG,now=NOW))
    assert sent==[]


def test_uncertain_receipt_no_same_day_retry_but_distinct_next_day_reminder(board,sender,monkeypatch):
    tid=hold(board);due=collect(board)[0];sent,adapter=sender
    async def timeout(*a,**k):sent.append('attempt');raise TimeoutError()
    monkeypatch.setattr(attention,'send_once',timeout)
    assert not asyncio.run(attention.deliver(board,due,adapter,CONFIG,now=NOW))
    assert collect(board,NOW+86399)==[] and sent==['attempt']
    # The same unchanged ask is never repeated (no daily nag).
    assert collect(board,NOW+86400)==[]
    assert board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='owner_attention_delivery_held'",(tid,)).fetchone()


def test_sources_never_consolidate_across_conversations(board):
    hold(board,chat='one');hold(board,chat='two')
    assert len(collect(board))==2


def test_inherited_sub_cannot_receive_owners_blocker(board):
    tid=hold(board)
    kb.add_notify_sub(board,task_id=tid,platform='telegram',chat_id='other',notifier_profile='default',delivery_metadata={'_kanban_inherited_subscription':True,'_kanban_subscription_origin_task_id':'parent'})
    due=collect(board)
    assert len(due)==1 and due[0]['sub']['chat_id']=='owner-chat'

from tests.gateway.test_member_owner_delegation import enrolled, request, INTENT
from gateway import owner_delegation as work


def test_new_member_native_contract_optin_and_fresh_enrollment_revoke(enrolled):
    import json
    root,catalog,_env=enrolled
    with request('newmember'):
        created=json.loads(work.create(INTENT))
    with kb.connect(work.database(catalog['newmember']['board'])) as conn:
        assert kb.block_task(conn,created['task_id'],reason='Please reconnect your account before this can continue.',kind='needs_input')
        due=collect(conn)
        assert len(due)==1 and due[0]['requester']=='newmember'
        assert due[0]['sub']['platform']=='api_server'
        assert due[0]['sub']['user_id']=='newmember'
        catalog.pop('newmember')
        assert collect(conn)==[]


def test_passive_notice_is_mirrored_to_same_conversation(board,sender,monkeypatch):
    import gateway.mirror
    mirrored=[]
    monkeypatch.setattr(gateway.mirror,'find_owner_dm_session',lambda p,c,t:'original-session')
    monkeypatch.setattr(gateway.mirror,'mirror_proactive_message',lambda *a,**k:mirrored.append((a,k)))
    hold(board)
    sent,adapter=sender
    assert asyncio.run(attention.deliver(board,collect(board)[0],adapter,CONFIG,now=NOW))
    assert len(mirrored)==1 and mirrored[0][0][1]=='owner-chat'
    assert mirrored[0][1]['session_id']=='original-session'


def test_two_unseen_block_events_do_not_reach_legacy_model_wake(board):
    import json
    tid=hold(board)
    kb._append_event(board,tid,'blocked',{'kind':'needs_input','reason':'Latest question','owner_board_hold':True,'owner_attention_requested':True,'requester':'chris'})
    events=[SimpleNamespace(kind=r['kind'],id=r['id'],payload=json.loads(r['payload'])) for r in board.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind='blocked'",(tid,))]
    assert len(events)==2
    assert all(attention.owns_event(board,subs(board)[0],ev,CONFIG) for ev in events)
    assert collect(board)[0]['items'][0]['reason']=='Latest question'


def test_capability_preflight_requires_work_optin_and_skips_transient(board):
    tid=hold(board,explicit=False,work_order=True,kind='capability')
    kb._append_event(board,tid,'capability_preflight_blocked',{'kind':'capability','reason':'Requested login is unavailable'})
    assert len(collect(board))==1
    board.execute("UPDATE tasks SET block_kind='transient' WHERE id=?",(tid,));board.commit()
    kb._append_event(board,tid,'capability_preflight_blocked',{'kind':'transient','reason':'Startup probe timed out'})
    assert collect(board)==[]


def _member_hold(conn, reason, event='blocked', extra=None):
    tid=kb.create_task(conn,title='Member work',assignee='riley',initial_status='blocked',session_id='member-session')
    conn.execute("UPDATE tasks SET block_kind='capability',current_run_id=NULL WHERE id=?",(tid,));conn.commit()
    kb._append_event(conn,tid,event,{'kind':'capability','reason':reason,'owner_board_hold':True,'owner_attention_requested':True,'requester':'joshgarcia',**(extra or {})})
    kb.add_notify_sub(conn,task_id=tid,platform='photon',chat_id='any;-;+15550000001',user_id='+15550000001',notifier_profile='default',chat_type='dm')
    return tid


@pytest.mark.parametrize('reason,event,extra',[
    ('worker capability admission failed','capability_preflight_blocked',{'admission_digest':'x'}),
    ("Cannot access this card: kanban_show returns 'Original private action link missing'",'blocked',None),
])
def test_member_technical_blocker_routes_to_operator_never_nags(board,reason,event,extra):
    import json
    tid=_member_hold(board,reason,event,extra)
    sub=[s for s in subs(board) if s['task_id']==tid][0]
    assert attention._validate.__name__  # real code path
    with pytest.raises(ValueError,match='operator'):
        attention._validate(board,sub)
    row=board.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",(tid,event)).fetchone()
    ev=SimpleNamespace(kind=row['kind'],id=row['id'],payload=json.loads(row['payload']))
    assert attention.owns_event(board,sub,ev,CONFIG) is True  # no legacy ping/model wake
    assert attention.owns_event(board,sub,ev,{}) is True       # even with attention disabled
    assert collect(board)==[] or all(i['task_id']!=tid for d in collect(board) for i in d['items'])
    assert board.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='operator_attention_routed'",(tid,)).fetchone()[0]==1


def test_member_genuine_decision_still_reminds(board):
    assert not attention.system_failure('blocked',{'kind':'needs_input','reason':'Which price tier should the offer use?'})
    assert not attention.system_failure('blocked',{'kind':'capability','reason':'Please reconnect the authorized account.'})


def test_real_timestamp_session_uses_original_canonical_source_metadata(board):
    tid=hold(board)
    board.execute("UPDATE tasks SET session_id='20260802_104816_357e6852' WHERE id=?",(tid,));board.commit()
    kb.add_notify_sub(board,task_id=tid,platform='telegram',chat_id='owner-chat',notifier_profile='default',delivery_metadata={'session_key':'agent:main:telegram:dm:owner-chat'})
    # The native origin marker is stamped on this unrelated explicit sub too,
    # but its source key still names the original chat and must not authorize it.
    kb.add_notify_sub(board,task_id=tid,platform='telegram',chat_id='foreign-chat',notifier_profile='default',delivery_metadata={'session_key':'agent:main:telegram:dm:owner-chat'})
    due=collect(board)
    assert len(due)==1 and due[0]['sub']['chat_id']=='owner-chat'


def test_native_member_app_attention_is_separate_passive_receipt(enrolled):
    import json
    from gateway import owner_attention_app
    root,catalog,_env=enrolled
    with request('newmember'):
        created=json.loads(work.create(INTENT))
    with kb.connect(work.database(catalog['newmember']['board'])) as conn:
        assert kb.block_task(conn,created['task_id'],reason='Please reconnect your account.',kind='needs_input')
        due=collect(conn)[0]
        assert asyncio.run(attention.deliver(conn,due,None,CONFIG,now=NOW))
        rows=owner_attention_app.read('newmember')
        assert len(rows)==1 and not rows[0]['resolved']
        assert rows[0]['sources'][0]['sessionId']=='fixture-session'
        assert rows[0]['sources'][0]['runId']=='fixture-run'
        assert 'reconnect' in rows[0]['summary']
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_app_replies'").fetchone() is None
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(created['task_id'],));conn.commit()
        assert owner_attention_app.read('newmember')[0]['resolved']


def test_night_app_ask_is_immediate_but_telegram_push_waits(enrolled, board):
    import json
    from gateway import owner_attention_app
    night=int(datetime(2026,10,5,21,tzinfo=ZoneInfo('America/Toronto')).timestamp())
    hold(board)
    assert collect(board,night)==[]
    root,catalog,_env=enrolled
    with request('newmember'):
        created=json.loads(work.create(INTENT))
    with kb.connect(work.database(catalog['newmember']['board'])) as conn:
        assert kb.block_task(conn,created['task_id'],reason='Please reconnect your account.',kind='needs_input')
        due=collect(conn,night)
        assert len(due)==1
        assert asyncio.run(attention.deliver(conn,due[0],None,CONFIG,now=night))
        assert not owner_attention_app.read('newmember')[0]['resolved']
        assert collect(conn,night+60)==[]  # daily cadence is unchanged
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(created['task_id'],));conn.commit()
        assert owner_attention_app.read('newmember')[0]['resolved']


# 2026-10-05 regression: pasted iMessage digest of seven technical blockers.
_PASTED = ['Worker capability admission denied for profile scarlett; toolset mcp missing',
           'Blocked on t_2a5d2aa5 review gate; already authorized, please re-approve deployment',
           'Gateway restart required to load release hermes-agent-20261005',
           'sqlite3.OperationalError: no such table owner_cell',
           'Needs kanban_show() refresh of digest sha256 before reviewer can proceed',
           'Timed out waiting for worktree commit in /Users/Chris/projects/x',
           'Preflight failed; retry the dispatcher']


@pytest.mark.parametrize('requester,chat', [('chris','owner-chat'), ('joshgarcia','josh-chat')])
def test_pasted_technical_digest_never_reaches_any_person(board,sender,requester,chat):
    ids=[hold(board,chat=chat,requester=requester) for _ in range(7)]
    for tid,reason in zip(ids,_PASTED):
        kb._append_event(board,tid,'blocked',{'kind':'needs_input','reason':reason,'owner_board_hold':True,'owner_attention_requested':True,'requester':requester})
    assert collect(board)==[]
    import json
    for sub in subs(board):
        row=board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(sub['task_id'],)).fetchone()
        ev=SimpleNamespace(kind='blocked',id=row['id'],payload=json.loads(row['payload']))
        imsg={**sub,'platform':'photon'}
        assert attention.owns_event(board,imsg,ev,CONFIG)  # no iMessage ping either
    routed=board.execute("SELECT COUNT(DISTINCT task_id) FROM task_events WHERE kind='operator_attention_routed'").fetchone()[0]
    assert routed==7
    assert board.execute("SELECT COUNT(*) FROM tasks WHERE status='blocked'").fetchone()[0]==7


def test_genuine_decision_is_one_gentle_question_asked_once(board,sender):
    hold(board);hold(board)
    sent,adapter=sender
    due=collect(board);assert len(due)==1 and len(due[0]['items'])==1
    assert asyncio.run(attention.deliver(board,due[0],adapter,CONFIG,now=NOW))
    text=sent[0][1]
    assert 't_' not in text and '•' not in text and 'reminder' not in text and 'move this work forward' not in text
    assert text.count('\n')==0 and text.endswith(('?','.')) and len(text)<300
    # next day: the other genuine ask, never the first one again
    due=collect(board,NOW+86400);assert len(due)==1
    assert asyncio.run(attention.deliver(board,due[0],adapter,CONFIG,now=NOW+86400))
    assert sent[0][1]==sent[1][1] and len(sent)==2  # two distinct cards, same title fixture
    assert collect(board,NOW+2*86400)==[]  # unchanged asks never repeat


@pytest.mark.parametrize('requester',['chris','joshgarcia'])
def test_imessage_sender_ineligible_for_pasted_digest(board,sender,requester):
    tids=[]
    for reason in _PASTED:
        tid=kb.create_task(board,title='Synthetic',assignee='riley',initial_status='blocked',session_id='imsg-session')
        board.execute("UPDATE tasks SET block_kind='needs_input',current_run_id=NULL WHERE id=?",(tid,));board.commit()
        kb._append_event(board,tid,'blocked',{'kind':'needs_input','reason':reason,'owner_board_hold':True,'owner_attention_requested':True,'requester':requester})
        kb.add_notify_sub(board,task_id=tid,platform='photon',chat_id='any;-;+155****0009',user_id='+155****0009',notifier_profile='default',chat_type='dm')
        tids.append(tid)
    sent,_=sender
    assert collect(board)==[] and sent==[]
    assert board.execute("SELECT COUNT(*) FROM owner_attention_deliveries").fetchone()[0]==0


# 2026-10-06 08:20 regression (t_e7e55830): owner-agent-tools-v1 reverted the
# gentle fix and Chris received "I need your help to move this work forward:"
# over iMessage for t_a8ce741d / t_250fdd7e "worker capability admission failed".
def test_2026_10_06_capability_admission_digest_never_renders_or_sends(board,sender):
    import inspect, json
    assert 'move this work forward' not in inspect.getsource(attention)
    assert 'one daytime reminder a day' not in inspect.getsource(attention)
    for _ in range(2):
        tid=kb.create_task(board,title='Synthetic admission',assignee='scarlett',initial_status='blocked',session_id='imsg-chris')
        board.execute("UPDATE tasks SET block_kind='capability',current_run_id=NULL WHERE id=?",(tid,));board.commit()
        kb._append_event(board,tid,'capability_preflight_blocked',{'kind':'capability','reason':'worker capability admission failed','admission_digest':'x','owner_board_hold':True,'owner_attention_requested':True,'requester':'chris'})
        kb.add_notify_sub(board,task_id=tid,platform='photon',chat_id='any;-;+155****0001',user_id='+155****0001',notifier_profile='default',chat_type='dm')
    sent,_=sender
    assert collect(board)==[] and sent==[]
    for sub in subs(board):
        row=board.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked'",(sub['task_id'],)).fetchone()
        assert attention.owns_event(board,sub,SimpleNamespace(kind=row['kind'],id=row['id'],payload=json.loads(row['payload'])),CONFIG)
    assert board.execute("SELECT COUNT(DISTINCT task_id) FROM task_events WHERE kind='operator_attention_routed'").fetchone()[0]==2

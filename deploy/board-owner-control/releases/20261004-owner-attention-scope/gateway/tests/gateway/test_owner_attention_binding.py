"""Trusted current caller binds passive follow-up without changing old returns."""
import json
from types import SimpleNamespace
import pytest
from gateway import owner_attention as attention, owner_attention_binding as source
from gateway import owner_delegation as work, session_context as sc
from hermes_cli import board_owner_manage as control, kanban_db as kb
from tests.gateway.test_member_owner_delegation import enrolled, request, INTENT, scope
from tests.gateway.test_owner_attention import CONFIG,NOW


@pytest.fixture
def root_board(tmp_path,monkeypatch):
    root=tmp_path.resolve();monkeypatch.setenv('HERMES_HOME',str(root));monkeypatch.setattr(control,'ROOT',root)
    monkeypatch.setattr(work,'ROOT',root)
    from gateway import app_business_authority
    def enrollment(principal, anchor):
        assert principal == 'chris' and anchor == root
        return dict(owner='chris',profile='chris',board='chris-home',fingerprint='fixture')
    monkeypatch.setattr(app_business_authority,'canonical_enrollment',enrollment)
    path=root/'kanban/boards/chris-home/kanban.db';conn=kb.connect(path)
    env={'HERMES_SESSION_ID':'20260802_104816_357e6852','HERMES_SESSION_KEY':'agent:main:telegram:dm:own','HERMES_SESSION_PLATFORM':'telegram','HERMES_SESSION_CHAT_ID':'own','HERMES_SESSION_CHAT_TYPE':'dm','HERMES_SESSION_USER_ID':'own','HERMES_SESSION_MESSAGE_ID':'123','HERMES_SESSION_PROFILE':'default'}
    from gateway import josh_board_authority
    monkeypatch.setattr(josh_board_authority,'admitted_board',lambda b:b)
    tid=kb.create_task(conn,title='Orphan blocked card',assignee='riley',initial_status='blocked',session_id='missing-original')
    monkeypatch.setattr(sc,'get_bound_session_env',lambda n,default='':env.get(n,default))
    conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,));conn.commit()
    kb._append_event(conn,tid,'blocked',{'kind':'needs_input','reason':'Please reconnect your own login'})
    yield conn,tid,env
    conn.close()


def test_root_orphan_binds_only_current_source_without_card_or_return_changes(root_board):
    conn,tid,env=root_board
    before=[tuple(r) for r in conn.execute('SELECT * FROM tasks')]
    assert source.bind_current(conn,tid,action='follow_up')['follow_up_bound']
    assert before==[tuple(r) for r in conn.execute('SELECT * FROM tasks')]
    assert kb.list_notify_subs(conn,tid)==[]
    assert conn.execute('SELECT 1 FROM task_return_intents WHERE task_id=?',(tid,)).fetchone() is None
    bound=source.subscriptions(conn)
    assert len(bound)==1 and bound[0]['chat_id']=='own'
    assert len(attention.collect(conn,bound,CONFIG,now=NOW))==1


def test_env_only_source_rejected_and_worker_cannot_bind(root_board,monkeypatch):
    conn,tid,env=root_board
    env.clear();monkeypatch.setenv('HERMES_SESSION_PLATFORM','telegram');monkeypatch.setenv('HERMES_SESSION_CHAT_ID','foreign')
    with pytest.raises(PermissionError,match='authenticated current'):source.bind_current(conn,tid,action='follow_up')
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    with pytest.raises(PermissionError,match='worker'):source.bind_current(conn,tid,action='follow_up')
    assert source.subscriptions(conn)==[]


def test_original_notify_subscription_is_untouched_and_no_stale_model_wake(root_board):
    conn,tid,env=root_board
    kb.add_notify_sub(conn,task_id=tid,platform='telegram',chat_id='old',notifier_profile='default',delivery_mode='notify+wake')
    original=kb.list_notify_subs(conn,tid,include_internal_metadata=True)
    source.bind_current(conn,tid,action='work')
    assert kb.list_notify_subs(conn,tid,include_internal_metadata=True)==original
    row=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
    assert attention.owns_event(conn,original[0],SimpleNamespace(id=row['id'],kind='blocked',payload=json.loads(row['payload'])),CONFIG)
    assert attention.collect(conn,original,CONFIG,now=NOW)==[]


def test_prior_trusted_owner_hold_survives_later_native_reblock(root_board):
    conn,tid,env=root_board
    conn.execute('UPDATE tasks SET session_id=? WHERE id=?',(env['HERMES_SESSION_KEY'],tid));conn.commit()
    kb.add_notify_sub(conn,task_id=tid,platform='telegram',chat_id='own',notifier_profile='default')
    kb._append_event(conn,tid,'blocked',{'kind':'needs_input','reason':'First owner hold','owner_board_hold':True,'owner_attention_requested':True,'requester':'chris'})
    kb._append_event(conn,tid,'unblocked',{})
    kb._append_event(conn,tid,'blocked',{'kind':'needs_input','reason':'Still need the actual account login'})
    items=attention.collect(conn,kb.list_notify_subs(conn,include_internal_metadata=True),CONFIG,now=NOW)
    assert len(items)==1 and items[0]['items'][0]['reason']=='Still need the actual account login'
    kb._append_event(conn,tid,'completed',{})
    kb._append_event(conn,tid,'blocked',{'kind':'needs_input','reason':'Different later request'})
    assert attention.collect(conn,kb.list_notify_subs(conn,include_internal_metadata=True),CONFIG,now=NOW)==[]


def test_member_current_app_followup_retains_original_completion_and_rechecks_enrollment(enrolled,monkeypatch):
    root,catalog,env=enrolled;monkeypatch.setattr(control,'ROOT',root)
    with request('newmember'):created=json.loads(work.create(INTENT))
    tid=created['task_id'];conn=kb.connect(work.database(catalog['newmember']['board']))
    try:
        original=conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]
        old_subs=kb.list_notify_subs(conn,tid,include_internal_metadata=True)
        assert kb.block_task(conn,tid,kind='needs_input',reason='Please reconnect this account')
        env.update(HERMES_SESSION_ID='current-session',HERMES_SESSION_CHAT_ID='current-session',HERMES_SESSION_KEY='new-key')
        with scope.execution_scope('newmember','newmember'),work.bind('newmember','newmember','current-session','new-run','Please follow up here'):
            assert source.bind_current(conn,tid,action='follow_up')['follow_up_bound']
        assert conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]==original
        assert kb.list_notify_subs(conn,tid,include_internal_metadata=True)==old_subs
        bound=source.subscriptions(conn)
        assert len(attention.collect(conn,bound,CONFIG,now=NOW))==1
        from gateway.owner_attention_app import persist,read
        due=attention.collect(conn,bound,CONFIG,now=NOW)[0]
        persist(conn,due,attention._text(due['items']))
        assert read('newmember')[0]['sources'][0]['sessionId']=='current-session'
        catalog.pop('newmember')
        assert attention.collect(conn,bound,CONFIG,now=NOW)==[]
    finally:conn.close()


def test_original_gateway_return_supports_real_short_dm_session_key(root_board):
    conn,tid,env=root_board
    env['HERMES_SESSION_KEY']='agent:main:telegram:dm'
    conn.execute('UPDATE tasks SET session_id=? WHERE id=?',(env['HERMES_SESSION_ID'],tid));conn.commit()
    from hermes_cli.task_return_intents import capture
    binding=capture(conn,session_id=env['HERMES_SESSION_ID'],request={'task_id':tid})
    with kb.write_txn(conn):
        conn.execute('INSERT INTO task_return_intents(task_id,binding_json) VALUES(?,?)',(tid,json.dumps(binding)))
        kb.add_notify_sub(conn,task_id=tid,**binding['target'])
    assert len(attention.collect(conn,kb.list_notify_subs(conn,include_internal_metadata=True),CONFIG,now=NOW))==1
    conn.execute("UPDATE kanban_notify_subs SET chat_id='foreign' WHERE task_id=?",(tid,));conn.commit()
    assert attention.collect(conn,kb.list_notify_subs(conn,include_internal_metadata=True),CONFIG,now=NOW)==[]


def _chris_source(env, catalog):
    catalog['chris']=dict(owner='chris',profile='chris',board='chris-home',fingerprint='chris-fixture')
    env.clear()
    env.update(HERMES_SESSION_ID='current-chris-session',HERMES_SESSION_KEY='agent:main:telegram:dm:chris',
               HERMES_SESSION_PLATFORM='telegram',HERMES_SESSION_CHAT_ID='chris',
               HERMES_SESSION_CHAT_TYPE='dm',HERMES_SESSION_USER_ID='chris',
               HERMES_SESSION_MESSAGE_ID='123',HERMES_SESSION_PROFILE='default')


def test_chris_administrative_access_cannot_attach_or_deliver_josh_private_followup(enrolled,monkeypatch):
    root,catalog,env=enrolled
    monkeypatch.setenv('HERMES_HOME',str(root));monkeypatch.setattr(control,'ROOT',root)
    with request('joshgarcia'):
        created=json.loads(work.create(INTENT))
    tid=created['task_id']
    with kb.connect(work.database('josh-home')) as conn:
        assert kb.block_task(conn,tid,kind='needs_input',reason='Reconnect your private login')
        original=conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]
        original_subs=kb.list_notify_subs(conn,tid,include_internal_metadata=True)
        before=[tuple(r) for r in conn.execute('SELECT * FROM tasks')]
        _chris_source(env,catalog)
        # Administrative board access is real and deliberately remains available.
        assert control.require_owner() is None
        from hermes_cli.owner_work_preparation import _connection_board
        assert _connection_board(conn,None)=='josh-home'
        monkeypatch.setattr(scope,'admitted_board',lambda board:board)
        from hermes_cli.task_return_intents import capture
        captured=capture(conn,session_id=env['HERMES_SESSION_ID'],request={'task_id':tid})
        unsafe=dict(requester='chris',grant=None,board='josh-home',binding=captured)
        with pytest.raises(PermissionError,match='board belongs to another owner'):
            source.bind_current(conn,tid,action='follow_up')
        with pytest.raises(PermissionError,match='board belongs to another owner'):
            source._store(conn,tid,unsafe)
        assert source.subscriptions(conn)==[]
        assert before==[tuple(r) for r in conn.execute('SELECT * FROM tasks')]
        assert original==conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]
        assert original_subs==kb.list_notify_subs(conn,tid,include_internal_metadata=True)
        work.verify_return(conn,original_subs[0])
        assert attention.collect(conn,original_subs,CONFIG,now=NOW)[0]['requester']=='joshgarcia'
        # A row written by the old bug must be refused again at delivery time.
        target={**captured['target'],'delivery_mode':'notify'}
        target['delivery_metadata']={**target['delivery_metadata'],source.MARKER:work.digest(unsafe)}
        sub=dict(task_id=tid,**target)
        source._schema(conn)
        conn.execute('INSERT INTO owner_attention_sources VALUES(?,?,?,?,?,?)',
                     (tid,'chris',work.encode(unsafe),source.identity(sub),work.encode(sub),NOW))
        conn.commit()
        with pytest.raises(PermissionError,match='board belongs to another owner'):
            source.validate(conn,sub)
        assert attention.collect(conn,[sub],CONFIG,now=NOW)==[]
        work.verify_return(conn,original_subs[0])


def test_missing_original_binding_does_not_authorize_chris_on_josh_board(enrolled,monkeypatch):
    root,catalog,env=enrolled
    monkeypatch.setenv('HERMES_HOME',str(root));monkeypatch.setattr(control,'ROOT',root)
    with kb.connect(work.database('josh-home')) as conn:
        tid=kb.create_task(conn,title='Unbound private-board task',initial_status='blocked')
        _chris_source(env,catalog)
        assert conn.execute('SELECT 1 FROM task_return_intents WHERE task_id=?',(tid,)).fetchone() is None
        with pytest.raises(PermissionError,match='board belongs to another owner'):
            source.bind_current(conn,tid,action='block')
        assert source.subscriptions(conn)==[]


@pytest.mark.parametrize('private_marker',['original_return','tenant','realm'])
def test_private_task_owner_cannot_be_overridden_on_own_board(root_board,private_marker):
    conn,tid,_env=root_board
    if private_marker=='original_return':
        binding={'kind':'native-api-return-v1','authority':{'owner':'joshgarcia'},'board':'chris-home'}
        conn.execute('INSERT INTO task_return_intents(task_id,binding_json) VALUES(?,?)',(tid,json.dumps(binding)))
    elif private_marker=='tenant':
        conn.execute("UPDATE tasks SET tenant='app:private:joshgarcia' WHERE id=?",(tid,))
    else:
        conn.execute("UPDATE tasks SET security_realm='owner:joshgarcia' WHERE id=?",(tid,))
    conn.commit()
    with pytest.raises(PermissionError,match='another private task owner'):
        source.bind_current(conn,tid,action='follow_up')
    assert source.subscriptions(conn)==[]


def test_josh_own_followup_succeeds_and_fresh_board_move_revokes(enrolled,monkeypatch):
    root,catalog,_env=enrolled
    monkeypatch.setenv('HERMES_HOME',str(root));monkeypatch.setattr(control,'ROOT',root)
    with request('joshgarcia'):
        created=json.loads(work.create(INTENT))
    with kb.connect(work.database('josh-home')) as conn:
        tid=created['task_id']
        original=conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]
        assert kb.block_task(conn,tid,kind='needs_input',reason='Reconnect your private login')
        with request('joshgarcia'):
            assert source.bind_current(conn,tid,action='follow_up')['follow_up_bound']
        sub=source.subscriptions(conn)[0]
        assert source.validate(conn,sub)['requester']=='joshgarcia'
        assert attention.collect(conn,[sub],CONFIG,now=NOW)[0]['requester']=='joshgarcia'
        assert original==conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(tid,)).fetchone()[0]
        catalog['joshgarcia']['board']='other-home'
        with pytest.raises(PermissionError,match='enrollment changed'):
            source.validate(conn,sub)
        assert attention.collect(conn,[sub],CONFIG,now=NOW)==[]

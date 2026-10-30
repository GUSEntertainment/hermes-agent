"""Owner-only idle holds on real temporary boards; no workers or providers."""
import hashlib
import json
from pathlib import Path

import pytest
from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import owner_work_start as start, owner_work_preparation as prep
from tests.hermes_cli.test_simple_owner_work import board, task, request, snap
from tests.hermes_cli.test_owner_work_preparation import plan


def hold(tid, **kwargs):
    return control.execute(dict(action='block', task_id=tid,
        reason='Waiting for the owner to provide the source file; follow up on that input.',
        kind='needs_input', **kwargs))


@pytest.mark.parametrize('status', ['triage','todo','ready','blocked'])
def test_hold_preserves_everything_except_status_and_kind(board, tmp_path, status):
    tid=task(board); child=kb.create_task(board,title='Dependent',body='Consume the prerequisite',assignee='maker',parents=[tid])
    f=tmp_path/'working-file.txt';f.write_text('Preserved original output')
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET status=?,consecutive_failures=3,block_recurrences=2,workspace_path=? WHERE id=?',
            (status,str(tmp_path),tid))
    before=dict(board.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    old_events=[dict(r) for r in board.execute('SELECT * FROM task_events WHERE task_id=?',(tid,))]
    assert hold(tid,dry_run=True)['proposed_status']=='blocked'
    assert dict(board.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())==before
    receipt=hold(tid)
    after=dict(board.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    assert after=={**before,'status':'blocked','block_kind':'needs_input'}
    assert receipt['worker_started'] is False and receipt['approval_granted'] is False
    assert 'source file' in receipt['next_action']
    events=[dict(r) for r in board.execute('SELECT * FROM task_events WHERE task_id=?',(tid,))]
    assert events[:-1]==old_events
    payload=json.loads(events[-1]['payload'])
    assert payload['owner_board_hold'] and payload['owner_attention_requested']
    assert payload['actor']=='default' and payload['requester']=='chris'
    assert board.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0
    kb.recompute_ready(board)
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,child).status=='todo'
    assert f.read_text()=='Preserved original output'


@pytest.mark.parametrize('kind', ['needs_input','capability','transient'])
def test_simple_work_resumes_hold_without_granting_external_actions(board, kind):
    tid=task(board)
    control.execute({'action':'block','task_id':tid,'kind':kind,'reason':'Wait for the recorded missing file.'})
    payload=json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()[0])
    assert payload['owner_attention_requested']==(kind!='transient')
    result=request(tid,'Use the newly provided local source; do not send or publish.')
    assert result['status']=='ready'
    assert result.get('approval_granted', False) is False
    assert result['work_order']['preserved_stop']['reason']=='Wait for the recorded missing file.'
    assert kb.get_task(board,tid).block_kind==kind
    assert 'Do not send or publish' in kb.get_task(board,tid).body
    assert start.allows_claim(board,kb.get_task(board,tid))
    assert board.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0


@pytest.mark.parametrize('kind', ['dependency','',None,'arbitrary'])
def test_invalid_hold_kind_cannot_create_fake_dependency_or_mutate(board,kind):
    tid=task(board);before=snap(board,tid)
    with pytest.raises(ValueError,match='block requires'):
        control.execute({'action':'block','task_id':tid,'kind':kind,'reason':'Wait'})
    assert snap(board,tid)==before


def test_stale_snapshot_and_active_work_are_preserved(board):
    tid=task(board);snapshot=control.describe(board,tid)['expected_state']
    kb.add_comment(board,tid,'owner','New instruction')
    with pytest.raises(ValueError,match='state changed'):
        hold(tid,expected_state=snapshot)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET worker_pid=123 WHERE id=?',(tid,))
    before=snap(board,tid)
    with pytest.raises(ValueError,match='active work'):
        hold(tid)
    assert snap(board,tid)==before


@pytest.mark.parametrize('key,value',[('HERMES_PROFILE','other'),('HERMES_KANBAN_TASK','t_other'),('HERMES_SESSION_SOURCE','cron')])
def test_unbound_profile_worker_or_cron_cannot_hold(board,monkeypatch,key,value):
    tid=task(board);before=snap(board,tid)
    monkeypatch.setenv(key,value)
    with pytest.raises(PermissionError): hold(tid)
    assert snap(board,tid)==before


@pytest.mark.parametrize('reviewed',[False,True])
def test_owner_paused_queued_review_returns_to_same_reviewer_without_fake_findings(board,tmp_path,monkeypatch,reviewed):
    from hermes_cli import owner_cell as oc
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract
    monkeypatch.setattr(oc,'_installed_profile',lambda value:value)
    extra={'owner_cell_contract':contract(owner_profile='maker',runtime_profile='maker')} if reviewed else {}
    tid=kb.create_task(board,title='Review local output',body='Original complete request. Do not publish.',assignee='maker',**extra)
    artifact=tmp_path/'review.txt';artifact.write_text('Reviewable original evidence')
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    reviewer='grace' if reviewed else 'second'
    assert kb.request_review(board,tid,summary='Review saved artifact',reviewer=reviewer,
        metadata={'owner_cell_artifact':{'ref':str(artifact),'digest':digest}})
    goal_before=[dict(r) for r in board.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(tid,))]
    runs_before=[dict(r) for r in board.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]
    assert hold(tid)['source_status']=='review'
    assert kb._resume_status_from_events(board,tid)=='review'
    result=request(tid,'The requested input is present; continue the already queued independent review.')
    assert result['status']=='review' and result['work_order']['phase']=='review'
    assert kb.get_task(board,tid).assignee==reviewer
    assert [dict(r) for r in board.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(tid,))]==goal_before
    assert [dict(r) for r in board.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]==runs_before


@pytest.mark.parametrize('old',[None,'default','missing-old'])
@pytest.mark.parametrize('full',[False,True])
def test_work_validates_new_worker_before_absent_or_invalid_old_worker(board,old,full):
    tid=task(board)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET assignee=? WHERE id=?',(old,tid))
    value=plan(assignee='second') if full else {'assignee':'second'}
    result=request(tid,'Complete the existing local work with the selected worker.',plan=value)
    assert result['status']=='ready' and kb.get_task(board,tid).assignee=='second'
    assert 'Do not send or publish' in kb.get_task(board,tid).body
    if not full:
        assert prep.current(board,tid) is None
        assert 'execution_budget' not in json.loads(kb.get_task(board,tid).worker_budget_json)
    assert board.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0


@pytest.mark.parametrize('worker',['default','missing',''])
def test_bad_replacement_worker_is_denied_atomically(board,worker):
    tid=task(board);before=snap(board,tid)
    with pytest.raises(ValueError): request(tid,plan={'assignee':worker})
    assert snap(board,tid)==before


def test_assignee_only_preserves_existing_plan_and_budget(board):
    tid=task(board)
    request(tid,plan=plan())
    before=prep.current(board,tid)
    result=request(tid,'Use the second eligible worker for this same prepared scope.',plan={'assignee':'second'})
    after=prep.current(board,tid)
    assert result['status']=='ready' and after['assignee']=='second'
    for key in ['brief','acceptance','requirements','skills','budget']:
        assert after[key]==before[key]


def test_reassignment_cannot_change_bound_route_or_fill_missing_body(board):
    tid=task(board)
    with kb.write_txn(board): board.execute("UPDATE tasks SET runtime_profile='maker' WHERE id=?",(tid,))
    before=snap(board,tid)
    with pytest.raises(ValueError,match='bound maker'): request(tid,plan={'assignee':'second'})
    assert snap(board,tid)==before
    with kb.write_txn(board): board.execute("UPDATE tasks SET runtime_profile=NULL,body='' WHERE id=?",(tid,))
    with pytest.raises(ValueError,match='actual requested work'): request(tid,plan={'assignee':'second'})


def test_authenticated_member_hold_is_bound_to_own_board_and_requester(tmp_path,monkeypatch):
    from gateway import josh_board_authority
    home=tmp_path/'home';home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME',str(home));monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setattr(control,'ROOT',home)
    for name in ('HERMES_KANBAN_DB','HERMES_KANBAN_HOME','HERMES_KANBAN_BOARD','HERMES_PROFILE','HERMES_KANBAN_TASK','HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(name,raising=False)
    grant={'principal':'new-member','profile':'new-member','board':'member-space','fingerprint':'a'*64}
    monkeypatch.setattr(control,'refresh_member_grant',lambda value:dict(grant))
    with kb.connect_closing(board='member-space') as own:
        tid=kb.create_task(own,title='Own wait',assignee='maker',triage=True)
    with kb.connect_closing(board='other-space') as other:
        other_id=kb.create_task(other,title='OTHER_PRIVATE_SENTINEL',assignee='maker',triage=True)
    from gateway import board_control_transport as transport, session_context
    monkeypatch.setattr(transport,'ROOT',home)
    transport.key_path().parent.mkdir(parents=True,mode=0o700)
    transport.key_path().write_bytes(b'k'*32);transport.key_path().chmod(0o600)
    transport.initialize_ledger(transport.ledger_path())
    monkeypatch.setattr(session_context,'get_bound_session_env',lambda key,*a:'session-1' if key=='HERMES_SESSION_ID' else '')
    direct=josh_board_authority._execution.set(('new-member','new-member'))
    try:
        envelope=transport.issue({'action':'block','task_id':tid,'board':'member-space',
            'kind':'needs_input','reason':'Waiting for the requested source file.'},grant,'session-1')
        verified_payload,verified_grant=transport.verify(envelope)
    finally:
        josh_board_authority._execution.reset(direct)
    token=control._member_context.set(verified_grant)
    try:
        result=control.execute(verified_payload)
        assert result['board']=='member-space' and result['status']=='blocked'
        with kb.connect_closing(board='member-space') as own:
            payload=json.loads(own.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",(tid,)).fetchone()[0])
            assert payload['requester']==payload['actor']=='new-member'
        with pytest.raises(PermissionError,match='outside authenticated member'):
            hold(other_id,board='other-space')
        with pytest.raises(ValueError,match='Unknown task'):
            hold(other_id)
        with kb.connect_closing(board='other-space') as other:
            assert kb.get_task(other,other_id).status=='triage'
        grant['fingerprint']='revoked'
        with pytest.raises(PermissionError,match='enrollment changed'):
            hold(tid)
    finally:
        control._member_context.reset(token)
    token=josh_board_authority._execution.set(('new-member','new-member'))
    try:
        with pytest.raises(PermissionError,match='signed controller'):
            hold(tid)
    finally:
        josh_board_authority._execution.reset(token)


def test_new_hold_does_not_override_prior_worker_review_failure(board,tmp_path,monkeypatch):
    from hermes_cli import owner_cell as oc
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract
    monkeypatch.setattr(oc,'_installed_profile',lambda value:value)
    tid=kb.create_task(board,title='Independent review',body='Original local task.',assignee='maker',
        owner_cell_contract=contract(owner_profile='maker',runtime_profile='maker'))
    artifact=tmp_path/'review-original.txt';artifact.write_text('Original artifact')
    assert kb.request_review(board,tid,summary='Review saved original',reviewer='grace',
        metadata={'owner_cell_artifact':{'ref':str(artifact),'digest':hashlib.sha256(artifact.read_bytes()).hexdigest()}})
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?",(tid,))
        kb._append_event(board,tid,'blocked',{'reason':'Reviewer needs original missing input','source_status':'review'})
    hold(tid)
    before=snap(board,tid)
    with pytest.raises(ValueError,match='reviewer findings|recorded stop and findings'):
        request(tid,'Continue the existing review after the owner wait.')
    assert snap(board,tid)==before

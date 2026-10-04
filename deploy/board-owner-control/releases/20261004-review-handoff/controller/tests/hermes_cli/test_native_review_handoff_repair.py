"""Repair a native review handoff through the authenticated owner work surface."""
import hashlib
import json

import pytest

from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import kanban_completion as completion
from tests.hermes_cli.test_ordinary_review_recovery import held, member_scope


@pytest.fixture
def queued(held):
    conn=held['conn']
    tid=kb.create_task(conn,title='Review maker output',body='Review the requested result; do not send or publish.',assignee='riley')
    maker_home=held['profile_home'].parent/'riley';maker_home.mkdir()
    (maker_home/'config.yaml').write_text('platform_toolsets:\n  cli: [file, terminal, kanban]\n')
    # The controller correctly keeps the existing no-send hold. Authorize only
    # local maker work through the real owner route before its native claim.
    control.execute(dict(action='work',task_id=tid,board=held['board'],
                         reason='Prepare the requested local evidence; do not send or publish.'))
    maker=kb.claim_task(conn,tid)
    assert maker and maker.current_run_id
    evidence={'artifacts':[{'path':str(held['artifact']), 'sha256':hashlib.sha256(held['artifact'].read_bytes()).hexdigest()}]}
    assert kb.request_review(conn,tid,summary='Completed maker output ready for independent review.',
        metadata=evidence,reviewer='margot',expected_run_id=maker.current_run_id)
    kb.add_comment(conn,tid,'riley','Existing findings and unsent output remain available.')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures=2,block_recurrences=1,last_failure_error='Historical maker failure' WHERE id=?",(tid,))
    handoff=conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
    return {**held,'task':tid,'maker':maker.current_run_id,'handoff':dict(handoff)}


def request(queued, **kw):
    return control.execute(dict(action='work',task_id=queued['task'],board=queued['board'],
        reason='Use Grace to independently review the existing Riley output; preserve the original evidence and restrictions.',
        plan={'assignee':'grace'},**kw))


def snapshot(queued):
    conn,tid=queued['conn'],queued['task']
    return {t:[dict(r) for r in conn.execute(f'SELECT * FROM {t} WHERE '+('id' if t=='tasks' else 'task_id')+'=?',(tid,))]
            for t in ('tasks','task_runs','task_events','task_comments','owner_cell_goals','review_verdicts')}


def test_real_native_handoff_changes_only_reviewer_and_appends_exact_lineage(queued):
    before=snapshot(queued)
    preview=request(queued,dry_run=True)
    assert not preview['changed'] and preview['reviewer']=='grace'
    assert snapshot(queued)==before
    result=request(queued)
    assert result['changed'] and result['status']=='review' and result['worker_started'] is False
    after=snapshot(queued)
    assert after['tasks']==[{**before['tasks'][0],'assignee':'grace'}]
    for table in ('task_runs','task_comments','owner_cell_goals','review_verdicts'):
        assert after[table]==before[table]
    assert after['task_events'][:-1]==before['task_events']
    successor=after['task_events'][-1]
    old=json.loads(queued['handoff']['payload']);new=json.loads(successor['payload'])
    assert successor['kind']=='review_requested' and successor['run_id']==queued['maker']
    assert new['implementer']==old['implementer']=='riley'
    assert new['reviewer']=='grace' and new['previous_reviewer']=='margot'
    assert new['prior_review_requested_event_id']==queued['handoff']['id']
    assert new['approval_granted'] is False
    binding=completion._independent_review_binding(queued['conn'],queued['task'])
    assert binding['reviewer']=='grace' and binding['producer_run_id']==queued['maker']
    assert request(queued)['changed'] is False


def test_plain_work_reports_missing_reviewer_without_false_queue_success(queued):
    before=snapshot(queued)
    with pytest.raises(ValueError,match='reviewer is not an installed'):
        control.execute(dict(action='work',task_id=queued['task'],board=queued['board'],reason='Please move this work forward.'))
    assert snapshot(queued)==before


@pytest.mark.parametrize('damage',['active','producer_outcome','missing_handoff','superseded','dependency','owner_goal','private_route','self_review','full_plan'])
def test_repair_refuses_invalid_or_bound_review_without_mutation(queued,monkeypatch,damage):
    conn,tid=queued['conn'],queued['task'];args={}
    if damage=='active':conn.execute("UPDATE tasks SET claim_lock='active' WHERE id=?",(tid,))
    elif damage=='producer_outcome':conn.execute("UPDATE task_runs SET outcome='failed' WHERE id=?",(queued['maker'],))
    elif damage=='missing_handoff':conn.execute("UPDATE task_events SET run_id=NULL WHERE id=?",(queued['handoff']['id'],))
    elif damage=='superseded':kb._append_event(conn,tid,'changes_requested',{'reason':'Existing artifact must be revised'})
    elif damage=='dependency':
        conn.commit();parent=kb.create_task(conn,title='Pending prerequisite',assignee='riley');kb.link_tasks(conn,parent,tid)
    elif damage=='owner_goal':
        # Use the fixture's real bound owner-cell review instead of fabricating a goal.
        goal=conn.execute('SELECT task_id FROM owner_cell_goals LIMIT 1').fetchone()[0]
        conn.execute("UPDATE tasks SET status='review' WHERE id=?",(goal,));queued['task']=goal
    elif damage=='private_route':
        from gateway import private_action_worker_policy as private
        monkeypatch.setattr(private,'classified_private',lambda *a:True)
    elif damage=='self_review':
        from hermes_cli import profiles
        monkeypatch.setattr(profiles,'profile_exists',lambda n:n in {'riley','grace'})
        args={'assignee':'riley'}
    elif damage=='full_plan':args={'assignee':'grace','brief':'Replace the evidence'}
    conn.commit();before=snapshot(queued)
    with pytest.raises(ValueError):
        if args:
            control.execute(dict(action='work',task_id=queued['task'],board=queued['board'],reason='Review existing output',plan=args))
        else:request(queued)
    assert snapshot(queued)==before


@pytest.mark.parametrize('damage',['profile','foreign_admission','revoked_member'])
def test_admission_rechecks_exact_evidence_profile_board_and_member(queued,monkeypatch,damage):
    token=None;revoked=False
    if damage=='revoked_member':
        grant,token=member_scope(queued,monkeypatch)
        from gateway import owner_delegation
        monkeypatch.setattr(owner_delegation,'agents',lambda principal:[{'id':'grace-id'}])
        monkeypatch.setattr(owner_delegation,'agent',lambda *a:{'runtime':'grace'})
    def changed(*a,**kw):
        admitted=queued['build'](*a,**kw)
        if damage=='profile':(queued['profile_home']/'config.yaml').write_text('model: changed\n')
        elif damage=='foreign_admission':admitted.contract['board_database']={'path':'another-board'}
        elif damage=='revoked_member':monkeypatch.setattr(control,'refresh_member_grant',lambda value: (_ for _ in ()).throw(PermissionError('revoked')))
        return admitted
    monkeypatch.setattr(kb,'_build_worker_admission',changed)
    before=snapshot(queued)
    try:
        with pytest.raises((ValueError,PermissionError)):request(queued)
        assert snapshot(queued)==before
    finally:
        if token is not None:control._member_context.reset(token)


def test_member_repair_requires_exact_board_and_active_own_roster(queued,monkeypatch):
    grant,token=member_scope(queued,monkeypatch)
    from gateway import owner_delegation
    monkeypatch.setattr(owner_delegation,'agents',lambda principal:[])
    monkeypatch.setattr(owner_delegation,'agent',lambda *a:{'runtime':'grace'})
    try:
        before=snapshot(queued)
        with pytest.raises(PermissionError,match='own roster'):request(queued)
        with pytest.raises(PermissionError,match='Board outside'):
            control.execute(dict(action='work',task_id=queued['task'],board='other-board',reason='Review',plan={'assignee':'grace'}))
        assert snapshot(queued)==before
        monkeypatch.setattr(owner_delegation,'agents',lambda principal:[{'id':'grace-id'}])
        assert request(queued)['reviewer']=='grace'
    finally:control._member_context.reset(token)


@pytest.mark.parametrize('key,value',[('HERMES_KANBAN_TASK','t_other'),('HERMES_SESSION_SOURCE','cron')])
def test_worker_and_cron_cannot_reassign_native_review(queued,monkeypatch,key,value):
    before=snapshot(queued);monkeypatch.setenv(key,value)
    with pytest.raises(PermissionError):request(queued)
    assert snapshot(queued)==before


def test_admission_runs_without_transaction_and_other_board_writes_can_finish(queued,monkeypatch):
    from hermes_cli import owner_board_recovery as recovery
    active={}
    original=recovery.reroute_native_review
    def track(conn,*a,**kw):
        active['conn']=conn
        return original(conn,*a,**kw)
    monkeypatch.setattr(recovery,'reroute_native_review',track)
    def competing_write(*a,**kw):
        assert not active['conn'].in_transaction
        admitted=queued['build'](*a,**kw)
        queued['conn'].execute('PRAGMA busy_timeout=0')
        queued['conn'].execute('UPDATE tasks SET priority=priority+1 WHERE id!=?',(queued['task'],))
        queued['conn'].commit()
        return admitted
    monkeypatch.setattr(kb,'_build_worker_admission',competing_write)
    assert request(queued)['changed']


@pytest.mark.parametrize('changed',['maker_evidence','task_state','new_comment'])
def test_short_commit_rejects_state_changes_during_unlocked_admission(queued,monkeypatch,changed):
    after_external=[]
    def competing_write(*a,**kw):
        admitted=queued['build'](*a,**kw)
        conn=queued['conn'];conn.execute('PRAGMA busy_timeout=0')
        if changed=='maker_evidence':
            conn.execute('UPDATE task_runs SET summary=? WHERE id=?',('New maker evidence',queued['maker']))
        elif changed=='task_state':
            conn.execute('UPDATE tasks SET title=? WHERE id=?',('New task scope',queued['task']))
        else:
            kb.add_comment(conn,queued['task'],'riley','New information arrived during admission.')
        conn.commit();after_external.append(snapshot(queued))
        return admitted
    monkeypatch.setattr(kb,'_build_worker_admission',competing_write)
    with pytest.raises(ValueError,match='state changed|unchanged maker evidence'):
        request(queued)
    assert after_external and snapshot(queued)==after_external[0]
    assert kb.get_task(queued['conn'],queued['task']).assignee=='margot'

"""Synthetic legacy state; never reads private artifacts or the live board."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_owner_cell_qa_regressions import contract, disposable_conn


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def legacy(tmp_path, monkeypatch, request):
    conn = disposable_conn(tmp_path, monkeypatch)
    for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = Path(os.environ['HERMES_HOME'])
    for profile in ('steven','legal-drafter'):
        p = home/'profiles'/profile
        p.mkdir(parents=True)
        (p/'config.yaml').write_text('model:\n  default: synthetic\n')
    art = tmp_path/'synthetic.txt'
    value = contract(owner_profile='steven', runtime_profile='legal-drafter',
        required_reviewer_profile='steven', senior_owner_profile='steven',
        owner_acceptance={'mode':'accountable_owner'},
        delegation_roles={'maker':'legal-drafter','reviewer':'steven'},
        approval_gates=['deploy','restart','security'], security_realm='founders')
    tid = kb.create_task(conn, title='Synthetic legacy root', body='Synthetic input only.',
        assignee='legal-drafter', owner_cell_contract=value,
        workspace_kind='dir', workspace_path=str(tmp_path))
    def claim(review=False):
        t = kb.claim_review_task(conn, tid) if review else kb.claim_task(conn, tid)
        assert t is not None
        kb._set_worker_pid(conn,tid,os.getpid())
        monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(t.current_run_id))
        monkeypatch.setenv('HERMES_PROFILE',t.assignee)
        from agent import review_authority as ra
        ra._reset_for_tests()
        monkeypatch.delenv('HERMES_REVIEW_DECISION_FD',raising=False)
        if review:
            rd,wr=os.pipe()
            os.write(wr,json.dumps({'version':'hermes.review-decision-handoff/v1',
                'task':tid,'run_id':t.current_run_id,'profile':t.assignee,
                'token':t.review_decision_capability}).encode())
            os.close(wr)
            monkeypatch.setenv('HERMES_REVIEW_DECISION_FD',str(rd))
            assert ra.decision_token(tid,t.current_run_id)==t.review_decision_capability
        return t
    def review(round):
        art.write_text(f'Synthetic revision {round}\n')
        res=json.loads(kt._handle_request_review({'task_id':tid,'summary':'Synthetic ready',
            'reviewer':'steven','metadata':{'owner_cell_artifact':{'ref':str(art),'digest':sha(art)}}}))
        assert res.get('status')=='review',res
        return claim(True)
    # Reach the requested loop count through native decisions, no counter reset.
    loops = getattr(request, 'param', 3)
    for i in range(loops):
        claim()
        t=review(i)
        assert kb.request_changes(conn,tid,reason=f'Synthetic defect {i}',expected_run_id=t.current_run_id,
            review_decision_capability=t.review_decision_capability)[0]
    claim()
    t=review(loops)
    assert kb.block_task(conn,tid,kind='capability',reason='Synthetic legacy routing failure',expected_run_id=t.current_run_id)
    # Historical buggy ordinary-owner run overwrote the resumable source phase.
    # This is test-fixture history, not an operation against a real board.
    kb._append_event(conn,tid,'blocked',{'source_status':'ready','kind':'capability','reason':'Synthetic legacy history'})
    yield conn,tid,art,claim,review
    from agent import review_authority as ra
    ra._reset_for_tests()
    conn.close()


@pytest.mark.parametrize("handoff_schema",["current","legacy"])
def test_legacy_unblock_restores_review_not_ordinary_owner_run(legacy,handoff_schema):
    conn,tid,art,claim,review=legacy
    if handoff_schema=='legacy':legacy_handoff_event(conn,tid)
    before=dict(oc.goal_projection(conn,tid))
    assert before['review_loop_count']==3
    assert kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='review'
    assert dict(oc.goal_projection(conn,tid))==before
    assert kb.claim_task(conn,tid) is None
    t=claim(True)
    res=json.loads(kt._handle_request_changes({'task_id':tid,'reason':'Synthetic remaining defect'}))
    assert res.get('status') in {'ready','todo'},res
    assert kb.get_task(conn,tid).assignee=='legal-drafter'
    assert oc.goal_projection(conn,tid)['review_loop_count']==4
    claim()
    review(4)
    result=json.loads(kt._handle_complete({'task_id':tid,'summary':'Synthetic exact artifact verified',
        'metadata':{'owner_cell_verdict':{'state_version':1,'artifact_ref':str(art),
            'artifact_digest':sha(art),'verdict':'PASS','defects':[]}}}))
    assert result.get('ok') is True,result
    assert kb.get_task(conn,tid).status=='done'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
    assert json.loads(oc.goal_projection(conn,tid)['contract_json'])==json.loads(before['contract_json'])


@pytest.mark.parametrize('field,value', [
    ('assignee','legal-drafter'),('runtime_profile','legal-drafter'),
    ('reviewer_profile','grace'),('current_state_version',99),
    ('claim_lock','active'),('worker_pid',123),('claim_expires',9999999999),
])
def test_inconsistent_or_active_projection_rejected_atomically(legacy,field,value):
    conn,tid,*_=legacy
    conn.execute(f'UPDATE tasks SET {field}=? WHERE id=?',(value,tid))
    before=conn.total_changes
    with pytest.raises(oc.OwnerCellGuardError,match='legacy review resume'):
        kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='blocked'
    assert conn.total_changes==before


def test_preserves_counters_history_and_repeated_call_is_noop(legacy):
    conn,tid,*_=legacy
    conn.execute("UPDATE tasks SET consecutive_failures=2,last_failure_error='synthetic' WHERE id=?",(tid,))
    before=conn.execute('SELECT block_recurrences,block_kind,body FROM tasks WHERE id=?',(tid,)).fetchone()
    assert kb.unblock_task(conn,tid)
    after=conn.execute('SELECT block_recurrences,block_kind,body FROM tasks WHERE id=?',(tid,)).fetchone()
    assert tuple(before)==tuple(after)
    assert conn.execute('SELECT consecutive_failures,last_failure_error FROM tasks WHERE id=?',(tid,)).fetchone()[:]==(2,'synthetic')
    events=conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
    assert not kb.unblock_task(conn,tid)
    assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]==events


def test_does_not_silently_release_sticky_hold(legacy):
    conn,tid,*_=legacy
    kb.recompute_ready(conn)
    assert kb.get_task(conn,tid).status=='blocked'
    assert kb.claim_review_task(conn,tid) is None
    assert kb.claim_task(conn,tid) is None


def test_restored_review_still_requires_fresh_authenticated_claim(legacy):
    conn,tid,art,claim,_=legacy
    assert kb.unblock_task(conn,tid)
    # A previous review worker's cached token is not authority for a new run.
    rejected=json.loads(kt._handle_request_changes({'task_id':tid,'reason':'Unauthorized stale decision'}))
    assert 'error' in rejected
    assert kb.get_task(conn,tid).status=='review'
    t=claim(True)
    with pytest.raises(ValueError,match='review decision capability mismatch'):
        kb.request_changes(conn,tid,reason='Wrong token',expected_run_id=t.current_run_id,
            review_decision_capability='not-the-capability')
    assert kb.get_task(conn,tid).status=='running'


@pytest.mark.parametrize('field,value', [('review_state_version',99),('review_artifact_digest',None)])
def test_stale_or_missing_review_binding_fails_closed(legacy,field,value):
    conn,tid,*_=legacy
    conn.execute(f'UPDATE owner_cell_goals SET {field}=? WHERE task_id=?',(value,tid))
    with pytest.raises(oc.OwnerCellGuardError,match='coherent bound review'):
        kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='blocked'


def test_unended_run_rejected_even_without_task_claim_pointer(legacy):
    conn,tid,*_=legacy
    conn.execute('UPDATE task_runs SET ended_at=NULL WHERE id=(SELECT MAX(id) FROM task_runs WHERE task_id=?)',(tid,))
    with pytest.raises(oc.OwnerCellGuardError,match='without active claims'):
        kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='blocked'


def test_parent_gate_survives_resume(legacy):
    conn,tid,*_=legacy
    parent=kb.create_task(conn,title='Synthetic prerequisite',assignee='legal-drafter')
    kb.link_tasks(conn,parent,tid)
    assert kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='todo'
    assert kb.claim_review_task(conn,tid) is None
    assert oc.goal_projection(conn,tid)['phase']=='review'


def test_active_mutator_lease_blocks_review_phase_repair(legacy):
    conn,tid,*_=legacy
    # Exercise the actual lease API on a synthetic declared target.
    conn.execute("INSERT INTO task_targets(task_id,target_key,access_mode,expected_version) VALUES(?,?,?,?)",
        (tid,'fixture://lease','mutate','v1'))
    tokens=oc.acquire_mutator_leases(conn,tid,run_id=kb.latest_run(conn,tid).id,expected_state_version=1)
    assert len(tokens)==1
    with pytest.raises(oc.OwnerCellGuardError,match='without active claims'):
        kb.unblock_task(conn,tid)
    assert kb.get_task(conn,tid).status=='blocked'


def test_old_pass_is_not_valid_after_state_change(legacy):
    conn,tid,art,*_=legacy
    oc.record_verdict(conn,tid,reviewer_profile='steven',state_version=1,
        artifact_ref=str(art),artifact_digest=sha(art),verdict='PASS',defects=[])
    oc.update_state(conn,tid,expected_version=1,facts=['Synthetic changed state'])
    with pytest.raises(oc.OwnerCellGuardError,match='current structured PASS'):
        oc.assert_completion_allowed(conn,tid)
    with pytest.raises(oc.OwnerCellGuardError,match='coherent bound review'):
        kb.unblock_task(conn,tid)


def test_maker_cannot_approve_after_native_rework(legacy):
    conn,tid,art,claim,_=legacy
    assert kb.unblock_task(conn,tid)
    claim(True)
    assert json.loads(kt._handle_request_changes({'task_id':tid,'reason':'Synthetic defect'})).get('ok')
    claim()
    res=json.loads(kt._handle_complete({'task_id':tid,'summary':'Invalid self approval',
        'metadata':{'owner_cell_verdict':{'state_version':1,'artifact_ref':str(art),
            'artifact_digest':sha(art),'verdict':'PASS','defects':[]}}}))
    assert 'error' in res
    assert kb.get_task(conn,tid).status=='running'
    assert kb.get_task(conn,tid).assignee=='legal-drafter'


@pytest.mark.parametrize('legacy',[2],indirect=True)
def test_explicit_two_round_budget_still_rejects_third_repair(legacy):
    conn,tid,art,claim,_=legacy
    row=oc.goal_projection(conn,tid)
    value=json.loads(row['contract_json'])
    value['worker_budget']['max_rework_rounds']=2
    conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?',(json.dumps(value),tid))
    assert kb.unblock_task(conn,tid)
    claim(True)
    res=json.loads(kt._handle_request_changes({'task_id':tid,'reason':'Synthetic budget test'}))
    assert 'repair budget exhausted' in str(res)
    assert oc.goal_projection(conn,tid)['review_loop_count']==2
    assert kb.get_task(conn,tid).assignee=='steven'



def legacy_handoff_event(conn,tid):
    row=conn.execute("SELECT id,payload,run_id FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
    value=json.loads(row['payload']);value['doer_assignee']=value.pop('implementer');value['review_assignee']=value['reviewer']
    conn.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps(value),row['id']));conn.commit()
    return row,value

@pytest.mark.parametrize('bad',['missing_run','foreign_task_run','wrong_producer','unfinished_run','wrong_reviewer','wrong_maker','empty_canonical'])
def test_legacy_alias_requires_real_original_producer_provenance(legacy,bad):
    conn,tid,art,claim,review=legacy;row,value=legacy_handoff_event(conn,tid)
    if bad=='missing_run':conn.execute('UPDATE task_events SET run_id=NULL WHERE id=?',(row['id'],))
    elif bad=='foreign_task_run':conn.execute("UPDATE task_runs SET task_id='different' WHERE id=?",(row['run_id'],))
    elif bad=='wrong_producer':conn.execute("UPDATE task_runs SET profile='steven' WHERE id=?",(row['run_id'],))
    elif bad=='unfinished_run':conn.execute('UPDATE task_runs SET ended_at=NULL WHERE id=?',(row['run_id'],))
    elif bad=='wrong_reviewer':value['review_assignee']='other'
    elif bad=='wrong_maker':value['doer_assignee']='other'
    else:value['implementer']=''
    conn.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps(value),row['id']));conn.commit()
    if bad=='unfinished_run':
        # Claim a valid review first; stale producer run must not be accepted as provenance.
        conn.execute('UPDATE task_runs SET ended_at=1 WHERE id=?',(row['run_id'],));conn.commit()
    assert kb.unblock_task(conn,tid);t=claim(True)
    if bad=='unfinished_run':conn.execute('UPDATE task_runs SET ended_at=NULL WHERE id=?',(row['run_id'],));conn.commit()
    before=oc.goal_projection(conn,tid)['review_loop_count']
    result=json.loads(kt._handle_request_changes({'task_id':tid,'reason':'Synthetic exact corrections'}))
    assert 'provenance' in json.dumps(result),result
    assert kb.get_task(conn,tid).status=='running'
    assert oc.goal_projection(conn,tid)['review_loop_count']==before

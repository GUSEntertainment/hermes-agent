"""A finished maker does not consume its first independent review allowance."""
import importlib
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_completion as completion

BUDGET = {'max_runs_per_day': 1, 'max_attempts_per_input': 1,
          'worker_max_turns': 24, 'worker_run_seconds': 300,
          'worker_total_tokens': 64000, 'worker_request_tokens': 16000}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home=tmp_path/'.hermes';home.mkdir()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(home/'kanban.db'))
    monkeypatch.setenv('HERMES_KANBAN_BOARD','default')
    monkeypatch.delenv('HERMES_KANBAN_WORKSPACES_ROOT',raising=False)
    monkeypatch.setattr(kb,'_fire_kanban_lifecycle_hook',lambda *a,**kw:None)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles,'profile_exists',lambda name:name in {'scarlett','grace'})
    conn=kb.connect()
    contract={'goal_id':'review-budget-fixture','objective':'Retain the useful maker output and independently review it',
        'owner_profile':'scarlett','owner_role_alias':'engineering_systems','runtime_profile':'scarlett',
        'required_reviewer_profile':'grace','security_realm':'company','router_mode':'reviewed',
        'risk_class':'material','capability_bundle':'engineering',
        'worker_budget':{'producers':1,'reviewers':1,'extras':0},'targets':[],
        'acceptance_test':{'expected':'pass'},'rollback':{'procedure_ref':'fixture://rollback'},
        'callback':{'source_surface':'test','return_surface':'test','report_policy':'milestones_only'},'approval_gates':[]}
    task=kb.create_task(conn,title='Useful existing output',body='Create the approved result, then independent review.',
                       assignee='scarlett',created_by='Chris',owner_cell_contract=contract)
    for table in ('tasks','owner_cell_goals'):
        key='id' if table=='tasks' else 'task_id'
        raw=json.loads(conn.execute(f'SELECT worker_budget_json FROM {table} WHERE {key}=?',(task,)).fetchone()[0])
        conn.execute(f'UPDATE {table} SET worker_budget_json=? WHERE {key}=?',
                     (json.dumps({**raw,'execution_budget':BUDGET}),task))
    conn.commit()
    yield conn,task
    conn.close()


def limits(conn,task,settings=None):
    return completion.task_limits(settings or {},kb.get_task(conn,task))


def maker_then_review(conn,task):
    maker=kb.claim_task(conn,task)
    assert maker is not None
    completion.record_admission(conn,task,limits(conn,task))
    assert kb.request_review(conn,task,summary='Six useful transcripts finished',reviewer='grace',
        metadata={'owner_cell_artifact':{'ref':'fixture://six-transcripts','digest':'fixture-sha'}},
        expected_run_id=maker.current_run_id)
    assert kb.get_task(conn,task).status=='review'
    return maker.current_run_id


def test_first_native_reviewer_gets_one_reserve_without_maker_restart_or_higher_tokens(board):
    conn,task=board
    maker_run=maker_then_review(conn,task)
    caps=limits(conn,task)
    assert {k:caps[k] for k in BUDGET}==BUDGET
    assert completion.admission_reason(conn,task,caps) is None
    claimed=kb.claim_review_task(conn,task)
    assert claimed is not None
    completion.record_admission(conn,task,caps)
    runs=conn.execute('SELECT id,profile FROM task_runs WHERE task_id=? ORDER BY id',(task,)).fetchall()
    assert [(x['id'],x['profile']) for x in runs]==[(maker_run,'scarlett'),(claimed.current_run_id,'grace')]
    receipts=[json.loads(x[0]) for x in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completion_admission' ORDER BY id",(task,))]
    assert [r['budget_phase'] for r in receipts]==['execute','review']
    assert all(r['max_total_tokens']==64000 and r['max_request_tokens']==16000 for r in receipts)
    assert completion.admission_reason(conn,task,caps)=='completion_daily_run_budget'
    # The rolling window expiring does not permit an identical review retry.
    assert importlib.reload(completion).admission_reason(conn,task,caps,now=10**12)=='completion_repeated_input_budget'


@pytest.mark.parametrize('setting',['global','task'])
def test_explicit_operator_daily_ceiling_still_counts_maker_and_reviewer(board,setting):
    conn,task=board;maker_then_review(conn,task)
    raw={'enabled':True}
    if setting=='global':raw['max_runs_per_day']=1
    else:raw['task_budgets']={task:{'max_runs_per_day':1}}
    caps=limits(conn,task,completion.policy({'kanban':{'completion':raw}}))
    assert completion.admission_reason(conn,task,caps)=='completion_daily_run_budget'
    assert conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task,)).fetchone()[0]==1


def test_status_flapping_or_forged_reviewer_cannot_create_a_reserve(board):
    conn,task=board
    completion.record_admission(conn,task,limits(conn,task))
    conn.execute("UPDATE tasks SET status='review',assignee='grace' WHERE id=?",(task,));conn.commit()
    assert completion.admission_reason(conn,task,limits(conn,task))=='completion_daily_run_budget'
    conn.execute("UPDATE tasks SET status='ready',assignee='scarlett' WHERE id=?",(task,));conn.commit()
    assert completion.admission_reason(conn,task,limits(conn,task))=='completion_daily_run_budget'


def test_legacy_review_claim_consumes_reserve_even_without_new_admission_receipt(board):
    conn,task=board;maker_then_review(conn,task)
    assert completion.admission_reason(conn,task,limits(conn,task)) is None
    assert kb.claim_review_task(conn,task) is not None
    # A process restart between claim and receipt cannot replenish the reserve.
    assert importlib.reload(completion).admission_reason(conn,task,limits(conn,task))=='completion_daily_run_budget'


def test_changed_review_labels_or_artifact_cannot_replenish_first_review_reserve(board):
    conn,task=board;maker_then_review(conn,task)
    caps=limits(conn,task)
    assert kb.claim_review_task(conn,task) is not None
    completion.record_admission(conn,task,caps)
    conn.execute("UPDATE tasks SET status='blocked',last_failure_error='new automatic message' WHERE id=?",(task,))
    conn.execute("UPDATE owner_cell_goals SET review_artifact_ref='fixture://renamed',review_artifact_digest='changed' WHERE task_id=?",(task,))
    conn.commit()
    assert completion.admission_reason(conn,task,caps)=='completion_daily_run_budget'


def test_recovery_without_budget_gets_one_attempt_without_invented_worker_caps():
    task={'id':'t_recovery','created_by':'Chris','worker_budget_json':json.dumps({
        'work_order':{'instruction':'Use the existing files to finish the requested result',
                      'revision':'substantive-revision','recovery':True,'phase':'execute'}})}
    caps=completion.task_limits({},task)
    assert caps=={'max_attempts_per_input':1,'budget_scope':'task:t_recovery'}
    env={'EXISTING':'unchanged'}
    assert completion.worker_limits(env,caps)==[]
    assert env=={'EXISTING':'unchanged'}


def test_recovery_preserves_existing_token_and_operator_caps():
    task={'id':'t_recovery','created_by':'Chris','worker_budget_json':json.dumps({
        'execution_budget':BUDGET,
        'work_order':{'instruction':'Finish the already authorized result',
                      'revision':'bounded-revision','recovery':True,'phase':'execute'}})}
    settings=completion.policy({'kanban':{'completion':{'enabled':True,
        'max_runs_per_day':1,'worker_total_tokens':48000,'worker_request_tokens':12000}}})
    caps=completion.task_limits(settings,task)
    assert caps['max_attempts_per_input']==caps['max_runs_per_day']==caps['_combined_max_runs_per_day']==1
    assert caps['worker_total_tokens']==48000 and caps['worker_request_tokens']==12000
    assert caps['worker_max_turns']==24 and caps['worker_run_seconds']==300


def plain_task(conn):
    task=kb.create_task(conn,title='Six requested transcripts',body='Make the six requested transcripts.',
                       assignee='scarlett',created_by='Chris')
    conn.execute('UPDATE tasks SET worker_budget_json=? WHERE id=?',(json.dumps({'execution_budget':BUDGET}),task))
    conn.commit()
    assert conn.execute('SELECT 1 FROM owner_cell_goals WHERE task_id=?',(task,)).fetchone() is None
    return task


def test_plain_native_review_gets_reserve_for_real_ended_maker_and_bound_artifacts(board):
    conn,_=board;task=plain_task(conn)
    maker=kb.claim_task(conn,task)
    completion.record_admission(conn,task,limits(conn,task))
    evidence={'artifacts':[{'path':f'fixture://transcript-{i}','sha256':str(i)*64} for i in range(1,7)]}
    assert kb.request_review(conn,task,summary='Six useful transcripts created',reviewer='grace',
                             metadata=evidence,expected_run_id=maker.current_run_id)
    caps=limits(conn,task)
    assert completion.admission_reason(conn,task,caps) is None
    binding=completion._independent_review_binding(conn,task)
    assert binding['producer_run_id']==maker.current_run_id and binding['source']=='native_review_requested'
    reviewer=kb.claim_review_task(conn,task)
    assert reviewer is not None
    completion.record_admission(conn,task,caps)
    assert completion._independent_review_binding(conn,task)==binding
    assert completion.admission_reason(conn,task,caps)=='completion_daily_run_budget'
    assert completion.admission_reason(conn,task,caps,now=10**12)=='completion_repeated_input_budget'
    assert conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task,)).fetchone()[0]==2


@pytest.mark.parametrize('damage',['wrong_maker','active_maker','wrong_outcome','self_review','no_run','phase_flip'])
def test_plain_review_reserve_refuses_incoherent_handoff(board,damage):
    conn,_=board;task=plain_task(conn);maker=kb.claim_task(conn,task)
    completion.record_admission(conn,task,limits(conn,task))
    assert kb.request_review(conn,task,summary='Artifacts ready',reviewer='grace',expected_run_id=maker.current_run_id)
    if damage=='wrong_maker':conn.execute("UPDATE task_runs SET profile='different' WHERE id=?",(maker.current_run_id,))
    if damage=='active_maker':conn.execute('UPDATE task_runs SET ended_at=NULL WHERE id=?',(maker.current_run_id,))
    if damage=='wrong_outcome':conn.execute("UPDATE task_runs SET outcome='blocked' WHERE id=?",(maker.current_run_id,))
    if damage=='self_review':
        conn.execute("UPDATE task_events SET payload=json_set(payload,'$.reviewer','scarlett') WHERE task_id=? AND kind='review_requested'",(task,))
        conn.execute("UPDATE tasks SET assignee='scarlett' WHERE id=?",(task,))
    if damage=='no_run':conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='review_requested'",(task,))
    if damage=='phase_flip':conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(task,))
    conn.commit()
    assert completion.admission_reason(conn,task,limits(conn,task))=='completion_daily_run_budget'


def test_verified_work_revision_controls_one_attempt_without_budget_side_effects(board, monkeypatch):
    from hermes_cli import board_owner_manage as control, owner_work_start as start
    conn,_=board
    for key in ('HERMES_PROFILE','HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(key, raising=False)
    task=kb.create_task(conn,title='Recover useful work',body='Create the authorized local result. Do not send it.',
                       assignee='scarlett',created_by='Chris',triage=True)
    with kb.write_txn(conn):
        kb._append_event(conn,task,'blocked',{'reason':'The original local source was missing.'})
    instruction='Use the saved local source to finish the requested result.'
    result=control.execute({'action':'work','task_id':task,'reason':instruction})
    assert result['work_order']['recovery'] is True
    first=completion.input_fingerprint(conn,task)
    caps=limits(conn,task)
    assert caps=={'max_attempts_per_input':1,'budget_scope':'task:'+task}
    completion.record_admission(conn,task,caps)
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'scarlett','failed',1,2,'failed')",(task,))
        conn.execute("UPDATE tasks SET status='triage',last_failure_error='reader error',consecutive_failures=2 WHERE id=?",(task,))
        kb._append_event(conn,task,'triaged',{'reason':'The attempted source was incomplete.'})
    assert start.current(conn,task) is not None
    assert completion.input_fingerprint(conn,task)==first
    assert importlib.reload(completion).admission_reason(conn,task,caps)=='completion_repeated_input_budget'
    with pytest.raises(ValueError,match='already received an attempt'):
        control.execute({'action':'work','task_id':task,'reason':instruction})
    changed=control.execute({'action':'work','task_id':task,'reason':'Use the alternate verified local source and finish the calculation.'})
    assert changed['work_order']['revision']!=result['work_order']['revision']
    assert completion.input_fingerprint(conn,task)!=first
    assert completion.admission_reason(conn,task,limits(conn,task)) is None
    completion.record_admission(conn,task,limits(conn,task))
    assert completion.admission_reason(conn,task,limits(conn,task))=='completion_repeated_input_budget'
    assert kb.get_task(conn,task).consecutive_failures==2
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completion_admission'",(task,)).fetchone()[0]==2
    assert limits(conn,task)==caps

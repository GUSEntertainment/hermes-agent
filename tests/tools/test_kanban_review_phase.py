"""Independent review entry must not be mistaken for final goal completion."""
import hashlib
import json
from types import SimpleNamespace
import pytest

from hermes_cli import kanban_db as kb, owner_cell as oc
from tests.hermes_cli.test_worker_readiness import board, contract


def forbidden_judge(*args, **kwargs):
    raise AssertionError('Review entry must not invoke an auxiliary goal judge')


def test_review_phase_never_resolves_or_calls_an_auxiliary_model(monkeypatch):
    from tools import kanban_tools as kt
    from hermes_cli import kanban, goals
    from agent import auxiliary_client
    task=SimpleNamespace(goal_mode=True,title='Review needed',body='Complete the packet and obtain independent reviewer PASS.')
    monkeypatch.setattr(kt,'_goal_judge_available',forbidden_judge)
    monkeypatch.setattr(kt,'judge_goal',forbidden_judge)
    monkeypatch.setattr(goals,'judge_goal',forbidden_judge)
    monkeypatch.setattr(auxiliary_client,'get_text_auxiliary_client',forbidden_judge)
    assert kt._goal_mode_handoff_rejection(task,'Sources captured; reviewer must assess the remaining packet.',phase='review') is None
    # upstream v0.21.5 CLI API returns (verdict, reason); review phase allows without a judge.
    assert kanban._goal_mode_handoff_rejection(task,'Sources captured; reviewer must assess the remaining packet.',phase='review') == ('done', None)


@pytest.mark.parametrize('surface',['tool','cli'])
def test_native_review_handoff_keeps_final_completion_and_action_gates(board,monkeypatch,surface):
    from tools import kanban_tools as kt
    from hermes_cli import kanban, goals
    conn,home=board
    value=contract()
    value.update(router_mode='reviewed',required_reviewer_profile='owner',owner_profile='coordinator',
                 worker_budget={'producers':1,'reviewers':1,'extras':0})
    value['delegation_roles']['reviewer']='owner'
    tid=kb.create_task(conn,title='Finish requested packet',assignee='worker',goal_mode=True,
        body='Produce the final packet, obtain independent reviewer PASS, and retain publish approval.',
        owner_cell_contract=value)
    claimed=kb.claim_task(conn,tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(claimed.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE','worker')
    monkeypatch.setattr(kt,'_goal_mode_handoff_rejection',forbidden_judge)
    monkeypatch.setattr(kanban,'_goal_mode_handoff_rejection',forbidden_judge)
    artifact=home/'captured-sources.txt';artifact.write_text('Verified source capture; remaining packet needs independent assessment.\n')
    artifact_digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    metadata={'owner_cell_artifact':{'ref':str(artifact),'digest':artifact_digest}}
    summary='Source capture verified. Independent reviewer must inspect evidence and identify remaining packet work; no final approval claimed.'
    if surface=='tool':
        out=json.loads(kt._handle_request_review(dict(task_id=tid,summary=summary,reviewer='owner',metadata=metadata)))
        assert out.get('status')=='review',out
    else:
        assert kanban._cmd_request_review(SimpleNamespace(task_id=tid,summary=summary,reviewer='owner',metadata=json.dumps(metadata),force=False))==0
    task=kb.get_task(conn,tid);goal=kb.owner_cell_read_state(conn,tid)
    assert task.status=='review' and task.assignee=='owner'
    assert kb.latest_run(conn,tid).outcome=='review_requested'
    assert goal['phase']=='review' and goal['review_artifact_digest']==artifact_digest
    stored=json.loads(conn.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?',(tid,)).fetchone()[0])
    assert stored['approval_gates']==['publish']
    assert conn.execute('SELECT COUNT(*) FROM review_verdicts WHERE task_id=?',(tid,)).fetchone()[0]==0
    with pytest.raises(oc.OwnerCellGuardError):oc.assert_completion_allowed(conn,tid)
    # Quiet CLI recognizes a successful handoff before any further model turn.
    monkeypatch.setattr(goals,'judge_goal',forbidden_judge)
    outcome=goals.run_kanban_goal_loop(task_id=tid,goal_text=task.body,first_response=summary,
        task_status_fn=lambda:kb.get_task(conn,tid).status,run_turn=forbidden_judge,block_fn=forbidden_judge)
    assert outcome['outcome']=='review_requested_by_worker'


def test_final_completion_keeps_full_goal_and_future_review_gate(board,monkeypatch):
    from tools import kanban_tools as kt
    conn,_=board
    tid=kb.create_task(conn,title='Completion gate',body='Final packet and reviewer PASS are required.',assignee='worker',goal_mode=True)
    claimed=kb.claim_task(conn,tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(claimed.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE','worker')
    calls=[]
    def judge(**kwargs):
        calls.append(kwargs)
        return 'continue','Final packet and reviewer PASS are missing',False,None,False
    monkeypatch.setattr(kt,'_goal_judge_available',lambda:True)
    monkeypatch.setattr(kt,'judge_goal',judge)
    out=json.loads(kt._handle_complete({'summary':'Sources captured only'}))
    assert 'Goal completion rejected by judge' in out['error']
    assert calls[0]['goal']=='Completion gate\n\nFinal packet and reviewer PASS are required.'
    assert kb.get_task(conn,tid).status=='running'


def test_cli_and_tool_final_completion_judges_keep_same_scope(monkeypatch):
    from hermes_cli import kanban,goals
    from tools import kanban_tools as kt
    from agent import auxiliary_client
    task=SimpleNamespace(id='t_fixture',goal_mode=True,title='Maker patch',body='Test patch; get independent PASS; deploy only after approval.')
    captured=[]
    def judge(**kwargs):
        captured.append(kwargs);return 'continue','Approval is missing',False,None,False
    monkeypatch.setattr(auxiliary_client,'get_text_auxiliary_client',lambda *_:(object(),'judge'))
    monkeypatch.setattr(goals,'judge_goal',judge)
    monkeypatch.setattr(kt,'_goal_judge_available',lambda:True)
    monkeypatch.setattr(kt,'judge_goal',judge)
    assert kanban._goal_mode_handoff_rejection(task,'Evidence')==('continue','Approval is missing')  # upstream (verdict, reason)
    assert kt._goal_mode_handoff_rejection(task,'Evidence')=='Approval is missing'
    assert captured[0]==captured[1]
    assert captured[0]['goal']==task.title+'\n\n'+task.body


def test_unknown_phase_cannot_disable_completion_judgment():
    from tools import kanban_tools as kt
    from hermes_cli import kanban
    for module in (kt,kanban):
        with pytest.raises(ValueError):module._goal_mode_handoff_rejection(None,'',phase='skip')


def test_cli_request_review_retains_native_rejection(monkeypatch,capsys):
    from contextlib import nullcontext
    from hermes_cli import kanban
    monkeypatch.setattr(kanban.kb,'connect_closing',lambda:nullcontext(object()))
    monkeypatch.setattr(kanban,'_goal_mode_handoff_rejection',forbidden_judge)
    monkeypatch.setattr(kanban,'_worker_run_id_for',lambda _:None)
    monkeypatch.setattr(kanban.kb,'request_review',lambda *a,**kw:(False,'stale exact artifact'))
    result=kanban._cmd_request_review(SimpleNamespace(task_id='t_fixture',summary='Captured evidence',metadata=None,reviewer='owner',force=False))
    assert result==1 and 'stale exact artifact' in capsys.readouterr().err

import json
import pytest
from pathlib import Path
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_legacy_review_resume import legacy, sha
from tests.hermes_cli.test_diagnosed_rework import diagnosis_args


def grant(conn, tid, args, monkeypatch, ref='codex-user-turn:fixture'):
    with monkeypatch.context() as context:
        context.delenv('HERMES_KANBAN_TASK', raising=False)
        return oc.authorize_operator_rework(conn, tid, authorization_ref=ref,
            authorization_text='Please get the existing corrected forms done.',
            brief_ref=args['brief_ref'], brief_digest=args['brief_digest'])


@pytest.mark.parametrize('legacy', [6], indirect=True)
def test_operator_correction_after_spent_reserve_preserves_maker_and_final_review(legacy, monkeypatch):
    conn, tid, artifact, claim, review = legacy
    assert kb.unblock_task(conn, tid)
    task = claim(True)
    args = diagnosis_args(tid, artifact)
    assert json.loads(kt._handle_owner_handoff(args))['status'] == 'diagnosed_rework_authorized'
    assert kb.request_changes(conn, tid, reason='Source diagnosed correction', expected_run_id=task.current_run_id,
        review_decision_capability=task.review_decision_capability)[0]
    claim(); task = review(7)
    before = dict(oc.goal_projection(conn, tid))
    with pytest.raises(oc.OwnerCellGuardError, match='task workers'):
        oc.authorize_operator_rework(conn, tid, authorization_ref='not-a-user', authorization_text='Worker text',
            brief_ref=args['brief_ref'], brief_digest=args['brief_digest'])
    assert grant(conn, tid, args, monkeypatch)['status'] == 'operator_rework_authorized'
    assert dict(oc.goal_projection(conn, tid)) == before
    with pytest.raises(oc.OwnerCellGuardError, match='already been used'):
        grant(conn, tid, args, monkeypatch, ref=' codex-user-turn:fixture ')
    with pytest.raises(ValueError):
        kb.request_changes(conn, tid, reason='Unauthorized decision', expected_run_id=task.current_run_id,
            review_decision_capability='invalid')
    assert kb.request_changes(conn, tid, reason='Exact newly authorized correction', expected_run_id=task.current_run_id,
        review_decision_capability=task.review_decision_capability)[0]
    assert kb.get_task(conn, tid).assignee == 'legal-drafter'
    assert oc.goal_projection(conn, tid)['review_loop_count'] == 8
    assert oc.goal_projection(conn, tid)['contract_json'] == before['contract_json']
    claim(); task = review(8)
    with pytest.raises(oc.OwnerCellGuardError, match='budget exhausted'):
        kb.request_changes(conn, tid, reason='Cannot automatically reuse operator authority', expected_run_id=task.current_run_id,
            review_decision_capability=task.review_decision_capability)
    result = json.loads(kt._handle_complete({'task_id':tid, 'summary':'Synthetic corrected artifact reviewed',
        'metadata':{'owner_cell_verdict':{'state_version':1, 'artifact_ref':str(artifact),
            'artifact_digest':sha(artifact), 'verdict':'PASS', 'defects':[]}}}))
    assert result.get('ok') is True, result
    assert kb.get_task(conn, tid).status == 'done'


@pytest.mark.parametrize('legacy', [6], indirect=True)
@pytest.mark.parametrize('mutation', ['artifact', 'brief', 'contract', 'late_failure'])
def test_operator_grant_is_exact_and_consumption_atomic(legacy, monkeypatch, mutation):
    conn, tid, artifact, claim, review = legacy
    assert kb.unblock_task(conn, tid); task = claim(True)
    args = diagnosis_args(tid, artifact)
    grant(conn, tid, args, monkeypatch)
    if mutation == 'artifact':
        conn.execute("UPDATE owner_cell_goals SET review_artifact_digest='changed' WHERE task_id=?", (tid,))
    elif mutation == 'brief':
        Path(args['brief_ref']).write_text('changed diagnosis')
    elif mutation == 'contract':
        c=json.loads(oc.goal_projection(conn,tid)['contract_json']);c['objective'] += ' changed'
        conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?',(json.dumps(c),tid))
    else:
        original=kb._end_run
        def fail(*a,**kw):
            original(*a,**kw)
            raise ValueError('synthetic late failure')
        monkeypatch.setattr(kb,'_end_run',fail)
    with pytest.raises((ValueError, oc.OwnerCellGuardError)):
        kb.request_changes(conn,tid,reason='Attempt correction',expected_run_id=task.current_run_id,
            review_decision_capability=task.review_decision_capability)
    assert kb.get_task(conn,tid).status == 'running'
    assert oc.goal_projection(conn,tid)['review_loop_count'] == 6
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='operator_rework_consumed'",(tid,)).fetchone()

"""Native Grace/Claire obsolete-v2 evidence handoff regression, isolated board."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb, kanban, owner_cell as oc
from tools import kanban_tools
from agent import review_authority
from tests.hermes_cli.test_owner_cell_qa_regressions import (
    contract, disposable_conn, request_and_claim_review, pass_metadata,
)

OLD = '8f8567734a84fa2f5cb9ac7c7347da63054a62cb6166702618d15a65f1e4c965'
NEW = 'a821b0637bca065aac47c7d67e3ff04fee7b38130ff33129ee4742ff4362ff54'


@pytest.fixture
def case(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    tid = kb.create_task(conn, title='Claire orchestration manifest v3 review', assignee='claire',
                         owner_cell_contract=contract(owner_profile='claire', runtime_profile='claire'))
    ref = str(tmp_path / 'orchestration-manifest.json')
    review = request_and_claim_review(conn, tid, ref=ref, digest=OLD)
    kwargs = dict(state_version=1, expected_artifact_ref=ref, expected_artifact_digest=OLD,
                  artifact_ref=ref, artifact_digest=NEW, reason='Independently checked v3 replaces obsolete v2 binding',
                  expected_run_id=review.current_run_id, review_decision_capability=review.review_decision_capability)
    yield conn, tid, review, kwargs
    conn.close()


def test_rebind_closes_claim_without_verdict_then_fresh_review_completes(case):
    conn, tid, review, kwargs = case
    before = dict(oc.goal_projection(conn, tid))
    assert kb.rebind_review_artifact_task(conn, tid, **kwargs)
    after = dict(oc.goal_projection(conn, tid))
    for key in ('owner_profile', 'runtime_profile', 'required_reviewer_profile', 'current_state_version', 'review_loop_count', 'contract_json', 'phase'):
        assert after[key] == before[key]
    assert after['review_artifact_digest'] == NEW
    assert kb.get_task(conn, tid).status == 'review'
    assert kb.latest_run(conn, tid).outcome == 'review_artifact_rebound'
    assert conn.execute('SELECT COUNT(*) FROM review_verdicts').fetchone()[0] == 0
    assert not kb._has_review_decision_capability(conn, tid, review.review_decision_capability)
    fresh = kb.claim_review_task(conn, tid)
    kb._set_worker_pid(conn, tid, os.getpid())
    assert fresh.review_decision_capability != review.review_decision_capability
    assert kb.goal_run_status(conn, tid, review.current_run_id) == 'review'
    with pytest.raises(ValueError):
        kb.rebind_review_artifact_task(conn, tid, **kwargs)
    assert kb.get_task(conn, tid).current_run_id == fresh.current_run_id
    assert kb.complete_task(conn, tid, expected_run_id=fresh.current_run_id,
                            review_decision_capability=fresh.review_decision_capability,
                            metadata=pass_metadata(ref=kwargs['artifact_ref'], digest=NEW))
    assert kb.get_task(conn, tid).status == 'done'


@pytest.mark.parametrize('change', [
    {'state_version':2}, {'state_version':True}, {'expected_artifact_digest':'0'*64},
    {'expected_artifact_ref':'different'}, {'artifact_ref':'new-unscoped-path'},
    {'artifact_digest':OLD}, {'artifact_digest':'not-a-hash'}, {'reason':''},
    {'review_decision_capability':None}, {'expected_run_id':None}, {'expected_run_id':9999},
])
def test_reject_bad_binding_or_authority_without_consuming(case, change):
    conn, tid, review, kwargs = case
    with pytest.raises((ValueError, oc.OwnerCellGuardError)):
        kb.rebind_review_artifact_task(conn, tid, **(kwargs | change))
    assert oc.goal_projection(conn, tid)['review_artifact_digest'] == OLD
    assert kb.get_task(conn, tid).status == 'running'
    assert kb._has_review_decision_capability(conn, tid, review.review_decision_capability)


def test_failed_event_rolls_back_binding_and_claim(case, monkeypatch):
    conn, tid, review, kwargs = case
    original = kb._append_event
    def fail(conn, tid, kind, *args, **kw):
        original(conn, tid, kind, *args, **kw)
        if kind == 'review_artifact_rebound':
            raise RuntimeError('injected receipt failure')
    monkeypatch.setattr(kb, '_append_event', fail)
    with pytest.raises(RuntimeError):
        kb.rebind_review_artifact_task(conn, tid, **kwargs)
    assert oc.goal_projection(conn, tid)['review_artifact_digest'] == OLD
    assert kb.get_task(conn, tid).current_run_id == review.current_run_id
    assert kb._has_review_decision_capability(conn, tid, review.review_decision_capability)


def test_missing_authority_even_legacy_contract_is_denied(case):
    conn, tid, review, kwargs = case
    with kb.write_txn(conn):
        kb._consume_review_decision_capability(conn, tid, review.review_decision_capability)
    with pytest.raises(ValueError, match='process-bound'):
        kb.rebind_review_artifact_task(conn, tid, **(kwargs | {'review_decision_capability':None}))


def test_reopened_prerequisite_requires_actual_preparation_handoff(case):
    conn, tid, review, kwargs = case
    prep = kb.create_task(conn, title='Claire: prepare exact immutable v3 handoff, not reviewer rebind', assignee='claire')
    kb.link_tasks(conn, prep, tid)
    with pytest.raises(ValueError, match='prerequisite preparation'):
        kb.rebind_review_artifact_task(conn, tid, **kwargs)
    # Preparation delivers its scoped payload; it does not claim root approval.
    assert kb.complete_task(conn, prep, summary='Verified v3 packet and old/new bindings ready for native Grace rebind')
    assert kb.rebind_review_artifact_task(conn, tid, **kwargs)
    assert kb.get_task(conn, tid).status == 'review'
    assert kb.get_task(conn, prep).status == 'done'
    assert oc.goal_projection(conn, tid)['phase'] == 'review'


@pytest.mark.parametrize('field,value',[('required_reviewer_profile','claire'), ('owner_profile','grace')])
def test_no_owner_or_reviewer_policy_loosening(case, field, value):
    conn, tid, review, kwargs = case
    # Simulate policy drift after claim; kernel must fail closed.
    with kb.write_txn(conn):
        conn.execute(f'UPDATE owner_cell_goals SET {field}=? WHERE task_id=?', (value, tid))
    with pytest.raises(ValueError, match='authorized reviewer'):
        kb.rebind_review_artifact_task(conn, tid, **kwargs)


@pytest.mark.parametrize('surface',['tool','cli'])
def test_native_surface_uses_same_authority_kernel(case, monkeypatch, capsys, surface):
    conn, tid, review, kwargs = case
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(review.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE', 'grace')
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(dict(version='hermes.review-decision-handoff/v1',task=tid,
                                     run_id=review.current_run_id,profile='grace',token=review.review_decision_capability)).encode())
    os.close(write_fd)
    monkeypatch.setenv('HERMES_REVIEW_DECISION_FD', str(read_fd))
    review_authority._reset_for_tests()
    args = {key: value for key,value in kwargs.items() if key not in {'expected_run_id','review_decision_capability'}} | {'task_id':tid}
    if surface == 'tool':
        result = json.loads(kanban_tools._handle_rebind_review_artifact(args))
        assert result.get('status') == 'review', result
    else:
        assert kanban._cmd_rebind_review_artifact(SimpleNamespace(**args)) == 0
    reopened = kb.connect(Path(conn.execute('PRAGMA database_list').fetchone()[2]))
    assert kb.get_task(reopened, tid).status == 'review'
    assert oc.goal_projection(reopened, tid)['review_artifact_digest'] == NEW
    reopened.close()
    review_authority._reset_for_tests()


def test_copied_token_cannot_authorize_child_process(case):
    conn, tid, review, kwargs = case
    db = conn.execute('PRAGMA database_list').fetchone()[2]
    child = subprocess.run([sys.executable, '-c', """
import json,sys
from pathlib import Path
from hermes_cli import kanban_db as kb
payload=json.load(sys.stdin)
conn=kb.connect(Path(payload['db']))
try:
    kb.rebind_review_artifact_task(conn,payload['tid'],**payload['kwargs'])
except ValueError as exc:
    assert 'caller_pid_mismatch' in str(exc), str(exc)
else:
    raise AssertionError('child process acquired reviewer authority')
"""], input=json.dumps(dict(db=db,tid=tid,kwargs=kwargs)), text=True, capture_output=True)
    assert child.returncode == 0, child.stderr
    assert oc.goal_projection(conn, tid)['review_artifact_digest'] == OLD
    assert kb._has_review_decision_capability(conn, tid, review.review_decision_capability)


def test_original_maker_cannot_rebind_by_matching_profile(case):
    conn, tid, review, kwargs = case
    with kb.write_txn(conn):
        row=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='review_requested'",(tid,)).fetchone()
        payload=json.loads(row['payload']); payload['implementer']='grace'
        conn.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
    with pytest.raises(ValueError, match='distinct'):
        kb.rebind_review_artifact_task(conn,tid,**kwargs)


def test_claire_case_preparation_then_rebind_then_fresh_pass(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    root = kb.create_task(conn, title='Claire v3 root', assignee='claire',
                          owner_cell_contract=contract(owner_profile='claire', runtime_profile='claire'))
    ref = str(tmp_path/'orchestration-manifest.json')
    assert kb.request_review(conn, root, reviewer='grace',
                             metadata={'owner_cell_artifact':{'ref':ref,'digest':OLD}})
    prep = kb.create_task(conn, title='Deliver exact binding packet to Grace after native capability exists', assignee='claire')
    kb.link_tasks(conn, prep, root)
    kb.recompute_ready(conn)
    assert kb.claim_review_task(conn,root) is None
    assert kb.get_task(conn,root).status == 'todo'
    assert kb.complete_task(conn,prep,summary='Native capability and exact source packet verified; reviewer rebind remains outstanding')
    kb.recompute_ready(conn)
    assert kb.get_task(conn,root).status == 'review'
    first=kb.claim_review_task(conn,root)
    kb._set_worker_pid(conn,root,os.getpid())
    assert kb.rebind_review_artifact_task(conn,root,state_version=1,expected_artifact_ref=ref,
        expected_artifact_digest=OLD,artifact_ref=ref,artifact_digest=NEW,reason='Same source, verified replacement',
        expected_run_id=first.current_run_id,review_decision_capability=first.review_decision_capability)
    assert kb.get_task(conn,root).status == 'review'
    fresh=kb.claim_review_task(conn,root)
    kb._set_worker_pid(conn,root,os.getpid())
    assert kb.complete_task(conn,root,metadata=pass_metadata(ref=ref,digest=NEW),
        expected_run_id=fresh.current_run_id,review_decision_capability=fresh.review_decision_capability)
    assert conn.execute('SELECT COUNT(*) FROM task_links WHERE parent_id=? AND child_id=?',(prep,root)).fetchone()[0] == 1
    conn.close()

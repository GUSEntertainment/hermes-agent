"""Round-two independent authority probes; isolated synthetic boards only."""
import json
import pytest
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_legacy_review_resume import legacy
from tests.hermes_cli.test_grace_operator_independent import setup, authorize, snapshot

pytestmark = pytest.mark.parametrize('legacy', [6], indirect=True)


@pytest.mark.parametrize('mutation', ['unbound_nonempty', 'task_only_binding', 'run_only_binding', 'wrong_run'])
def test_partial_or_false_authority_rejects_and_valid_retry_succeeds(legacy, monkeypatch, mutation):
    conn, tid, art, task, args = setup(legacy)
    assert json.loads(kt._handle_owner_handoff(args))['status'] == 'diagnosed_rework_authorized'
    assert kb.request_changes(conn, tid, reason='Spend synthetic diagnosed reserve', expected_run_id=task.current_run_id,
                              review_decision_capability=task.review_decision_capability)[0]
    legacy[3](); task = legacy[4](7)
    authorize(conn, tid, args, monkeypatch)
    task_digest = conn.execute('SELECT review_decision_digest FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    run_digest = conn.execute('SELECT review_decision_digest FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()[0]
    token = task.review_decision_capability
    run_id = task.current_run_id
    if mutation in ('unbound_nonempty', 'run_only_binding'):
        conn.execute('UPDATE tasks SET review_decision_digest=NULL WHERE id=?', (tid,))
    if mutation in ('unbound_nonempty', 'task_only_binding'):
        conn.execute('UPDATE task_runs SET review_decision_digest=NULL WHERE id=?', (run_id,))
    if mutation == 'unbound_nonempty': token = 'nonempty-but-not-authenticated'
    if mutation == 'wrong_run': run_id += 100
    before = snapshot(conn)
    try:
        result = kb.request_changes(conn, tid, reason='Synthetic invalid authority', expected_run_id=run_id, review_decision_capability=token)
    except (ValueError, oc.OwnerCellGuardError):
        result = (False, 'guard rejected')
    assert not result[0], result
    assert snapshot(conn) == before
    conn.execute('UPDATE tasks SET review_decision_digest=? WHERE id=?', (task_digest, tid))
    conn.execute('UPDATE task_runs SET review_decision_digest=? WHERE id=?', (run_digest, task.current_run_id))
    assert kb.request_changes(conn, tid, reason='Synthetic valid retry', expected_run_id=task.current_run_id,
                              review_decision_capability=task.review_decision_capability)[0]
    assert kb.get_task(conn, tid).assignee == 'legal-drafter'
    assert oc.goal_projection(conn, tid)['review_loop_count'] == 8
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='operator_rework_consumed'").fetchone()[0] == 1


def test_operator_grant_unavailable_through_worker_handoff(legacy, monkeypatch):
    conn, tid, art, task, args = setup(legacy)
    before = snapshot(conn)
    args.update(action='operator_rework', authorization_ref='synthetic:user:2', authorization_text='Continue once')
    result = json.loads(kt._handle_owner_handoff(args))
    assert 'error' in result
    assert snapshot(conn) == before

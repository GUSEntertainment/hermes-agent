"""Independent real-dispatch recovery probes; synthetic state only."""
import json
import pytest
from tests.hermes_cli.test_worker_readiness import board, contract
from hermes_cli import kanban_db as kb, kanban_completion as completion


def failed_task(conn, caps, index):
    ct = contract()
    ct.update(goal_id=f'grace-{index}', objective=f'Synthetic recovery {index}')
    tid = kb.create_task(conn, title=f'Synthetic recovery {index}', assignee='worker',
                         owner_cell_contract=ct, max_retries=1)
    caps['task_budgets'][tid] = {}
    claim = kb.claim_task(conn, tid)
    assert claim is not None
    completion.record_admission(conn, tid, completion.task_limits(caps, kb.get_task(conn, tid)))
    assert kb._record_task_failure(conn, tid, error='kanban_request_token_budget',
        outcome='timed_out', release_claim=True, end_run=True,
        event_payload_extra={'completion_budget_reason':'kanban_request_token_budget'})
    assert kb.get_task(conn, tid).status == 'blocked'
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET created_at=? WHERE id=?', (index+1,tid))
    return tid


@pytest.mark.parametrize('older_holds', [0, 4])
def test_dispatch_reaches_eligible_after_held_rows(board, monkeypatch, older_holds):
    conn, _ = board
    caps = {**completion.DEFAULTS, 'task_budgets': {}}
    monkeypatch.setattr(completion, 'policy', lambda: caps)
    held = []
    for i in range(older_holds):
        tid = failed_task(conn, caps, i)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'blocked', {'reason':'Explicit synthetic manual hold'})
        held.append(tid)
    eligible = failed_task(conn, caps, older_holds)
    spawned = []
    def spawn(task, workspace, admission_contract=None, **kwargs):
        spawned.append(task.id)
    for _ in range(3):
        kb.dispatch_once(conn, spawn_fn=spawn)
        if spawned:
            break
    assert all(kb.get_task(conn, t).status == 'blocked' for t in held)
    assert spawned == [eligible], {'spawned':spawned, 'eligible_status':kb.get_task(conn, eligible).status}


@pytest.mark.parametrize('malformed', ['null', '[]', '17'])
def test_dispatch_survives_malformed_contract(board, monkeypatch, malformed):
    conn, _ = board
    caps = {**completion.DEFAULTS, 'task_budgets': {}}
    monkeypatch.setattr(completion, 'policy', lambda: caps)
    tid = failed_task(conn, caps, 0)
    with kb.write_txn(conn):
        conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?', (malformed,tid))
    kb.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **kw: None)
    assert kb.get_task(conn,tid).status == 'blocked'

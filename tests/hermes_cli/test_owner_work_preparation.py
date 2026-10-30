"""Bounded preparation on isolated native boards; no workers or providers."""
import json
from pathlib import Path
import socket
import subprocess

import pytest

from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import owner_work_preparation as prep


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    for key in ('HERMES_PROFILE', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID',
                'HERMES_SESSION_SOURCE', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'board.db'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('worker', 'second'):
        root = home / 'profiles' / name
        root.mkdir(parents=True)
        (root / 'config.yaml').write_text('platform_toolsets:\n  cli: [file, kanban]\n')
    monkeypatch.setattr(control, 'require_owner', lambda: None)
    from gateway import member_worker_policy
    monkeypatch.setattr(member_worker_policy, 'select', lambda *a, **kw: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('Unexpected subprocess'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('Unexpected network'))
    conn = kb.connect()
    workspace = home / 'work'; workspace.mkdir()
    tid = kb.create_task(conn, title='Finish the real deliverable', body='Original authorized request.\n```worker-requirements\n{"toolsets":["file"]}\n```',
                         assignee='worker', workspace_kind='dir', workspace_path=str(workspace))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',consecutive_failures=2,last_failure_error='Original failure',worker_budget_json=? WHERE id=?",
                     (json.dumps({'producers': 1, 'reviewers': 1}), tid))
        kb._append_event(conn, tid, 'triaged', {'reason': 'Original failure'})
    yield conn, tid, workspace
    conn.close()


def plan(**changes):
    value = {'brief': 'Produce the requested deliverable using the saved inputs.',
             'acceptance': ['Save the deliverable and verify its contents.'],
             'requirements': {'toolsets': ['file'], 'mcp': {}},
             'budget': {'max_runs_per_day': 1, 'max_attempts_per_input': 1,
                        'worker_max_turns': 24, 'worker_run_seconds': 300,
                        'worker_total_tokens': 64000, 'worker_request_tokens': 16000}}
    value.update(changes)
    return value


def run(board, value=None, **kwargs):
    conn, tid, _ = board
    return prep.prepare(conn, tid, value or plan(), actor='default', member=None,
                        reason='Use the corrected bounded next step.', **kwargs)


def snapshot(conn, tid):
    return {'task': dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()),
            'events': [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))]}


def test_preview_and_apply_preserve_request_holds_failures_and_budget_siblings(board):
    conn, tid, _ = board
    before = snapshot(conn, tid)
    preview = run(board, dry_run=True)
    assert not preview['changed'] and preview['would_change']
    assert preview['readiness']['local_checks_passed']
    assert snapshot(conn, tid) == before
    receipt = run(board)
    after = snapshot(conn, tid)
    assert receipt['changed'] and receipt['status'] == 'triage'
    for key in ('body', 'status', 'consecutive_failures', 'last_failure_error', 'block_kind'):
        assert after['task'][key] == before['task'][key]
    budget = json.loads(after['task']['worker_budget_json'])
    assert budget['producers'] == budget['reviewers'] == 1
    assert budget['execution_budget'] == plan()['budget']
    assert len(after['events']) == len(before['events']) + 1
    assert prep.current(conn, tid)['brief'] == plan()['brief']
    from hermes_cli.owner_work_start import instruction_recovery
    diagnosis = instruction_recovery(conn, tid)['current_instruction']
    assert diagnosis['source'] == 'verified_prepared_brief'
    assert diagnosis['text'] == plan()['brief']
    assert diagnosis['event_id'] == prep.current(conn, tid)['preparation_event_id']
    state = snapshot(conn, tid)
    assert not run(board)['changed']
    assert snapshot(conn, tid) == state


def test_lifecycle_does_not_stale_plan_but_changed_request_does(board):
    conn, tid, _ = board
    run(board)
    for status in ('ready', 'running', 'blocked'):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid)); conn.commit()
        assert prep.current(conn, tid) is not None
    conn.execute("UPDATE tasks SET body=body || '\nNew controlling instruction' WHERE id=?", (tid,)); conn.commit()
    assert prep.current(conn, tid) is None
    assert prep.inspect(conn, tid)['stale']


def test_reassignment_preserves_failure_history(board):
    conn, tid, _ = board
    run(board, plan(assignee='second'))
    task = kb.get_task(conn, tid)
    assert task.assignee == 'second' and task.consecutive_failures == 2
    assert task.last_failure_error == 'Original failure'


def test_local_readiness_reports_exact_missing_resources_without_connecting(board):
    receipt = run(board, plan(requirements={'toolsets': ['browser'],
        'read_paths': ['missing-source.txt'], 'mcp': {'unavailable': ['read']}, 'localhost_server': True}))
    report = receipt['readiness']
    assert report['offline_only'] and report['claim_time_admission_required']
    assert {'code': 'missing_toolset', 'resource': 'browser'} in report['issues']
    assert {'code': 'missing_input', 'resource': 'missing-source.txt'} in report['issues']
    assert {'code': 'missing_mcp_server', 'resource': 'unavailable'} in report['issues']
    assert {'code': 'localhost_bind_check', 'resource': 'terminal'} in report['deferred']


@pytest.mark.parametrize('field,value', [('worker_pid', 42), ('claim_lock', 'active')])
def test_active_refused_atomically(board, field, value):
    conn, tid, _ = board
    conn.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (value, tid)); conn.commit()
    before = snapshot(conn, tid)
    with pytest.raises(ValueError, match='active work'):
        run(board)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('kind', ['missing_profile', 'bound_route', 'budget_bool', 'budget_extra', 'budget_oversize'])
def test_invalid_plan_is_atomic(board, kind):
    conn, tid, _ = board
    value = plan()
    if kind == 'missing_profile':
        value['assignee'] = 'missing'
    elif kind == 'bound_route':
        conn.execute("UPDATE tasks SET runtime_profile='worker' WHERE id=?", (tid,)); conn.commit()
        value['assignee'] = 'second'
    elif kind == 'budget_bool':
        value['budget']['worker_max_turns'] = True
    elif kind == 'budget_extra':
        value['budget']['unlimited'] = True
    else:
        value['budget']['worker_max_turns'] = 1000000
    before = snapshot(conn, tid)
    with pytest.raises(ValueError):
        run(board, value)
    assert snapshot(conn, tid) == before


def test_member_cannot_choose_another_roster(board, monkeypatch):
    conn, tid, _ = board
    member = {'principal': 'member', 'profile': 'member', 'board': 'member-home', 'fingerprint': 'fixture'}
    monkeypatch.setattr(control, 'require_owner', lambda: member)
    from gateway import owner_delegation
    monkeypatch.setattr(owner_delegation, 'agents', lambda principal: [])
    before = snapshot(conn, tid)
    with pytest.raises(PermissionError, match='own roster'):
        prep.prepare(conn, tid, plan(assignee='second'), actor='member', member=member, reason='Prepare')
    assert snapshot(conn, tid) == before


def test_state_race_during_readiness_refuses_atomic_apply(board, monkeypatch):
    conn, tid, _ = board
    original = prep._readiness
    def changed(*args):
        result = original(*args)
        kb.add_comment(conn, tid, 'owner', 'New direction arrived during preview')
        return result
    monkeypatch.setattr(prep, '_readiness', changed)
    with pytest.raises(ValueError, match='changed'):
        run(board)
    assert prep.current(conn, tid) is None
    assert not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?', (tid, prep.EVENT)).fetchone()


def test_changed_brief_updates_admission_identity_without_resetting_failures(board):
    conn, tid, _ = board
    run(board)
    before = snapshot(conn, tid)['task']
    first = json.loads(before['worker_budget_json'])
    receipt = run(board, plan(brief='Use the corrected source and verify the final document.'))
    after = snapshot(conn, tid)['task']
    second = json.loads(after['worker_budget_json'])
    assert receipt['changed']
    assert first['execution_budget'] == second['execution_budget']
    assert first['preparation_digest'] != second['preparation_digest']
    assert second['preparation_digest'] == prep.current(conn, tid)['plan_digest']
    for key in ('consecutive_failures', 'last_failure_error', 'body', 'status'):
        assert before[key] == after[key]


def test_daily_budget_can_count_prior_work_and_allow_one_corrected_attempt(board):
    value = plan()
    value['budget']['max_runs_per_day'] = 2
    receipt = run(board, value, dry_run=True)
    assert receipt['plan']['budget']['max_runs_per_day'] == 2
    assert receipt['plan']['budget']['max_attempts_per_input'] == 1
    value['budget']['max_runs_per_day'] = 5
    with pytest.raises(ValueError, match='max_runs_per_day'):
        run(board, value, dry_run=True)


def test_effective_budget_reports_operator_and_task_runtime_limits(board, monkeypatch):
    from hermes_cli import kanban_completion as completion
    conn, tid, _ = board
    settings = completion.policy({'kanban': {'completion': {'enabled': True, 'task_budgets': {
        tid: {'worker_max_turns': 8, 'worker_run_seconds': 300}}}}})
    monkeypatch.setattr(completion, 'policy', lambda: settings)
    conn.execute('UPDATE tasks SET max_runtime_seconds=120 WHERE id=?', (tid,));conn.commit()
    value = plan();value['budget']['worker_run_seconds'] = 600
    receipt = run(board, value, dry_run=True)
    assert receipt['effective_budget']['worker_max_turns'] == 8
    assert receipt['effective_budget']['worker_run_seconds'] == 120
    assert receipt['effective_budget']['max_runtime_seconds'] == 120
    constraints = receipt['budget_constraints']
    assert {'field': 'worker_max_turns', 'requested': 24, 'effective': 8,
            'source': 'existing_operator_budget'} in constraints
    assert {'field': 'worker_run_seconds', 'requested': 600, 'effective': 120,
            'source': 'task.max_runtime_seconds'} in constraints
    assert receipt['readiness']['effective_budget'] == receipt['effective_budget']


def test_larger_prepared_budget_has_accounting_guidance_and_keeps_operator_ceiling(board, monkeypatch):
    from hermes_cli import kanban_completion as completion
    settings = completion.policy({'kanban': {'completion': {
        'enabled': True, 'worker_total_tokens': 600000}}})
    monkeypatch.setattr(completion, 'policy', lambda: settings)
    value = plan()
    original_budget = dict(value['budget'])
    value['budget']['worker_total_tokens'] = 2_000_000
    receipt = run(board, value, dry_run=True)
    assert receipt['plan']['budget']['worker_total_tokens'] == 2_000_000
    assert receipt['effective_budget']['worker_total_tokens'] == 600000
    assert receipt['effective_budget']['worker_request_tokens'] == original_budget['worker_request_tokens']
    assert receipt['effective_budget']['worker_max_turns'] == original_budget['worker_max_turns']
    assert receipt['effective_budget']['max_attempts_per_input'] == 1
    guidance = ' '.join(receipt['readiness']['budget_guidance'])
    assert 'cached input tokens also count' in guidance
    assert 'limits are independent' in guidance
    assert 'does not increase it or authorize another attempt' in guidance
    assert receipt['budget_guidance'] == receipt['readiness']['budget_guidance']
    value['budget']['worker_total_tokens'] = 2_000_001
    with pytest.raises(ValueError, match='worker_total_tokens'):
        run(board, value, dry_run=True)


def test_connection_board_ignores_current_ui_board(board, monkeypatch):
    conn, tid, workspace = board
    target = workspace.parent / 'kanban' / 'boards' / 'actual-board' / 'kanban.db'
    target.parent.mkdir(parents=True)
    other = kb.connect(db_path=target)
    try:
        monkeypatch.setattr(kb, 'get_current_board', lambda: 'unrelated-board')
        assert prep._connection_board(other, None) == 'actual-board'
    finally:
        other.close()


def test_scratch_materialization_preserves_plan_but_other_workspace_invalidates(board):
    conn, tid, workspace = board
    conn.execute("UPDATE tasks SET workspace_kind='scratch',workspace_path=NULL WHERE id=?", (tid,));conn.commit()
    run(board)
    planned = kb._planned_workspace_contract(kb.get_task(conn, tid), prep._connection_board(conn, None))['path']
    kb.set_workspace_path(conn, tid, planned)
    assert prep.current(conn, tid) is not None
    kb.set_workspace_path(conn, tid, str(workspace / 'different'))
    assert prep.current(conn, tid) is None

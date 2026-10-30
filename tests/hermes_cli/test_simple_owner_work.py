"""Real temporary board/controller boundary; no workers or providers."""
import json
from pathlib import Path

import pytest

from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import owner_work_start as start, owner_work_preparation as prep


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'board.db'))
    for key in ('HERMES_PROFILE', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID',
                'HERMES_SESSION_SOURCE', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('maker', 'second'):
        profile = home / 'profiles' / name; profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text('platform_toolsets:\n  cli: [file, terminal, kanban]\n')
    from gateway import member_worker_policy
    monkeypatch.setattr(member_worker_policy, 'select', lambda *a, **kw: None)
    with kb.connect_closing() as conn:
        yield conn


def task(conn, **kwargs):
    return kb.create_task(conn, title='Useful work', body='Original authorized deliverable. Preserve privacy. Do not send or publish.',
                          assignee='maker', triage=True, **kwargs)


def request(tid, instruction='Find the available sources and save the requested local result.', **kwargs):
    return control.execute(dict(action='work', task_id=tid, reason=instruction, **kwargs))


def snap(conn, tid):
    return [dict(row) for row in conn.execute('SELECT * FROM tasks WHERE id=?', (tid,))], [dict(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))]


def stopped(conn, tid, reason='The old source reader failed; preserve privacy and do not publish.'):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',block_kind='needs_input',consecutive_failures=2,last_failure_error='old error' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'blocked', {'reason': reason})


def test_simple_instruction_starts_new_task_without_planner_or_comment(board):
    tid = task(board); original = kb.get_task(board, tid)
    before = snap(board, tid)
    preview = request(tid, dry_run=True)
    assert preview['proposed_status'] == 'ready' and snap(board, tid) == before
    result = request(tid)
    assert result['status'] == 'ready' and result['changed']
    assert result['worker_started'] is False
    current = kb.get_task(board, tid)
    assert current.body == original.body and current.assignee == original.assignee
    assert prep.current(board, tid) is None
    assert 'execution_budget' not in json.loads(current.worker_budget_json)
    assert board.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (tid,)).fetchone()[0] == 0
    assert request(tid)['changed'] is False


def test_technical_needs_input_continues_without_answering_protected_action(board):
    from gateway import gus_admin_cli
    RELEASE_EVENTS = ('unblocked', 'promoted_manual', 'restore_released', 'triage_resumed')
    unresolved_hold = getattr(gus_admin_cli, 'unresolved_hold', lambda conn, task: None)
    tid = task(board); stopped(board, tid)
    original_hold = unresolved_hold(board, kb.get_task(board, tid))
    original_releases = board.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN (%s)' % ','.join('?' * len(RELEASE_EVENTS)), (tid, *RELEASE_EVENTS)).fetchone()[0]
    result = request(tid)
    assert result['status'] == 'ready'
    current = kb.get_task(board, tid)
    assert current.consecutive_failures == 2 and current.block_kind == 'needs_input'
    assert current.last_failure_error == 'old error'
    assert unresolved_hold(board, current) == original_hold
    assert board.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN (%s)' % ','.join('?' * len(RELEASE_EVENTS)), (tid, *RELEASE_EVENTS)).fetchone()[0] == original_releases
    assert start.allows_claim(board, current)
    context = kb.build_worker_context(board, tid)
    assert context.index(result['work_order']['instruction']) < context.index(current.body)
    assert 'does not authorize' in context
    assert result['work_order']['preserved_stop']['reason'].startswith('The old source reader failed')


def test_unchanged_consumed_instruction_does_not_retry_but_changed_step_can(board):
    tid = task(board); stopped(board, tid)
    one = request(tid)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'maker','failed',1,2,'failed')", (tid,))
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(board, tid, 'triaged', {'reason': 'The attempted input is unavailable'})
    before = snap(board, tid)
    with pytest.raises(ValueError, match='already received an attempt'):
        request(tid)
    assert snap(board, tid) == before
    two = request(tid, 'Use the saved local source and finish the calculation instead.')
    assert two['work_order']['revision'] != one['work_order']['revision']
    assert kb.get_task(board, tid).consecutive_failures == 2
    assert two['work_order']['max_attempts_per_revision'] == 1


def test_dependencies_remain_real_and_can_promote_when_finished(board):
    parent = task(board); child = task(board, parents=[parent])
    result = request(child)
    assert result['status'] == 'todo' and not result['dependencies_satisfied']
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done',completed_at=1 WHERE id=?", (parent,))
    kb.recompute_ready(board)
    # The controller's legacy hold classifier is handled by the authenticated
    # work-order claim hook. No dependency itself is bypassed.
    assert kb._parents_satisfied(board, child)
    assert kb.get_task(board, child).status == 'ready'


def test_changed_task_or_new_stop_invalidates_pickup(board):
    tid = task(board); request(tid)
    assert start.allows_claim(board, kb.get_task(board, tid))
    kb.add_comment(board, tid, 'owner', 'Wait for the missing file before continuing.')
    assert not start.allows_claim(board, kb.get_task(board, tid))
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET body=body || ' Changed scope.' WHERE id=?", (tid,))
    assert start.current(board, tid) is None


@pytest.mark.parametrize('key,value', [('HERMES_PROFILE','josh'), ('HERMES_SESSION_SOURCE','cron'), ('HERMES_KANBAN_TASK','t_other')])
def test_named_profiles_workers_and_cron_cannot_mint_work(board, monkeypatch, key, value):
    tid = task(board); before = snap(board, tid)
    monkeypatch.setenv(key, value)
    with pytest.raises(PermissionError):
        request(tid)
    assert snap(board, tid) == before


def test_stale_snapshot_and_active_task_are_refused(board):
    tid = task(board)
    stale = control.describe(board, tid)['expected_state']
    kb.add_comment(board, tid, 'owner', 'New evidence')
    with pytest.raises(ValueError, match='state changed'):
        request(tid, expected_state=stale)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET worker_pid=23 WHERE id=?', (tid,))
    with pytest.raises(ValueError, match='active work'):
        request(tid)


def test_existing_preparation_and_budget_survive_current_work_instruction(board):
    tid = task(board)
    plan = {'brief': 'Existing detailed next step', 'acceptance': ['Save verified file'],
            'requirements': {'toolsets': ['file'], 'mcp': {}},
            'budget': {'max_runs_per_day': 2, 'max_attempts_per_input': 1,
                       'worker_max_turns': 24, 'worker_run_seconds': 600,
                       'worker_total_tokens': 256000, 'worker_request_tokens': 64000}}
    control.execute(dict(action='prepare',task_id=tid,reason='Existing useful preparation',
                         expected_state=control.describe(board,tid)['expected_state'],plan=plan))
    prior = prep.current(board,tid)
    receipt = request(tid)
    fresh = prep.current(board,tid)
    assert fresh['plan_digest'] == prior['plan_digest']
    assert fresh['budget'] == plan['budget']
    assert fresh['preparation_event_id'] > prior['preparation_event_id']
    assert receipt['status'] == 'ready'


def test_queued_review_is_noop_and_retains_review_contract(board):
    tid = task(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    before = snap(board, tid)
    result = request(tid)
    assert result['status'] == 'review' and not result['changed']
    assert snap(board, tid) == before


def test_budget_only_plan_change_cannot_purchase_another_recovery_attempt(board):
    tid = task(board); stopped(board, tid)
    plan = {'brief': 'Existing next step', 'acceptance': ['Save file'],
            'requirements': {'toolsets': ['file'], 'mcp': {}},
            'budget': {'max_runs_per_day': 2, 'max_attempts_per_input': 1,
                       'worker_max_turns': 24, 'worker_run_seconds': 600,
                       'worker_total_tokens': 256000, 'worker_request_tokens': 64000}}
    first = request(tid, plan=plan)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'maker','failed',1,2,'failed')", (tid,))
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    plan['budget']['worker_max_turns'] = 32
    before = snap(board,tid)
    with pytest.raises(ValueError, match='already received an attempt'):
        request(tid, plan=plan)
    assert snap(board,tid) == before
    assert start.current(board,tid)['revision'] == first['work_order']['revision']


def test_change_after_internal_snapshot_before_transaction_is_rejected(board, monkeypatch):
    tid = task(board)
    original = prep._eligible
    def changed(conn, task_row, worker, member):
        original(conn, task_row, worker, member)
        kb.add_comment(conn, task_row['id'], 'owner', 'New scope before mutation')
    monkeypatch.setattr(prep,'_eligible',changed)
    with pytest.raises(ValueError,match='state changed'):
        request(tid)
    assert kb.get_task(board,tid).status == 'triage'
    assert start.current(board,tid) is None


def test_unspent_budget_update_keeps_prepared_plan_and_reports_change(board):
    tid = task(board)
    plan = {'brief': 'Existing next step', 'acceptance': ['Save file'],
            'requirements': {'toolsets': ['file'], 'mcp': {}},
            'budget': {'max_runs_per_day': 2, 'max_attempts_per_input': 1,
                       'worker_max_turns': 24, 'worker_run_seconds': 600,
                       'worker_total_tokens': 256000, 'worker_request_tokens': 64000}}
    first = request(tid, plan=plan)
    plan['budget']['worker_max_turns'] = 32
    second = request(tid, plan=plan)
    assert second['changed']
    assert second['work_order']['revision'] == first['work_order']['revision']
    assert prep.current(board,tid)['budget']['worker_max_turns'] == 32
    assert prep.current(board,tid)['requirements']['toolsets'] == ['file']


def test_explicit_work_after_restore_enables_pickup_without_action_release(board):
    tid = task(board)
    # Same real restore event consumed by the native pause logic; keep original
    # no-send/no-publish restrictions in place.
    with kb.write_txn(board):
        kb._append_event(board,tid,'restored',{'status':'triage'})
    result = request(tid)
    assert result['status'] == 'ready'
    assert start.allows_claim(board,kb.get_task(board,tid))
    if hasattr(kb,'restore_paused'):
        assert not kb.restore_paused(board,tid)
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='restore_released'", (tid,)).fetchone()


@pytest.mark.parametrize('kind', ['qa_continued', 'restored', 'archived'])
def test_later_explicit_pause_supersedes_previous_work_order(board, kind):
    tid = task(board); request(tid)
    assert start.allows_claim(board,kb.get_task(board,tid))
    with kb.write_txn(board):
        kb._append_event(board,tid,kind,{'reason':'New explicit scheduling decision'})
    assert not start.allows_claim(board,kb.get_task(board,tid))


def test_instruction_diagnosis_and_real_corrected_work_preserve_request(board):
    tid = task(board)
    with kb.write_txn(board):
        for reason in ('Obsolete capture instruction requires missing tool.',
                       'Obsolete capture instruction requires missing tool.'):
            kb._append_event(board, tid, 'blocked', {'kind': 'capability', 'reason': reason})
        kb._append_event(board, tid, 'blocked', {'owner_board_hold': True,
            'kind': 'needs_input', 'reason': 'This owner note is not a worker failure.'})
    before = snap(board, tid)
    diagnosis = start.instruction_recovery(board, tid)
    assert diagnosis['repeated_same_failure'] is True
    assert diagnosis['latest_worker_failure']['reason'].startswith('Obsolete capture')
    assert diagnosis['current_instruction']['source'] == 'original_task_body'
    assert diagnosis['current_instruction']['original_request_ref'] == 'task.body'
    assert diagnosis['retry_granted'] is False and snap(board, tid) == before
    corrected = 'Use the existing local source instead of the obsolete capture step.'
    result = request(tid, corrected)
    assert result['status'] == 'ready'
    diagnosis = start.instruction_recovery(board, tid)
    assert diagnosis['current_instruction']['text'] == corrected
    assert diagnosis['current_instruction']['source'] == 'verified_owner_work_order'
    context = kb.build_worker_context(board, tid)
    assert corrected in context and 'Do not send or publish.' in context
    assert kb.get_task(board, tid).body == before[0][0]['body']
    assert board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 0


def test_instruction_diagnosis_distinguishes_causes_and_rejects_stale_order(board):
    tid = task(board)
    request(tid, 'Use the existing sources to finish.')
    with kb.write_txn(board):
        kb._append_event(board, tid, 'blocked', {'kind': 'capability', 'reason': 'Missing source file.'})
        kb._append_event(board, tid, 'blocked', {'kind': 'capability', 'reason': 'Browser connection unavailable.'})
        board.execute("UPDATE tasks SET body=body || ' Current correction.' WHERE id=?", (tid,))
    diagnosis = start.instruction_recovery(board, tid)
    assert diagnosis['repeated_same_failure'] is False
    assert diagnosis['current_instruction']['source'] == 'original_task_body'
    assert diagnosis['current_instruction']['text'] is None
    assert diagnosis['latest_worker_failure']['reason'] == 'Browser connection unavailable.'

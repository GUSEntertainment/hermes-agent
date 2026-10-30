"""Prepared substantive work gets explicit budgets without resetting attempts."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_completion as completion


PLAN_BUDGET = {
    'max_runs_per_day': 1, 'max_attempts_per_input': 1,
    'worker_max_turns': 64, 'worker_run_seconds': 1800,
    'worker_total_tokens': 256000, 'worker_request_tokens': 128000,
}


def task(budget=None):
    return {'id': 't_prepared', 'created_by': 'Chris', 'worker_budget_json': {
        'max_children': 3, 'max_rework_rounds': 2,
        'execution_budget': deepcopy(PLAN_BUDGET if budget is None else budget),
    }}


@pytest.mark.parametrize('encoded', [False, True])
def test_prepared_budget_applies_without_maintenance_policy_and_preserves_siblings(encoded):
    value = task()
    if encoded:
        value['worker_budget_json'] = json.dumps(value['worker_budget_json'])
    before = deepcopy(value)
    limits = completion.task_limits(completion.policy({}), SimpleNamespace(**value))
    assert limits == {**PLAN_BUDGET, 'budget_scope': 'task:t_prepared'}
    assert value == before
    env = {}
    assert completion.worker_limits(env, limits) == ['--max-turns', '64', '--run-budget', '1800']
    assert env == {'HERMES_KANBAN_TOTAL_TOKEN_LIMIT': '256000',
                   'HERMES_KANBAN_REQUEST_TOKEN_LIMIT': '128000'}


def test_implicit_maintenance_defaults_do_not_cap_prepared_work():
    settings = completion.policy({'kanban': {'completion': {'enabled': True}}})
    assert completion.task_limits(settings, task()) == {**PLAN_BUDGET, 'budget_scope': 'task:t_prepared'}
    ordinary = {'id': 't_normal', 'created_by': 'Chris', 'worker_budget_json': {'max_children': 3}}
    assert completion.task_limits(settings, ordinary) == {}
    assert completion.task_limits({}, ordinary) == {}
    assert completion.policy({'kanban': {'completion': {'enabled': False, 'worker_max_turns': 12}}}) == {}
    repair = {**ordinary, 'created_by': 'worker-readiness'}
    assert completion.task_limits(settings, repair)['budget_scope'] == 'maintenance'


def test_explicit_global_and_task_limits_only_tighten_prepared_plan():
    settings = completion.policy({'kanban': {'completion': {
        'enabled': True, 'worker_total_tokens': 64000, 'worker_max_turns': 16,
        'worker_run_seconds': 3600,
        'task_budgets': {'t_prepared': {'worker_max_turns': 12, 'worker_request_tokens': 96000}},
    }}})
    limits = completion.task_limits(settings, task())
    assert limits['worker_max_turns'] == 12
    assert limits['worker_run_seconds'] == 1800
    assert limits['worker_total_tokens'] == limits['worker_request_tokens'] == 64000
    assert limits['max_runs_per_day'] == limits['max_attempts_per_input'] == 1


@pytest.mark.parametrize('bad', [
    {}, {'extra': 1, **PLAN_BUDGET},
    {**PLAN_BUDGET, 'max_runs_per_day': 0},
    {**PLAN_BUDGET, 'max_runs_per_day': 5},
    {**PLAN_BUDGET, 'max_attempts_per_input': True},
    {**PLAN_BUDGET, 'worker_max_turns': 11},
    {**PLAN_BUDGET, 'worker_max_turns': 65},
    {**PLAN_BUDGET, 'worker_run_seconds': 59},
    {**PLAN_BUDGET, 'worker_run_seconds': 1801},
    {**PLAN_BUDGET, 'worker_total_tokens': 31999},
    {**PLAN_BUDGET, 'worker_total_tokens': 2_000_001},
    {**PLAN_BUDGET, 'worker_request_tokens': 7999},
    {**PLAN_BUDGET, 'worker_request_tokens': 128001},
    {**PLAN_BUDGET, 'worker_total_tokens': 32000, 'worker_request_tokens': 64000},
    {**PLAN_BUDGET, 'worker_max_turns': '24'},
])
def test_invalid_prepared_budget_fails_closed_without_enabled_maintenance(bad):
    with pytest.raises(ValueError, match='execution_budget.*repair the task plan'):
        completion.task_limits({}, task(bad))


@pytest.mark.parametrize('raw', ['not-json', '[]', 'null', [], True, {'execution_budget': None}])
def test_malformed_budget_metadata_fails_closed(raw):
    with pytest.raises(ValueError, match='execution_budget.*repair the task plan'):
        completion.task_limits({}, {**task(), 'worker_budget_json': raw})


def test_daily_plan_allowance_accepts_four_but_preserves_tighter_root_cap():
    value = task({**PLAN_BUDGET, 'max_runs_per_day': 4})
    assert completion.task_limits({}, value)['max_runs_per_day'] == 4
    settings = completion.policy({'kanban': {'completion': {
        'enabled': True, 'task_budgets': {'t_prepared': {'max_runs_per_day': 1}},
    }}})
    assert completion.task_limits(settings, value)['max_runs_per_day'] == 1


@pytest.mark.parametrize('total', [256001, 600000, 2_000_000])
def test_explicit_larger_budget_is_accepted_without_increasing_other_limits(total):
    budget = {**PLAN_BUDGET, 'worker_total_tokens': total}
    assert completion.task_limits({}, task(budget)) == {**budget, 'budget_scope': 'task:t_prepared'}
    settings = completion.policy({'kanban': {'completion': {
        'enabled': True, 'worker_total_tokens': 600000,
        'task_budgets': {'t_prepared': {'worker_request_tokens': 96000}},
    }}})
    limits = completion.task_limits(settings, task(budget))
    assert limits == {**budget, 'worker_total_tokens': min(total, 600000),
                      'worker_request_tokens': 96000, 'budget_scope': 'task:t_prepared'}


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(conn, title='Existing work', body='Implement the approved fix',
                             created_by='Chris', assignee='worker')
    yield conn, task_id
    conn.close()


def store_prepared_plan(conn, task_id, *, budget=None, **changes):
    from hermes_cli import kanban_db as kb, owner_work_preparation as preparation
    plan = {'brief': 'Implement the identified routing correction',
            'acceptance': ['The same request reaches its own board'],
            'requirements': {'toolsets': ['file']}, 'assignee': 'worker', 'skills': [],
            'budget': deepcopy(PLAN_BUDGET if budget is None else budget), **changes}
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_budget_json=? WHERE id=?',
                     (json.dumps({'max_children': 3, 'execution_budget': plan['budget']}), task_id))
        kb._append_event(conn, task_id, preparation.EVENT, {
            'version': 1, 'plan': plan, 'plan_digest': preparation._hash(plan),
            'binding_digest': preparation._binding(conn, task_id), 'readiness': {'offline_only': True},
        })
    assert preparation.current(conn, task_id) is not None


def test_recorded_attempt_survives_budget_only_plan_revision_restart_and_bookkeeping(board):
    conn, task_id = board
    store_prepared_plan(conn, task_id)
    value = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    limits = completion.task_limits({}, value)
    first = completion.input_fingerprint(conn, task_id)
    completion.record_admission(conn, task_id, limits)
    conn.execute("UPDATE tasks SET status='blocked',consecutive_failures=1,last_failure_error='prior failure' WHERE id=?", (task_id,))
    conn.commit()
    store_prepared_plan(conn, task_id, budget={**PLAN_BUDGET, 'worker_max_turns': 24,
                                             'worker_total_tokens': 2_000_000})
    refreshed = importlib.reload(completion)
    assert refreshed.input_fingerprint(conn, task_id) == first
    # After the rolling day expires, the same input still cannot retry.
    assert refreshed.admission_reason(conn, task_id, limits, now=10**12) == 'completion_repeated_input_budget'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completion_admission'", (task_id,)).fetchone()[0] == 1
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == 1


def test_corrected_plan_can_use_one_additional_run_without_resetting_prior_admission(board):
    conn, task_id = board
    store_prepared_plan(conn, task_id)
    original = {**PLAN_BUDGET, 'budget_scope': 'task:' + task_id}
    completion.record_admission(conn, task_id, original)
    budget = {**PLAN_BUDGET, 'max_runs_per_day': 2}
    store_prepared_plan(conn, task_id, budget=budget)
    value = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    limits = completion.task_limits({}, value)
    assert completion.admission_reason(conn, task_id, limits) == 'completion_repeated_input_budget'
    store_prepared_plan(conn, task_id, budget=budget,
                        brief='Implement the newly identified missing path before verifying the result')
    assert completion.admission_reason(conn, task_id, limits) is None
    completion.record_admission(conn, task_id, limits)
    assert completion.admission_reason(conn, task_id, limits) == 'completion_daily_run_budget'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completion_admission'", (task_id,)).fetchone()[0] == 2


@pytest.mark.parametrize('changed', [
    {'brief': 'Fix the newly located implementation rather than repeating the old test'},
    {'acceptance': ['Verify the repaired implementation with the observed failing case']},
    {'requirements': {'toolsets': ['file', 'terminal']}},
    {'assignee': 'another-worker'},
    {'skills': ['focused-debugging']},
])
def test_substantive_prepared_input_changes_attempt_identity_without_erasing_history(board, changed):
    conn, task_id = board
    store_prepared_plan(conn, task_id)
    limits = {**PLAN_BUDGET, 'budget_scope': 'task:' + task_id}
    first = completion.input_fingerprint(conn, task_id)
    completion.record_admission(conn, task_id, limits)
    store_prepared_plan(conn, task_id, **changed)
    assert completion.input_fingerprint(conn, task_id) != first
    # New input does not replenish today's single-run allowance.
    assert completion.admission_reason(conn, task_id, limits) == 'completion_daily_run_budget'
    assert completion.admission_reason(conn, task_id, limits, now=10**12) is None
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completion_admission'", (task_id,)).fetchone()[0] == 1


def test_db_task_preserves_prepared_budget_and_claim_cas_without_resetting_attempts(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn, task_id = board
    monkeypatch.setattr(kb, '_fire_kanban_lifecycle_hook', lambda *args, **kwargs: None)
    profile_home = tmp_path / 'worker-profile'
    profile_home.mkdir()
    database = conn.execute('PRAGMA database_list').fetchone()['file']

    def admission_for(actual_task):
        contract = {'version': 'hermes.worker-admission/v1', 'profile_home': str(profile_home),
                    'board_database': kb._database_identity_contract(database)}
        return kb.WorkerAdmission(contract, 'fixture-admission',
                                  kb._task_admission_snapshot(actual_task),
                                  kb._profile_config_digest(str(profile_home)))

    store_prepared_plan(conn, task_id)
    first_task = kb.get_task(conn, task_id)
    assert isinstance(first_task, kb.Task)
    assert json.loads(first_task.worker_budget_json) == {
        'max_children': 3, 'execution_budget': PLAN_BUDGET}
    first_limits = completion.task_limits({}, first_task)
    assert first_limits == {**PLAN_BUDGET, 'budget_scope': 'task:' + task_id}
    first_fingerprint = completion.input_fingerprint(conn, task_id)
    first_admission = admission_for(first_task)
    completion.record_admission(conn, task_id, first_limits)

    # A larger budget is not new substantive work and cannot reuse an admission
    # whose exact Task snapshot was validated before that budget changed.
    changed_budget = {**PLAN_BUDGET, 'max_runs_per_day': 2, 'worker_max_turns': 24}
    store_prepared_plan(conn, task_id, budget=changed_budget)
    conn.execute("UPDATE tasks SET status='ready', consecutive_failures=1 WHERE id=?", (task_id,))
    conn.commit()
    budget_task = kb.get_task(conn, task_id)
    limits = completion.task_limits({}, budget_task)
    assert limits == {**changed_budget, 'budget_scope': 'task:' + task_id}
    assert completion.input_fingerprint(conn, task_id) == first_fingerprint
    assert completion.admission_reason(conn, task_id, limits) == 'completion_repeated_input_budget'
    assert not kb._admission_still_current(budget_task, first_admission)
    assert kb.claim_task(conn, task_id, admission=first_admission, expected_assignee='worker') is None
    assert kb.get_task(conn, task_id).status == 'ready'
    assert conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0] == 0

    # A corrected execution brief can use the second daily slot, after fresh
    # admission; neither the original attempt nor its failure history disappears.
    store_prepared_plan(conn, task_id, budget=changed_budget,
                        brief='Implement the missing route before verifying it, using the existing failure evidence')
    corrected_task = kb.get_task(conn, task_id)
    corrected_limits = completion.task_limits({}, corrected_task)
    corrected_fingerprint = completion.input_fingerprint(conn, task_id)
    assert corrected_fingerprint != first_fingerprint
    assert completion.admission_reason(conn, task_id, corrected_limits) is None
    fresh_admission = admission_for(corrected_task)
    assert kb._admission_still_current(corrected_task, fresh_admission)
    claimed = kb.claim_task(conn, task_id, admission=fresh_admission, expected_assignee='worker')
    assert isinstance(claimed, kb.Task)
    assert completion.task_limits({}, claimed) == corrected_limits
    assert claimed.consecutive_failures == 1
    completion.record_admission(conn, task_id, corrected_limits)
    assert completion.admission_reason(conn, task_id, corrected_limits) == 'completion_daily_run_budget'
    history = [json.loads(row['payload']) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='completion_admission' ORDER BY id", (task_id,))]
    assert [event['input_fingerprint'] for event in history] == [first_fingerprint, corrected_fingerprint]

from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_completion as completion


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    kb.init_db()
    con = kb.connect()
    yield con
    con.close()


def limits(**values):
    return {**completion.DEFAULTS, **values}


def test_no_budget_is_opt_in():
    assert completion.policy({}) == {}
    assert completion.worker_limits({}, {}) == []


def test_invalid_enabled_budget_fails_closed():
    with pytest.raises(ValueError):
        completion.policy({'kanban': {'completion': {'enabled': True, 'max_runs_per_day': 0}}})


def test_repeated_input_survives_process_restart_and_bookkeeping(conn):
    task = kb.create_task(conn, title='bounded task', body='Implement authorized change')
    cap = limits(max_attempts_per_input=2)
    for _ in range(2):
        assert completion.admission_reason(conn, task, cap) is None
        completion.record_admission(conn, task, cap)
    conn.execute("UPDATE tasks SET status='blocked',last_failure_error='new automatic failure' WHERE id=?", (task,))
    conn.commit()
    assert completion.admission_reason(conn, task, cap) == 'completion_repeated_input_budget'
    conn.execute('UPDATE tasks SET body=? WHERE id=?', ('Implement revised approved input', task))
    conn.commit()
    assert completion.admission_reason(conn, task, cap) is None


def test_rolling_budget_counts_other_tasks_but_expires(conn):
    one = kb.create_task(conn, title='one')
    two = kb.create_task(conn, title='two')
    cap = limits(max_runs_per_day=1)
    completion.record_admission(conn, one, cap)
    assert completion.admission_reason(conn, two, cap) == 'completion_daily_run_budget'
    assert completion.admission_reason(conn, two, cap, now=10**12) is None


def test_worker_limits_and_tokens(monkeypatch):
    env = {'HERMES_KANBAN_TASK': 't_test'}
    args = completion.worker_limits(env, limits(worker_total_tokens=100, worker_request_tokens=40))
    assert args == ['--max-turns', '24', '--run-budget', '600']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    agent = SimpleNamespace(session_input_tokens=50, session_output_tokens=10)
    assert completion.token_budget_reason(agent, 35) is None
    assert completion.token_budget_reason(agent, 41) == 'kanban_total_token_budget'
    agent.session_input_tokens = 0
    assert completion.token_budget_reason(agent, 41) == 'kanban_request_token_budget'
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    assert completion.token_budget_reason(agent, 99999) is None


def test_malformed_worker_token_limit_stops_before_model(monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_test')
    monkeypatch.setenv('HERMES_KANBAN_TOTAL_TOKEN_LIMIT', 'bad')
    assert completion.token_budget_reason(SimpleNamespace(), 2) == 'kanban_invalid_token_budget'


def test_worker_request_budget_scales_existing_output_storage(monkeypatch, tmp_path):
    from agent.tool_executor import _budget_for_agent
    from tools.tool_result_storage import maybe_persist_tool_result, extract_persisted_path
    from pathlib import Path
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_synthetic')
    monkeypatch.setenv('HERMES_KANBAN_REQUEST_TOKEN_LIMIT', '32000')
    compressor = SimpleNamespace(context_length=1000000,threshold_tokens=750000,
                                  threshold_tokens_cap=None,_tail_token_budget=100000)
    agent = SimpleNamespace(context_compressor=compressor)
    completion.prepare_request_budget(agent)
    assert compressor.threshold_tokens < 32000
    assert compressor.context_length == 1000000
    budget = _budget_for_agent(agent)
    assert budget.turn_budget < 32000 * 4
    source = 'Full source evidence\n' * 4000
    result = maybe_persist_tool_result(source, 'kanban_show', 'synthetic-context',config=budget)
    path = extract_persisted_path(result)
    assert path and Path(path).read_text() == source
    assert len(result) < len(source)
    # Bounded reads must not enter a persist -> read -> persist loop.
    assert budget.resolve_threshold('read_file') == float('inf')
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    assert _budget_for_agent(agent).default_result_size > budget.default_result_size


def test_compression_cap_never_raises_existing_threshold(monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK','t_test')
    monkeypatch.setenv('HERMES_KANBAN_REQUEST_TOKEN_LIMIT','32000')
    compressor=SimpleNamespace(threshold_tokens=8000,threshold_tokens_cap=8000,_tail_token_budget=1000)
    completion.prepare_request_budget(SimpleNamespace(context_compressor=compressor))
    assert compressor.threshold_tokens == compressor.threshold_tokens_cap == 8000
    assert compressor._tail_token_budget == 1000


def test_tick_receipt_is_zero_model_and_names_root_owner(conn, monkeypatch, tmp_path):
    root = kb.create_task(conn, title='root', assignee='engineering-wren')
    child = kb.create_task(conn, title='child')
    conn.execute("INSERT INTO task_links(parent_id,child_id,edge_role) VALUES(?,?,'dependency')", (root, child))
    conn.commit()
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: tmp_path / 'board.db')
    result = kb.DispatchResult()
    receipt = completion.write_tick_receipt(conn, 'fixture', result, limits())
    assert receipt['routine_model_calls'] == 0
    assert receipt['root_blockers'][0]['owner'] == 'engineering-wren'
    assert receipt['root_blockers'][0]['waiting_descendants'] == 1
    assert (tmp_path / 'completion-status.json').stat().st_mode & 0o777 == 0o600
    next_receipt = completion.write_tick_receipt(conn, 'fixture', result, limits())
    assert next_receipt['evidence_hash'] == receipt['evidence_hash']


def test_budget_attention_deduplicates_and_keeps_task_pending(conn):
    task = kb.create_task(conn, title='bounded', assignee='engineering-wren')
    before = kb.get_task(conn, task).status
    for _ in range(3):
        completion.record_budget_attention(conn, task, 'completion_repeated_input_budget')
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='completion_attention'").fetchone()[0] == 1
    assert kb.get_task(conn, task).status == before


def test_real_dispatch_keeps_bookkeeping_alive_when_execution_budget_spent(conn, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, 'profile_exists', lambda name: True)
    monkeypatch.setattr(kb, '_worker_profile_uses_root_home', lambda name: False)
    monkeypatch.setattr(kb, '_memory_pressure_level', lambda: 'normal')
    monkeypatch.setattr(completion, 'policy', lambda: limits(max_runs_per_day=1))
    spent = kb.create_task(conn, title='prior attempt')
    completion.record_admission(conn, spent, limits())
    task = kb.create_task(conn, title='ready work', assignee='worker', created_by='worker-readiness')
    spawns = []
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: spawns.append(args), max_spawn=1)
    assert not spawns
    assert (task, 'completion_daily_run_budget') in result.respawn_guarded
    assert kb.get_task(conn, task).status == 'ready'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completion_attention'", (task,)).fetchone()[0] == 1


def test_authorization_never_coerces_false_string_or_unscoped_true():
    assert not kb.normalize_authorization_continuity({'authorized': 'false', 'scope': 'fixture'})['authorized']
    assert not kb.normalize_authorization_continuity({'authorized': True})['authorized']
    contract = kb.normalize_authorization_continuity({'authorized': True, 'scope': 'fixture'})
    assert 'authorized_by' not in contract


def test_completed_parent_authorization_does_not_cross_tenants(conn):
    parent = kb.create_task(conn, title='parent', tenant='private')
    kb.complete_task(conn, parent, summary='specified', metadata={'authorization_continuity': {'authorized': True, 'scope': 'private task'}})
    child = kb.create_task(conn, title='child', tenant='business', parents=[parent])
    assert kb.authorization_continuity_for_task(conn, child) is None


def test_needs_input_requires_concrete_new_decision_after_authorization_check(conn, monkeypatch):
    import json
    from tools import kanban_tools as kt
    body = kb.append_authorization_continuity_to_body('Implement the approved local change', {'authorized': True, 'scope': 'local implementation and verification'})
    task = kb.create_task(conn, title='authorized', body=body, assignee='worker')
    monkeypatch.setattr(kt, '_connect', lambda **kwargs: (kb, kb.connect()))
    denied = json.loads(kt._handle_block({'task_id': task, 'kind': 'needs_input', 'reason': 'Need approval to implement'}))
    assert denied.get('ok') is not True
    assert 'recorded scope authorization' in denied['error']
    assert kb.get_task(conn, task).status != 'blocked'


def test_substantive_work_is_not_starved_by_maintenance_limits():
    settings = completion.policy({'kanban': {'completion': {'enabled': True}}})
    assert completion.task_limits(settings, {'id': 'build', 'created_by': 'Chris'}) == {}
    assert completion.task_limits(settings, {'id': 'repair', 'created_by': 'worker-readiness'})['budget_scope'] == 'maintenance'
    settings['task_budgets']['build'] = {'worker_total_tokens': 2000000, 'worker_max_turns': 100}
    actual = completion.task_limits(settings, {'id': 'build', 'created_by': 'Chris'})
    assert actual['worker_total_tokens'] == 2000000
    assert actual['worker_max_turns'] == 100
    assert actual['budget_scope'] == 'task:build'


def test_outcome_candidates_preserve_uncertainty_and_never_auto_publish(conn, tmp_path):
    import json
    task = kb.create_task(conn, title='work')
    kb.complete_task(conn, task, summary='Claimed local result', metadata={'attempted_method': 'narrow local implementation'})
    settings = limits(outcome_candidates_dir=str(tmp_path / 'candidates'))
    assert completion.queue_outcome_candidates(conn, 'fixture', settings) == 1
    assert completion.queue_outcome_candidates(conn, 'fixture', settings) == 0
    records = list((tmp_path / 'candidates/fixture').glob('run-*.json'))
    result = json.loads(records[0].read_text())
    assert result['attempted_method'] == 'narrow local implementation'
    assert result['verification'] == 'needs_source_and_outcome_review'
    assert not result['automatic_acceptance']
    assert result['failure_cause'] == 'not independently established'
    assert records[0].stat().st_mode & 0o777 == 0o600


def test_outcome_cursor_tied_timestamps_survives_retry_and_restart(conn, tmp_path):
    import json
    import time
    task = kb.create_task(conn, title='many completed attempts')
    stamp = int(time.time())
    for n in range(70):
        conn.execute("INSERT INTO task_runs(task_id,started_at,ended_at,status,outcome,summary) VALUES(?,?,?,?,?,?)", (task, stamp-1, stamp, 'completed', 'completed', str(n)))
    conn.commit()
    settings = limits(outcome_candidates_dir=str(tmp_path / 'candidates'))
    assert completion.queue_outcome_candidates(conn, 'fixture', settings) == 4
    folder = tmp_path / 'candidates/fixture'
    # Simulate a crash after receipts were committed but before cursor replacement.
    (folder / 'cursor.json').write_text(json.dumps({'ended_at': stamp, 'id': 0}))
    total = 4
    for _ in range(20):
        total += completion.queue_outcome_candidates(conn, 'fixture', settings)
    assert total == 70
    assert len(list(folder.glob('run-*.json'))) == 70
    assert completion.queue_outcome_candidates(conn, 'fixture', settings) == 0
    assert json.loads((folder / 'cursor.json').read_text())['id'] == conn.execute('SELECT MAX(id) FROM task_runs').fetchone()[0]


def test_default_worker_ignores_old_path_shim_but_preserves_explicit_override(monkeypatch):
    import shutil
    import sys
    monkeypatch.delenv('HERMES_BIN', raising=False)
    monkeypatch.setattr(shutil, 'which', lambda name: '/old/checkout/hermes')
    assert kb._resolve_hermes_argv() == [sys.executable, *(['-P'] if sys.version_info >= (3, 11) else []), '-m', 'hermes_cli.main']
    monkeypatch.setenv('HERMES_BIN', '/operator/approved/hermes')
    assert kb._resolve_hermes_argv() == ['/operator/approved/hermes']

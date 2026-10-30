import json
import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    for key in ('HERMES_PROFILE', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(kt, '_profile_has_kanban_toolset', lambda: True)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def test_same_card_history_is_bounded_and_fully_retrievable_without_context_duplicate(board):
    tid = kb.create_task(board, title='Existing work', body='Keep the original restriction.', assignee='maker', triage=True)
    for index in range(23):
        kb.add_comment(board, tid, author='maker', body=f'Original evidence {index}')
    result = json.loads(kt._handle_show({'task_id': tid}))
    assert result['task']['body'] == 'Keep the original restriction.'
    assert result['worker_context'] is None
    assert len(result['comments']) == 8
    seen = {c['id'] for c in result['comments']}
    while result['history_window']['next_comments_before_id']:
        result = json.loads(kt._handle_show({'task_id': tid, 'comments_before_id': result['history_window']['next_comments_before_id']}))
        ids = {c['id'] for c in result['comments']}
        assert not seen & ids
        seen |= ids
    assert len(seen) == 23
    assert result['preparation']['current'] is None


@pytest.mark.parametrize('extra', [{'history_limit': 0}, {'history_limit': 21}, {'comments_before_id': -1}, {'events_before_id': 0}, {'include_event_payloads': 'true'}])
def test_invalid_history_bounds_rejected(board, extra):
    tid = kb.create_task(board, title='History fixture', assignee='maker', triage=True)
    assert 'error' in json.loads(kt._handle_show({'task_id': tid, **extra}))


def test_prepared_context_keeps_original_scope_and_latest_failure(board, monkeypatch):
    from hermes_cli import owner_work_preparation as prep
    tid = kb.create_task(board, title='Current work', body='Never send external messages.', assignee='maker', triage=True)
    for index in range(20):
        kb.add_comment(board, tid, author='maker', body=f'Old discovery {index}')
    plan = {'brief': 'Implement the missing harness from the retained source.',
            'acceptance': ['One isolated proof'], 'requirements': {'toolsets': ['file']},
            'budget': {'worker_max_turns': 24}}
    monkeypatch.setattr(prep, 'current', lambda c, t: plan)
    context = kb.build_worker_context(board, tid)
    assert context.index(plan['brief']) < context.index('Never send external messages.')
    assert 'Old discovery 19' in context
    assert 'Old discovery 0\n' not in context
    assert '17 earlier comments' in context
    assert 'not new authority' in context


def test_prepared_worker_shows_brief_once_and_can_retrieve_exact_audit_history(board, monkeypatch):
    from hermes_cli import owner_work_preparation as prep
    # Real task, preparation event and run rows. No provider or subprocess runs.
    # Presentation is tested after the existing connection authority boundary.
    monkeypatch.setattr(kt, '_connect', lambda board=None: (kb, kb.connect()))
    original = 'Keep the authorized request and do not send external messages. ' * 30
    tid = kb.create_task(board, title='Implement the remaining harness', body=original,
                         assignee='maker', workspace_kind='dir', workspace_path='/fixture/work', triage=True)
    for index in range(12):
        kb.add_comment(board, tid, author='owner', body=f'Historical evidence {index}: ' + 'retained source details ' * 40)
    brief = 'CURRENT_PREPARED_BRIEF: ' + 'Implement the concrete missing behavior using the retained input. ' * 85
    plan = {'brief': brief, 'acceptance': ['Save and inspect the real artifact.'],
            'requirements': {'toolsets': ['file', 'terminal']}, 'assignee': 'maker', 'skills': [],
            'budget': {'max_runs_per_day': 2, 'max_attempts_per_input': 1, 'worker_max_turns': 24,
                       'worker_run_seconds': 600, 'worker_total_tokens': 256000, 'worker_request_tokens': 64000}}
    with kb.write_txn(board):
        kb._append_event(board, tid, prep.EVENT, {
            'version': 1, 'plan': plan, 'plan_digest': prep._hash(plan),
            'binding_digest': prep._binding(board, tid), 'actor': 'default',
            'reason': 'Continue the existing authorized work.', 'preserved_status': 'triage',
            'readiness': {'local_checks_passed': True, 'claim_time_admission_required': True}})
        run_id = board.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES(?,?,'running',1)",
                               (tid, 'maker')).lastrowid
        board.execute("UPDATE tasks SET status='running',current_run_id=? WHERE id=?", (run_id, tid))
        kb._append_event(board, tid, 'claimed', {'run_id': run_id})
        kb._append_event(board, tid, 'scope_hold', {'instruction': 'Preserve private files; explicit owner release required.'})
    exact_events = {e.id: e.payload for e in kb.list_events(board, tid)}
    monkeypatch.setenv('HERMES_PROFILE', 'maker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id))
    response = kt._handle_show({'task_id': tid, 'board': 'fixture-board'})
    result = json.loads(response)
    assert 'error' not in result, result
    assert result['preparation']['current']['brief'] == brief
    assert response.count('CURRENT_PREPARED_BRIEF:') == 1
    assert result['task']['body'] == original
    assert result['task']['current_run_id'] == run_id
    assert result['history_window']['prepared_worker']
    assert len(result['comments']) == 3
    assert result['history_window']['next_comments_before_id']
    assert result['history_window']['next_events_before_id']
    compact = next(e for e in result['events'] if e['kind'] == prep.EVENT)
    assert compact['id'] in result['history_window']['omitted_event_payload_ids']
    assert compact['payload_omitted'] == ['plan', 'readiness']
    assert compact['full_payload_query']['board'] == 'fixture-board'
    assert compact['payload']['plan_digest'] == prep._hash(plan)
    hold = next(e for e in result['events'] if e['kind'] == 'scope_hold')
    assert hold['payload'] == exact_events[hold['id']]
    expanded = json.loads(kt._handle_show(compact['full_payload_query']))
    assert len(expanded['events']) == 1
    assert expanded['events'][0]['payload'] == exact_events[compact['id']]
    # All previous event payloads remain page-accessible, not just the latest plan.
    seen = set()
    query = {'task_id': tid, 'history_limit': 3, 'include_event_payloads': True}
    while True:
        page = json.loads(kt._handle_show(query))
        for event in page['events']:
            assert event['id'] not in seen
            seen.add(event['id'])
            assert event['payload'] == exact_events[event['id']]
        cursor = page['history_window']['next_events_before_id']
        if cursor is None:
            break
        query['events_before_id'] = cursor
    assert seen == set(exact_events)
    # Stale inherited run identity cannot select the worker-only default.
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id + 1))
    ordinary = json.loads(kt._handle_show({'task_id': tid}))
    assert not ordinary['history_window']['prepared_worker']
    assert len(ordinary['comments']) == 8

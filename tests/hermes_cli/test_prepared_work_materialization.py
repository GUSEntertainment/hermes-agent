"""Native worktree persistence preserves only an unchanged prepared task."""
import json
from pathlib import Path
import socket
import subprocess

import pytest

from hermes_cli import kanban_db as kb, owner_work_preparation as prep


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    monkeypatch.delenv('HERMES_KANBAN_WORKSPACES_ROOT', raising=False)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('Unexpected network'))
    repo = tmp_path / 'project'
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    (repo / 'README.md').write_text('Original project\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test User',
                    '-c', 'user.email=test@example.com', 'commit', '-m', 'Initial'],
                   check=True, capture_output=True)
    conn = kb.connect()
    tid = kb.create_task(conn, title='Complete the authorized implementation',
                         body='Original scope; retain its original delivery and review.',
                         assignee='worker', workspace_kind='worktree', workspace_path=str(repo))
    plan = {'brief': 'Use the existing source and finish its bounded implementation.',
            'acceptance': ['The requested change passes its focused verification.'],
            'requirements': {'toolsets': ['file', 'kanban']},
            'assignee': 'worker', 'skills': [],
            'budget': {'max_runs_per_day': 1, 'max_attempts_per_input': 1,
                       'worker_max_turns': 24, 'worker_run_seconds': 300,
                       'worker_total_tokens': 64000, 'worker_request_tokens': 16000}}
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_budget_json=? WHERE id=?',
                     (json.dumps({'execution_budget': plan['budget'], 'preparation_digest': prep._hash(plan)}), tid))
        kb._append_event(conn, tid, prep.EVENT, {
            'version': 1, 'plan': plan, 'plan_digest': prep._hash(plan),
            'binding_digest': prep._binding(conn, tid), 'readiness': {'offline_only': True},
        })
    assert prep.current(conn, tid) is not None
    try:
        yield conn, tid, repo, plan
    finally:
        conn.close()


def preparation_events(conn, tid):
    return [dict(row) for row in conn.execute(
        'SELECT id,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, prep.EVENT))]


def test_native_materialization_preserves_plan_and_audits_without_rewriting_history(board):
    conn, tid, repo, plan = board
    old_task = kb.get_task(conn, tid)
    old_events = preparation_events(conn, tid)
    persisted, workspace = kb._persist_resolved_worktree_preclaim(conn, old_task, 'ready', board='default')
    assert workspace == (repo / '.worktrees' / tid).resolve()
    assert persisted.workspace_path == str(workspace)
    assert persisted.branch_name == f'wt/{tid}'
    assert persisted.body == old_task.body
    assert persisted.worker_budget_json == old_task.worker_budget_json
    prepared = prep.current(conn, tid)
    assert {key: prepared[key] for key in plan} == plan
    new_events = preparation_events(conn, tid)
    assert new_events[:-1] == old_events
    new_record = json.loads(new_events[-1]['payload'])
    assert new_record['materialization']['prior_event_id'] == old_events[-1]['id']
    assert new_record['plan_digest'] == prep._hash(plan)
    assert new_record['materialization']['source'] == 'native_worktree_preclaim'
    assert kb.claim_task(conn, tid) is not None
    assert prep.current(conn, tid)['plan_digest'] == prep._hash(plan)
    context = kb.build_worker_context(conn, tid)
    assert plan['brief'] in context and old_task.body in context


def test_arbitrary_workspace_edit_stays_stale_after_native_materialization(board):
    conn, tid, repo, _ = board
    old_events = preparation_events(conn, tid)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?',
                     (str(repo / '.worktrees' / 'different-authorized-location'), tid))
    assert prep.current(conn, tid) is None
    persisted, workspace = kb._persist_resolved_worktree_preclaim(
        conn, kb.get_task(conn, tid), 'ready', board='default')
    assert persisted is not None and workspace is not None
    assert prep.current(conn, tid) is None
    assert preparation_events(conn, tid) == old_events


@pytest.mark.parametrize('race', ['status', 'body'])
def test_lost_materialization_cas_does_not_reseal_or_publish_workspace(board, monkeypatch, race):
    conn, tid, repo, _ = board
    original = kb.get_task(conn, tid)
    old_events = preparation_events(conn, tid)
    resolve = kb._resolve_worktree_workspace
    def resolve_with_race(*args, **kwargs):
        result = resolve(*args, **kwargs)
        with kb.write_txn(conn):
            if race == 'status':
                conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            else:
                conn.execute("UPDATE tasks SET body=body || '\nNew controlling instruction' WHERE id=?", (tid,))
        return result
    monkeypatch.setattr(kb, '_resolve_worktree_workspace', resolve_with_race)
    assert kb._persist_resolved_worktree_preclaim(conn, original, 'ready', board='default') == (None, None)
    fresh = kb.get_task(conn, tid)
    assert fresh.workspace_path == original.workspace_path
    assert fresh.branch_name == original.branch_name
    assert preparation_events(conn, tid) == old_events
    assert not (repo / '.worktrees' / tid).exists()
    assert (prep.current(conn, tid) is not None) == (race == 'status')

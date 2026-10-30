"""Independent acceptance checks: authorized work and callback boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'fixture.db'))
    monkeypatch.setenv('HERMES_PROFILE', 'builder')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing() as conn:
        yield kb, conn


@pytest.mark.parametrize('surface', ['tool', 'cli'])
def test_repeat_broad_consent_does_not_park_authorized_implementation(board, monkeypatch, surface):
    from hermes_cli import kanban
    from tools import kanban_tools
    kb, conn = board
    body = kb.append_authorization_continuity_to_body(
        'Fix CSV import preserving quoted commas and run the parser tests.',
        {'authorized': True, 'authorized_by': 'Chris', 'scope': 'local CSV parser implementation and tests'},
    )
    task_id = kb.create_task(conn, title='Fix CSV quoted commas', body=body, assignee='builder')
    task = kb.claim_task(conn, task_id, claimer='builder:fixture')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task_id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    reason = 'Need Chris approval to start implementing the CSV fix.'
    if surface == 'tool':
        result = json.loads(kanban_tools._handle_block({'kind': 'needs_input', 'reason': reason}))
        assert not result.get('ok')
    else:
        result = kanban._cmd_block(SimpleNamespace(task_id=task_id, ids=None, kind='needs_input', reason=[reason]))
        assert result == 1
    assert kb.get_task(conn, task_id).status == 'running'


@pytest.mark.parametrize('operation', ['create', 'link'])
def test_dependency_does_not_authorize_cross_topic_notifications(board, operation):
    kb, conn = board
    parent = kb.create_task(conn, title='Shared CSV format prerequisite', tenant='gus')
    kb.add_notify_sub(conn, task_id=parent, platform='telegram', chat_id='fixture-chat', thread_id='music', delivery_mode='notify+wake')
    if operation == 'create':
        child = kb.create_task(conn, title='Business import fix', tenant='gus', parents=[parent])
    else:
        child = kb.create_task(conn, title='Business import fix', tenant='gus')
        kb.link_tasks(conn, parent, child)
    # A prerequisite link grants no watcher authority for another topic.
    rows = conn.execute('SELECT thread_id,delivery_mode FROM kanban_notify_subs WHERE task_id=?', (child,)).fetchall()
    assert not rows


def test_admission_uses_active_release_outside_repository_cwd(board, tmp_path, monkeypatch):
    from hermes_cli import profiles
    kb, conn = board
    profile = tmp_path / '.hermes' / 'profiles' / 'builder'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  provider: anthropic\n  default: fixture-model\nplatform_toolsets:\n  cli: [file]\n')
    monkeypatch.setattr(profiles, 'resolve_profile_env', lambda name: str(profile))
    task_id = kb.create_task(conn, title='Repair local parser', assignee='builder')
    task = kb.get_task(conn, task_id)
    # The target repository can naturally contain an unrelated agent package.
    workspace = tmp_path / 'target-repository'
    (workspace / 'agent').mkdir(parents=True)
    (workspace / 'agent' / '__init__.py').write_text('raise RuntimeError("target repository package selected instead of active release")\n')
    monkeypatch.chdir(workspace)
    # Even an explicit release PYTHONPATH must not lose to target CWD.
    monkeypatch.setenv('PYTHONPATH', str(Path(kb.__file__).resolve().parents[1]))
    admitted = kb._build_worker_admission(task, 'builder')
    assert admitted.contract['model'] == 'fixture-model'


def test_worker_launch_does_not_execute_target_repository_cli(board, tmp_path, monkeypatch):
    import subprocess
    kb, conn = board
    profile = tmp_path / '.hermes' / 'profiles' / 'builder'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  provider: anthropic\n  default: fixture-model\nplatform_toolsets:\n  cli: [file]\n')
    workspace = tmp_path / 'target-hermes-checkout'
    package = workspace / 'hermes_cli'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'main.py').write_text('from pathlib import Path\nPath("wrong-release-ran").write_text("target checkout CLI executed")\n')
    task_id = kb.create_task(conn, title='Fix source checkout', assignee='builder', workspace_kind='dir', workspace_path=str(workspace))
    task = kb.claim_task(conn, task_id, claimer='builder:fixture')
    monkeypatch.delenv('HERMES_BIN', raising=False)
    # Preserve actual argv/cwd/environment, but keep disposable children joined
    # to the runner so the canonical process guard can guarantee cleanup.
    real_popen = subprocess.Popen
    processes = []
    def joined_popen(*args, **kwargs):
        kwargs['start_new_session'] = False
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', joined_popen)
    kb._default_spawn(task, str(workspace))
    for proc in processes:
        proc.wait(timeout=10)
    assert not (workspace / 'wrong-release-ran').exists(), 'Dispatcher executed CLI from task source instead of active release'


def test_delegated_implementation_can_reach_review_and_completion(board, tmp_path):
    import hashlib
    import os
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract, pass_metadata
    kb, conn = board
    parent_contract = contract(goal_id='csv-owner', objective='Deliver full CSV import project', command_board_flow=True,
        delegation_roles={'maker': 'builder', 'reviewer': 'grace'})
    parent = kb.create_task(conn, title='Deliver reliable CSV import', assignee='scarlett', owner_cell_contract=parent_contract)
    child_contract = contract(goal_id='csv-implementation', objective='Repair quoted cell parser', runtime_profile='builder',
        delegation_roles={'maker': 'builder', 'reviewer': 'grace'})
    child = kb.create_task(conn, title='Fix quoted CSV cells and independently review',
        assignee='builder', parents=[parent], parent_edge_role='delegation_maker',
        owner_cell_contract=child_contract)
    maker = kb.claim_task(conn, child, claimer='builder:csv')
    assert maker is not None
    artifact = tmp_path / 'parser-results.json'
    artifact.write_text(json.dumps({'quoted_comma': ['a,b', 'c'], 'tests': 'passed'}))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert kb.request_review(conn, child, reviewer='grace', summary='CSV fixture and artifact verified',
        metadata={'owner_cell_artifact': {'ref': str(artifact), 'digest': digest}}, expected_run_id=maker.current_run_id)
    # The owner goal remains pending while the declared reviewer does its work.
    assert kb.get_task(conn, parent).status != 'done'
    kb.recompute_ready(conn)
    reviewer = kb.claim_review_task(conn, child, claimer='grace:csv')
    assert reviewer is not None, 'Maker handoff reinstated a circular wait on its still-pending owner'
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    assert kb._set_worker_pid(conn, child, os.getpid())
    assert kb.complete_task(conn, child, summary='Independent parser artifact PASS',
        metadata=pass_metadata(ref=str(artifact), digest=digest), expected_run_id=reviewer.current_run_id,
        review_decision_capability=reviewer.review_decision_capability)
    assert kb.get_task(conn, child).status == 'done'

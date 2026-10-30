"""No providers or live data: real native task/return/custody, isolated owners."""
from contextlib import contextmanager
import json
import sqlite3

import pytest
import yaml

from gateway import app_business_authority as authority
from gateway import josh_board_authority as scope
from gateway import owner_delegation as work
from gateway import owner_agent_creation as creation
from hermes_cli import kanban_db as kb


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(work, 'ROOT', root)
    principals = ('joshgarcia', 'connor', 'newmember')
    catalog = {p: dict(owner=p, profile=p, board=p + '-home', fingerprint=p + '-fixture')
               for p in principals}
    catalog['joshgarcia']['board'] = 'josh-home'  # Existing persisted board name.

    def canonical(principal, anchor):
        assert anchor == root
        if principal not in catalog:
            raise PermissionError('Not enrolled')
        return dict(catalog[principal])

    # Only enrollment is a fixture. Context-bound identity, protected path
    # checks, native task writes, subscriptions and Desk custody are real.
    monkeypatch.setattr(authority, 'canonical_enrollment', canonical)
    monkeypatch.setattr(scope, '_current', lambda p: canonical(p, root))
    root.joinpath('agent-os').mkdir(mode=0o700)
    root.joinpath('profiles').mkdir(mode=0o700)
    with sqlite3.connect(root / 'agent-os/owner-agents.sqlite') as conn:
        conn.execute('CREATE TABLE owner_agents(id TEXT,owner TEXT,name TEXT,purpose TEXT,archivedAt INTEGER,createdAt INTEGER,requestId TEXT,PRIMARY KEY(owner,id))')
        for p in principals:
            for ident in ('scarlett', 'grace'):
                conn.execute('INSERT INTO owner_agents VALUES(?,?,?,?,NULL,1,?)',
                             (ident, p, ident.title(), 'Fixture specialist', 'fixture'))
        conn.execute('INSERT INTO owner_agents VALUES(?,?,?,?,NULL,1,?)',
                     ('foreign-agent', 'connor', 'Foreign', 'Not shared', 'fixture'))
    for profile in ('scarlett', 'grace'):
        folder = root / 'profiles' / profile
        folder.mkdir(mode=0o700)
        folder.joinpath('config.yaml').write_text('{}\n')
    for principal in principals:
        with kb.connect(work.database(catalog[principal]['board'])) as conn:
            pass
    # Native session getter is a dispatcher-owned context, never process env.
    from gateway import session_context
    env = {'HERMES_SESSION_PLATFORM': 'api_server', 'HERMES_SESSION_ID': 'fixture-session',
           'HERMES_SESSION_CHAT_ID': 'fixture-session', 'HERMES_SESSION_KEY': 'fixture-approval'}
    monkeypatch.setattr(session_context, 'get_bound_session_env', env.get)
    return root, catalog, env


@contextmanager
def request(principal, run='fixture-run', message='Make the fixture document.'):
    with scope.execution_scope(principal, principal):
        with work.bind(principal, principal, 'fixture-session', run, message):
            yield


INTENT = {'title': 'Fixture document', 'request': 'Create a short local document.', 'agent': 'scarlett'}


@pytest.mark.parametrize('principal', ('joshgarcia', 'connor', 'newmember'))
def test_native_creation_ready_reviewed_private_and_idempotent(enrolled, principal):
    root, catalog, _env = enrolled
    board = catalog[principal]['board']
    with request(principal):
        created = json.loads(work.create(INTENT))
        repeated = json.loads(work.create(INTENT))
    assert created['accepted'] and not created['duplicate']
    assert repeated['duplicate'] and repeated['task_id'] == created['task_id']
    assert created['board'] == board
    with work._connect(work.database(board)) as conn:
        record, task, goal, binding = work.verify_task(conn, created['task_id'])
        assert task['status'] == 'ready'
        assert task['created_by'] == principal
        assert task['tenant'] == 'app:private:' + principal
        assert task['security_realm'] == 'owner:' + principal
        assert task['assignee'] == 'scarlett'
        assert goal['required_reviewer_profile'] == 'grace'
        assert record['authority'] == catalog[principal]
        assert binding['kind'] == 'owner-delegation-return-v1'
        assert binding['principal'] == binding['profile'] == principal
        sub = dict(conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=?', (created['task_id'],)).fetchone())
        assert sub['user_id'] == sub['notifier_profile'] == principal
        work.verify_return(conn, sub)
        custody = conn.execute('SELECT owner,domain,record_json FROM task_document_custody WHERE task_id=?', (created['task_id'],)).fetchone()
        assert tuple(custody[:2]) == (principal, 'recipient_documents')
        assert json.loads(custody['record_json'])['authority']['realm'] == 'recipient:' + principal
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    policy = work.worker_policy(created['task_id'], board, 'scarlett')
    from gateway.member_worker_policy import effective, PRIVATE_VERSION
    assert policy['version'] == PRIVATE_VERSION and policy['principal'] == principal
    cfg = effective({'mcp_servers': {'host': {}}, 'terminal': {'backend': 'ssh'}, 'toolsets': ['browser']}, policy)
    assert cfg['mcp_servers'] == {}
    assert cfg['terminal']['backend'] == 'local'
    assert cfg['security']['workspace_confinement']['deny_terminal_network'] is True
    with pytest.raises(PermissionError, match='assignment'):
        work.worker_policy(created['task_id'], board, 'grace')


def test_unenrolled_mismatch_revocation_and_foreign_roster_are_denied(enrolled):
    root, catalog, _env = enrolled
    with pytest.raises(PermissionError, match='Not enrolled'):
        with request('not_enrolled'):
            pass
    with scope.execution_scope('connor', 'connor'):
        with pytest.raises(PermissionError, match='Authenticated private owner'):
            with work.bind('newmember', 'newmember', 'fixture-session', 'run', 'request'):
                pass
    with request('newmember'):
        assert {r['id'] for r in json.loads(work.list_agents({}))['agents']} == {'scarlett', 'grace'}
        with pytest.raises(PermissionError, match='sidebar'):
            work.agent('foreign-agent')
        catalog['newmember']['fingerprint'] = 'revoked-fixture'
        with pytest.raises(PermissionError, match='enrollment changed'):
            work.create(INTENT)
    with work._connect(work.database('newmember-home')) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_original_binding_cannot_be_rerouted_or_executed_by_child(enrolled):
    _root, _catalog, _env = enrolled
    from agent.delegation_context import delegated_child_context
    with request('newmember'):
        with delegated_child_context('fixture-child'):
            with pytest.raises(PermissionError):
                work.create(INTENT)
        created = json.loads(work.create(INTENT))
    with work._connect(work.database('newmember-home'), write=True) as conn:
        _record, _task, _goal, binding = work.verify_task(conn, created['task_id'])
        binding['target']['user_id'] = 'connor'
        with pytest.raises(PermissionError, match='return mismatch'):
            work.custody_authority(binding)
        record = conn.execute('SELECT record_json FROM owner_delegated_work WHERE task_id=?', (created['task_id'],)).fetchone()[0]
    # A copied protected row cannot turn another member's DB into its source.
    with work._connect(work.database('connor-home'), write=True) as conn:
        conn.execute('CREATE TABLE owner_delegated_work(task_id TEXT PRIMARY KEY,source_key TEXT UNIQUE,record_json TEXT)')
        conn.execute('INSERT INTO owner_delegated_work VALUES(?,?,?)', (created['task_id'], 'fixture', record))
        with pytest.raises(PermissionError, match='board changed'):
            work.verify_task(conn, created['task_id'])


def test_generic_profile_uses_own_board_never_josh_device(enrolled):
    root, _catalog, _env = enrolled
    ident = 'oa_' + 'a' * 32
    with request('newmember'):
        runtime = creation._profile(ident, 'Specialist', 'Fixture work')
    config = yaml.safe_load((root / 'profiles' / runtime / 'config.yaml').read_text())
    assert runtime.startswith('owner-agent-')
    assert config['owner_agent'] == {'owner': 'newmember', 'id': ident, 'board': 'newmember-home'}
    assert config['terminal'] == {'backend': 'local', 'timeout': 600}
    assert config['owner_device_enabled'] is False
    assert config['security']['workspace_confinement']['deny_terminal_network'] is True
    assert 'josh' not in (root / 'profiles' / runtime / 'config.yaml').read_text().casefold()
    assert work.agent_profile_name('joshgarcia', ident) == 'josh-agent-' + ident[3:]
    assert work.agent_profile_name('connor', ident) != runtime


def test_new_member_confirm_creates_agent_and_ready_task(enrolled):
    root, _catalog, _env = enrolled
    args = dict(name='Specialist', purpose='Fixture work', title='Local fixture', request='Create a short local document.')
    with request('newmember', 'proposal-run'):
        proposal = json.loads(creation.propose(args))
    with request('connor', 'confirmation-run', 'yes'):
        with pytest.raises(creation.ConfirmationDenied, match='No agent proposal'):
            creation.confirm({'proposal_id': proposal['proposal_id']})
    with request('newmember', 'confirmation-run', 'yes'):
        result = json.loads(creation.confirm({'proposal_id': proposal['proposal_id']}))
        selected = work.agent(result['agent_id'])
        assert selected['runtime'] == result['profile']
    assert result['created'] and result['board'] == 'newmember-home'
    with work._connect(work.database('newmember-home')) as conn:
        record, task, goal, binding = work.verify_task(conn, result['task_id'])
        assert task['status'] == 'ready' and task['assignee'] == result['profile']
        assert record['selected']['owner'] == binding['principal'] == 'newmember'
        assert goal['required_reviewer_profile'] == 'grace'


def test_worker_imports_dispatcher_release_instead_of_old_pythonpath(enrolled, monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.delenv('HERMES_BIN', raising=False)
    assert kb._resolve_hermes_argv() == [sys.executable, *kb._python_safe_path_flag(), '-m', 'hermes_cli.main']
    env = dict(os.environ, PYTHONPATH=str(tmp_path / 'obsolete-release'))
    kb._prepend_active_source_to_pythonpath(env)
    expected = Path(work.__file__).resolve()
    probe = subprocess.run(
        [sys.executable, *kb._python_safe_path_flag(), '-c',
         'from gateway import owner_delegation; print(owner_delegation.__file__)'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=True)
    assert Path(probe.stdout.strip()).resolve() == expected
    # Generic admission must not reject unrelated valid native board slugs.
    assert work.worker_policy('fixture', '2026.research', 'scarlett') is None


@pytest.mark.parametrize('principal', ('joshgarcia', 'connor', 'newmember'))
def test_reviewed_app_return_pump_and_read_are_owner_bound(enrolled, principal, monkeypatch):
    import os
    from gateway import owner_app_returns as replies, mirror
    _root, catalog, _env = enrolled
    mirrored = []
    monkeypatch.setattr(mirror, 'mirror_proactive_message', lambda *a, **k: mirrored.append((a, k)))
    with request(principal):
        created = json.loads(work.create(INTENT))
    tid, board = created['task_id'], catalog[principal]['board']
    with work._connect(work.database(board), write=True) as conn:
        maker = kb.claim_task(conn, tid, claimer='scarlett:fixture')
        assert maker is not None
        assert kb.request_review(conn, tid, summary='Fixture production completed.', reviewer='grace',
            expected_run_id=maker.current_run_id,
            metadata={'owner_cell_artifact': {'ref': 'fixture-evidence', 'digest': 'fixture-digest'}})
        reviewer = kb.claim_review_task(conn, tid, claimer='grace:fixture')
        assert reviewer is not None
        kb._set_worker_pid(conn, tid, os.getpid())
        assert work.worker_policy(tid, board, 'grace')['principal'] == principal
        assert kb.complete_task(conn, tid, summary='Independent fixture review passed.',
            expected_run_id=reviewer.current_run_id, review_decision_capability=reviewer.review_decision_capability,
            metadata={'owner_cell_verdict': {'state_version': 1, 'artifact_ref': 'fixture-evidence',
                'artifact_digest': 'fixture-digest', 'verdict': 'PASS', 'defects': []}}, fire_lifecycle_hook=False)
        assert replies.pump(conn) == 1
        assert replies.pump(conn) == 0
        values = replies.read(conn, owner=principal, session_id='fixture-session', run_id='fixture-run')
        assert len(values) == 1 and values[0]['owner'] == principal and values[0]['task_id'] == tid
        with pytest.raises(PermissionError, match='owner required'):
            replies.read(conn, owner='connor' if principal != 'connor' else 'newmember',
                         session_id='fixture-session', run_id='fixture-run')
    assert len(mirrored) == 1 and mirrored[0][1]['session_id'] == 'fixture-session'


def test_delegation_tool_catalog_revalidates_owner_without_cached_grant(enrolled):
    from tools.registry import registry, _check_fn_cached
    from agent.delegation_context import delegated_child_context
    work.register()
    assert _check_fn_cached(work.available) is False
    with request('newmember'):
        assert _check_fn_cached(work.available) is True
        with delegated_child_context('fixture-child'):
            assert _check_fn_cached(work.available) is False
        for name in work.TOOLS:
            entry = next(e for e in registry.get_all_entries() if e.name == name)
            assert entry.toolset == 'owner_delegation' and entry.check_fn is work.available
    assert _check_fn_cached(work.available) is False

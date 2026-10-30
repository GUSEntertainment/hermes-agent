"""Metadata-only repair through native control; no workers, services or models."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import board_owner_manage as owner, kanban_db as kb, owner_cell as oc


@pytest.fixture
def held(tmp_path, monkeypatch):
    for key in ('HERMES_PROFILE', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID',
                'HERMES_SESSION_SOURCE', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_HOME',
                'HERMES_KANBAN_WORKSPACES_ROOT'):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'board.db'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(oc, '_installed_profile', lambda name: name)
    work = home / 'work'
    work.mkdir()
    artifact = work / 'original.txt'
    artifact.write_text('Existing reviewed document; findings remain unresolved.')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    contract = {
        'goal_id': 'metadata-only-fixture', 'objective': 'Review existing artifact',
        'owner_profile': 'steven', 'owner_role_alias': 'legal',
        'runtime_profile': 'legal-drafter', 'required_reviewer_profile': 'steven',
        'security_realm': 'private', 'router_mode': 'reviewed', 'risk_class': 'material',
        'capability_bundle': 'legal',
        'worker_budget': {'producers': 1, 'reviewers': 1, 'extras': 0, 'max_rework_rounds': 2},
        'targets': [], 'acceptance_test': {'expected': 'pass'},
        'rollback': {'procedure_ref': 'fixture://rollback'},
        'callback': {'source_surface': 'test', 'return_surface': 'test', 'report_policy': 'milestones_only'},
        'approval_gates': ['external_send'], 'owner_acceptance': {'mode': 'accountable_owner'},
        'delegation_roles': {'maker': 'legal-drafter', 'reviewer': 'steven'},
    }
    conn = kb.connect()
    tid = kb.create_task(conn, title='Held review with redacted locator', assignee='legal-drafter',
                         workspace_kind='dir', workspace_path=str(work), owner_cell_contract=contract)
    assert kb.request_review(conn, tid, summary='Review exact artifact', reviewer='steven',
                             metadata={'owner_cell_artifact': {'ref': str(artifact), 'digest': digest}})
    oc.record_verdict(conn, tid, reviewer_profile='steven', state_version=1,
                      artifact_ref=str(artifact), artifact_digest=digest,
                      verdict='REQUEST_CHANGES', defects=['Retain this unresolved finding'])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',block_kind='needs_input',consecutive_failures=1 WHERE id=?", (tid,))
        conn.execute("UPDATE owner_cell_goals SET review_loop_count=1,review_artifact_ref='redacted://artifact/063' WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, 'block_loop_detected', {'reason': 'Held old review'})
    kb.add_comment(conn, tid, 'steven', 'Unresolved findings are still binding.')
    yield conn, tid, artifact
    conn.close()


def payload(held, **overrides):
    conn, tid, artifact = held
    native = owner.describe(conn, tid)
    args = {'task_id': tid, 'action': 'repair_review_reference',
            'expected_state': native['expected_state'],
            'reason': 'Correct redacted locator to the original reviewed bytes; preserve the hold.',
            'evidence': {key: native[key] for key in ('expected_goal_sha256', 'expected_artifact_ref', 'expected_artifact_digest')}}
    args['evidence']['artifact_ref'] = str(artifact)
    args.update(overrides)
    return args


def state(conn, tid):
    return {
        'task': dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()),
        'goal': dict(oc.goal_projection(conn, tid)),
        'verdicts': [dict(r) for r in conn.execute('SELECT * FROM review_verdicts WHERE task_id=?', (tid,))],
        'events': [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))],
        'runs': [dict(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=?', (tid,))],
    }


def test_dry_run_leaves_every_field_unchanged(held):
    conn, tid, _ = held
    before = state(conn, tid)
    result = owner.execute(payload(held, dry_run=True))
    assert result['metadata_only'] and result['status'] == 'triage'
    assert not result['dispatch_enabled'] and not result['changed']
    assert state(conn, tid) == before


def test_repair_changes_only_reference_and_audits_without_releasing_review(held):
    conn, tid, artifact = held
    before = state(conn, tid)
    result = owner.execute(payload(held))
    after = state(conn, tid)
    assert result['changed'] and result['metadata_only'] and not result['verdict_recorded']
    assert result['status'] == result['status_preserved'] == 'triage'
    assert not result['dispatch_enabled']
    assert after['task'] == before['task']
    assert after['verdicts'] == before['verdicts'] and after['runs'] == before['runs']
    assert after['goal'] == {**before['goal'], 'review_artifact_ref': str(artifact),
                             'updated_at': after['goal']['updated_at']}
    assert len(after['events']) == len(before['events']) + 1
    event = after['events'][-1]
    assert event['kind'] == 'review_artifact_reference_repaired'
    assert str(artifact) not in event['payload']
    assert not kb.has_spawnable_review(conn)
    with pytest.raises(oc.OwnerCellGuardError, match='PASS'):
        oc.assert_completion_allowed(conn, tid)


@pytest.mark.parametrize('changed', ['task', 'goal', 'digest', 'expected_digest', 'outside',
                                    'symlink_file', 'symlink_directory', 'traversal', 'directory',
                                    'worker_pid', 'claim', 'review_status', 'real_reference'])
def test_refuses_changed_or_unscoped_evidence_atomically(held, tmp_path, changed):
    conn, tid, artifact = held
    args = payload(held)
    if changed == 'task':
        kb.add_comment(conn, tid, 'owner', 'New current direction')
    elif changed == 'goal':
        conn.execute('UPDATE owner_cell_goals SET review_loop_count=2 WHERE task_id=?', (tid,)); conn.commit()
    elif changed == 'digest':
        artifact.write_text('Different bytes')
    elif changed == 'expected_digest':
        args['evidence']['expected_artifact_digest'] = '0' * 64
    elif changed == 'outside':
        outside = tmp_path / 'outside.txt'; outside.write_bytes(artifact.read_bytes())
        args['evidence']['artifact_ref'] = str(outside)
    elif changed == 'symlink_file':
        link = artifact.parent / 'link.txt'; link.symlink_to(artifact)
        args['evidence']['artifact_ref'] = str(link)
    elif changed == 'symlink_directory':
        link = artifact.parent / 'link'; link.symlink_to(artifact.parent, target_is_directory=True)
        args['evidence']['artifact_ref'] = str(link / artifact.name)
    elif changed == 'traversal':
        args['evidence']['artifact_ref'] = str(artifact.parent / '..' / 'work' / artifact.name)
    elif changed == 'directory':
        args['evidence']['artifact_ref'] = str(artifact.parent)
    elif changed == 'worker_pid':
        conn.execute('UPDATE tasks SET worker_pid=123 WHERE id=?', (tid,)); conn.commit();args = payload(held)
    elif changed == 'claim':
        conn.execute("UPDATE tasks SET claim_lock='live' WHERE id=?", (tid,)); conn.commit();args = payload(held)
    elif changed == 'review_status':
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,));conn.commit();args = payload(held)
    else:
        conn.execute('UPDATE owner_cell_goals SET review_artifact_ref=? WHERE task_id=?', (str(artifact),tid));conn.commit();args = payload(held)
    before = state(conn, tid)
    with pytest.raises((ValueError, PermissionError)):
        owner.execute(args)
    assert state(conn, tid) == before


def test_late_file_replacement_rolls_back_metadata(held, monkeypatch):
    from hermes_cli import owner_artifact_reference as repair
    conn, tid, _ = held
    real = repair._artifact_identity
    calls = []
    def changed_on_second(*args):
        value = real(*args);calls.append(value)
        return value if len(calls) == 1 else ('0' * 64, value[1])
    monkeypatch.setattr(repair, '_artifact_identity', changed_on_second)
    before = state(conn, tid)
    with pytest.raises(ValueError, match='changed before'):
        owner.execute(payload(held))
    assert state(conn, tid) == before


def test_audit_failure_rolls_back_reference(held, monkeypatch):
    conn, tid, _ = held
    before = state(conn, tid)
    def fail(*args, **kwargs): raise RuntimeError('audit unavailable')
    monkeypatch.setattr(kb, '_append_event', fail)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        owner.execute(payload(held))
    assert state(conn, tid) == before


@pytest.mark.parametrize('source', ['kanban', 'cron'])
def test_worker_or_schedule_cannot_repair(held, monkeypatch, source):
    conn, tid, _ = held
    before = state(conn, tid)
    monkeypatch.setenv('HERMES_SESSION_SOURCE', source)
    with pytest.raises(PermissionError): owner.execute(payload(held))
    assert state(conn, tid) == before


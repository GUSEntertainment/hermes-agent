"""One-use board identity bridge: isolated local state, no model/network calls."""
import copy

import pytest

from gateway import board_control_transport as transport
from gateway import session_context
from hermes_cli import board_owner_manage


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = tmp_path / 'hermes'
    directory = root / 'private' / 'board-owner-control'
    directory.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(transport, 'ROOT', root)
    transport.key_path().write_bytes(b'k' * 32)
    transport.key_path().chmod(0o600)
    grant = dict(principal='joshgarcia', profile='joshgarcia', board='josh-home', fingerprint='a' * 64)
    monkeypatch.setattr(board_owner_manage, 'require_owner', lambda: dict(grant))
    monkeypatch.setattr(board_owner_manage, 'refresh_member_grant', lambda value: dict(grant), raising=False)
    monkeypatch.setattr(session_context, 'get_bound_session_env',
                        lambda key, *args: 'session-1' if key == 'HERMES_SESSION_ID' else '')
    for key in ('HERMES_KANBAN_TASK', 'HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(transport.time, 'time', lambda: 1000)
    transport.initialize_ledger(transport.ledger_path())
    payload = dict(action='archive', task_id='t_example', board='josh-home',
                   expected_state='b' * 64, reason='obsolete fixture')
    return payload, grant


def test_exact_request_verifies_once_without_mutating_inputs(bridge):
    payload, grant = bridge
    original = copy.deepcopy((payload, grant))
    envelope = transport.issue(payload, grant, 'session-1')
    saved = copy.deepcopy(envelope)
    assert transport.verify(envelope) == (payload, grant)
    assert envelope == saved
    assert (payload, grant) == original
    with pytest.raises(PermissionError, match='already consumed'):
        transport.verify(envelope)


@pytest.mark.parametrize('field,value', [
    ('payload', {'board': 'chris-home'}), ('grant', {'principal': 'chris'}),
    ('session_id', 'other-session'), ('purpose', 'different-purpose'),
    ('expires_at', 9000), ('nonce', 'f' * 32), ('payload_sha256', 'c' * 64),
])
def test_envelope_tampering_is_rejected_without_consuming(bridge, field, value):
    envelope = transport.issue(*bridge, 'session-1')
    altered = dict(envelope, **{field: value})
    with pytest.raises(PermissionError):
        transport.verify(altered)
    assert transport.verify(envelope) == bridge


def test_expired_and_future_requests_fail(bridge, monkeypatch):
    envelope = transport.issue(*bridge, 'session-1')
    for now in (999, 1030):
        monkeypatch.setattr(transport.time, 'time', lambda: now)
        with pytest.raises(PermissionError, match='expired or not yet valid'):
            transport.verify(envelope)


def test_revocation_and_other_board_fail(bridge, monkeypatch):
    envelope = transport.issue(*bridge, 'session-1')
    monkeypatch.setattr(board_owner_manage, 'refresh_member_grant',
                        lambda grant: dict(grant, fingerprint='c' * 64))
    with pytest.raises(PermissionError, match='enrollment changed'):
        transport.verify(envelope)
    with pytest.raises(PermissionError, match='enrolled board'):
        transport.issue(dict(bridge[0], board='chris-home'), bridge[1], 'session-1')


def test_enrollment_refresh_cannot_extend_request_lifetime(bridge, monkeypatch):
    envelope = transport.issue(*bridge, 'session-1')
    def refresh(grant):
        monkeypatch.setattr(transport.time, 'time', lambda: 1031)
        return dict(grant)
    monkeypatch.setattr(board_owner_manage, 'refresh_member_grant', refresh)
    with pytest.raises(PermissionError, match='expired before admission'):
        transport.verify(envelope)


def test_request_identity_cannot_come_from_environment(bridge, monkeypatch):
    monkeypatch.setattr(session_context, 'get_bound_session_env', lambda *args: '')
    monkeypatch.setenv('HERMES_SESSION_ID', 'session-1')
    monkeypatch.setenv('HERMES_PROFILE', 'joshgarcia')
    with pytest.raises(PermissionError):
        transport.issue(*bridge, 'session-1')


def test_other_session_or_grant_cannot_be_signed(bridge):
    with pytest.raises(PermissionError, match='session mismatch'):
        transport.issue(*bridge, 'other-session')
    with pytest.raises(PermissionError, match='current member'):
        transport.issue(bridge[0], dict(bridge[1], principal='chris'), 'session-1')


@pytest.mark.parametrize('context', ['worker', 'cron', 'child'])
def test_workers_and_children_cannot_issue_or_use_requests(bridge, monkeypatch, context):
    envelope = transport.issue(*bridge, 'session-1')
    if context == 'worker':
        monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    elif context == 'cron':
        monkeypatch.setenv('HERMES_SESSION_SOURCE', 'cron')
    else:
        from agent import delegation_context
        monkeypatch.setattr(delegation_context, 'is_delegated_child_context', lambda: True)
    with pytest.raises(PermissionError, match='direct member'):
        transport.issue(*bridge, 'session-1')
    with pytest.raises(PermissionError, match='direct member'):
        transport.verify(envelope)


def test_missing_key_or_ledger_is_not_created(bridge):
    envelope = transport.issue(*bridge, 'session-1')
    transport.ledger_path().unlink()
    with pytest.raises((PermissionError, FileNotFoundError)):
        transport.verify(envelope)
    assert not transport.ledger_path().exists()
    transport.key_path().unlink()
    with pytest.raises((PermissionError, FileNotFoundError)):
        transport.issue(*bridge, 'session-1')
    assert not transport.key_path().exists()


def test_symlink_or_nonprivate_key_fails(bridge, tmp_path):
    transport.key_path().chmod(0o644)
    with pytest.raises(PermissionError):
        transport.issue(*bridge, 'session-1')
    transport.key_path().unlink()
    other = tmp_path / 'other-key'
    other.write_bytes(b'k' * 32)
    other.chmod(0o600)
    transport.key_path().symlink_to(other)
    with pytest.raises(PermissionError):
        transport.issue(*bridge, 'session-1')


def test_oversize_extra_fields_and_nonfinite_payload_fail(bridge):
    envelope = transport.issue(*bridge, 'session-1')
    for altered in (dict(envelope, actor='chris'), dict(envelope, payload='x' * 40000)):
        with pytest.raises(PermissionError):
            transport.verify(altered)
    with pytest.raises(PermissionError):
        transport.issue(dict(bridge[0], bad=float('nan')), bridge[1], 'session-1')


def test_installer_refuses_replacing_existing_ledger(bridge):
    with pytest.raises(FileExistsError):
        transport.initialize_ledger(transport.ledger_path())


def test_signed_overlong_lifetime_is_still_rejected(bridge):
    envelope = transport.issue(*bridge, 'session-1')
    unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
    unsigned['expires_at'] = 1031
    altered = dict(unsigned, signature=transport._sign(unsigned, b'k' * 32))
    with pytest.raises(PermissionError, match='lifetime'):
        transport.verify(altered)

"""Historical profile stamps do not disable otherwise authorized native work."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from plugins.capability_projection import resolver
from plugins.capability_projection.admission import preflight_task
from plugins.capability_projection.policy import (
    ApprovalError, CapabilityContract, HmacApprovalVerifier, issue_test_approval,
)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    import hermes_constants

    home = tmp_path / '.hermes'
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: home)
    profile = home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  default: fixture\n')
    (profile / 'SOUL.md').write_text('A careful specialist.\n')
    workspace = tmp_path / 'work' / 'task'
    workspace.mkdir(parents=True)
    fingerprint = resolver.profile_config_fingerprint('worker')
    matrix = home / 'policy' / 'capability-profiles.json'
    matrix.parent.mkdir()
    matrix.write_text(json.dumps({'schema_version': 1, 'profiles': [{
        'name': 'worker', 'profile_fingerprint': fingerprint,
        'allowed_capabilities': ['file_write_workspace'], 'role': 'specialist',
        'privacy_tier': 'internal', 'data_realms': ['gus'],
        'workspace_roots': [str(workspace.parent)], 'toolsets': ['file'],
        'skills': ['writing'], 'mcp_families_read': [], 'mcp_families_mutation': [],
    }]}))
    matrix.chmod(0o600)
    contract = CapabilityContract(
        required_capabilities=('file_write_workspace',),
        forbidden_capabilities=('localhost_bind', 'terminal_external_egress',
                                'outbound_network_read', 'external_mutation'),
        workspace_path=str(workspace), data_realm='gus', tool_needs=('file',),
        required_skills=('writing',), scope_id='task-scope-fixture-001',
    )
    return profile, matrix, fingerprint, contract


def admission(matrix, contract):
    return preflight_task(contract.as_dict(), assignee='worker',
                          authoritative_workspace=contract.workspace_path,
                          inventory=resolver.load_capability_inventory(matrix))


@pytest.mark.parametrize('filename,addition', [
    ('SOUL.md', '\nWrite concise progress updates.\n'),
    ('config.yaml', '\n# Clarify the existing model selection.\n'),
])
def test_routine_profile_edit_routes_with_current_identity_without_resealing(installed, filename, addition):
    profile, matrix, previous, contract = installed
    before_matrix = matrix.read_bytes()
    assert admission(matrix, contract).may_spawn
    path = profile / filename
    path.write_text(path.read_text() + addition)
    result = admission(matrix, contract)
    assert result.may_spawn
    assert result.result.profile_fingerprint == resolver.profile_config_fingerprint('worker')
    assert result.result.profile_fingerprint != previous
    assert matrix.read_bytes() == before_matrix


def test_unavailable_profile_still_denied(installed):
    profile, matrix, _, contract = installed
    profile.rename(profile.with_name('removed-worker'))
    result = admission(matrix, contract)
    assert not result.may_spawn
    assert result.result.diagnosis_code == 'unknown_assignee'


@pytest.mark.parametrize('change,diagnosis', [
    ({'required_capabilities': ('file_write_workspace', 'terminal')}, 'no_safe_profile'),
    ({'data_realm': 'family'}, 'data_realm_mismatch'),
    ({'workspace_path': '/outside-company-boundary/task'}, 'workspace_root_mismatch'),
    ({'tool_needs': ('browser',)}, 'required_toolset_unavailable'),
])
def test_stale_stamp_never_broadens_declared_authority(installed, change, diagnosis):
    profile, matrix, _, contract = installed
    before_matrix = matrix.read_bytes()
    (profile / 'SOUL.md').write_text('Updated instructions.\n')
    result = admission(matrix, replace(contract, **change))
    assert not result.may_spawn
    assert result.result.diagnosis_code == diagnosis
    assert matrix.read_bytes() == before_matrix


def test_policy_file_ownership_boundary_preserved(installed):
    _, matrix, _, _ = installed
    matrix.chmod(0o666)
    with pytest.raises(ApprovalError, match='group/world writable'):
        resolver.load_capability_inventory(matrix)


@pytest.mark.parametrize('identity', ['assignee', 'task', 'host'])
def test_existing_signed_approval_cannot_follow_changed_execution_identity(installed, identity):
    _, _, _, base = installed
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    secret = b'local-test-only-approval-secret-32-bytes'
    contract = replace(base, required_capabilities=('localhost_bind',),
                       forbidden_capabilities=(), network_mode='localhost_only')
    signed = issue_test_approval(
        issuer='fixture', secret=secret, scope_id=contract.scope_id,
        assigned_profile='worker', capabilities=('localhost_bind',),
        workspace_path=contract.workspace_path, execution_host='fixture-host',
        bind_hosts=('127.0.0.1',), issued_at=now, expires_at=now + timedelta(minutes=5),
    )
    verifier = HmacApprovalVerifier({'fixture': secret}, now=lambda: now)
    verifier.verify(signed, contract, assigned_profile='worker', execution_host='fixture-host')
    if identity == 'task':
        contract = replace(contract, scope_id='task-scope-fixture-002')
    with pytest.raises(ApprovalError, match='does not match'):
        verifier.verify(signed, contract,
                        assigned_profile='different-worker' if identity == 'assignee' else 'worker',
                        execution_host='different-host' if identity == 'host' else 'fixture-host')

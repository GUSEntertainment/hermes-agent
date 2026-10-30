"""Real registry regressions: shared registrations are not run grants."""
from unittest.mock import Mock
import pytest
from gateway import josh_pilot_tools as pilot, owner_api_intake as intake
from gateway import owner_device_tools as device, josh_board_authority as authority
from gateway import member_board_tools as member
from hermes_cli import board_owner_manage as control
from tools.registry import registry
import model_tools

BASE = {pilot.TOOL, *member.TOOLS}

@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.delenv('HERMES_SESSION_SOURCE', raising=False)
    monkeypatch.setattr(authority, 'app_binding', lambda: None)
    monkeypatch.setattr(pilot, 'pilot_required', lambda: True)
    monkeypatch.setattr(device, 'enabled', lambda: False)
    monkeypatch.setattr(control, 'require_owner', lambda: {
        'principal': 'fixture-member', 'profile': 'fixture-member',
        'board': 'fixture-board', 'fingerprint': 'current-fixture-enrollment'})
    token = intake._ACTIVE.set(None)
    pilot.register()
    member.register(pilot.TOOLSET)
    yield
    intake._ACTIVE.reset(token)


def names():
    return {t['function']['name'] for t in model_tools.get_tool_definitions([pilot.TOOLSET], quiet_mode=True)}


def test_stray_global_registration_is_not_advertised(scoped):
    registry.register(name='fixture_chris_only', toolset=pilot.TOOLSET,
        schema={'name': 'fixture_chris_only', 'description': 'Fixture', 'parameters': {'type': 'object', 'properties': {}}},
        handler=lambda args, **kw: 'not callable')
    try:
        assert names() == BASE
    finally:
        registry.deregister('fixture_chris_only')


def test_intake_active_then_inactive_same_process(scoped):
    token = intake._ACTIVE.set(object())
    try:
        pilot.prepare()
        assert names() == BASE | {'owner_secure_intake', 'owner_password_reset'}
    finally:
        intake._ACTIVE.reset(token)
    pilot.prepare()
    assert names() == BASE


def test_device_registration_does_not_break_next_owner(scoped, monkeypatch):
    monkeypatch.setattr(device, 'enabled', lambda: True)
    pilot.prepare()
    assert names() == BASE | {device.TOOL}
    monkeypatch.setattr(device, 'enabled', lambda: False)
    pilot.prepare()
    assert names() == BASE


def test_permitted_names_read_once_before_computation(scoped, monkeypatch):
    allowed = Mock(side_effect=[BASE, BASE | {'unavailable'}])
    monkeypatch.setattr(pilot, 'allowed_names', allowed)
    assert names() == BASE
    assert allowed.call_count == 1


def test_missing_required_tool_fails_with_names_only(scoped, monkeypatch):
    monkeypatch.setattr(model_tools, '_compute_tool_definitions', lambda *a, **kw: [])
    with pytest.raises(PermissionError) as error:
        names()
    assert 'josh_task_status' in str(error.value)
    assert 'own_work_list' in str(error.value)


def test_schema_scope_snapshot_resets_and_does_not_grant_dispatch(scoped, monkeypatch):
    with pilot.schema_scope():
        assert pilot.schema_names() == BASE
        monkeypatch.setattr(device, 'enabled', lambda: True)
        assert pilot.schema_names() == BASE
        assert pilot.allowed_names() == BASE | {device.TOOL}
    assert pilot.schema_names() == BASE | {device.TOOL}


def test_assert_schemas_reports_symmetric_difference(scoped):
    with pytest.raises(PermissionError) as error:
        pilot.assert_schemas([{'function': {'name': 'fixture_extra', 'description': 'PRIVATE_SENTINEL'}}])
    assert all(name in str(error.value) for name in BASE | {'fixture_extra'})
    assert 'PRIVATE_SENTINEL' not in str(error.value)

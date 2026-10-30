"""Exercise policy-backed packages against real temporary profile files."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def routing(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    registry_path = root / 'gateway/policies/role-registry.yaml'
    registry = yaml.safe_load(registry_path.read_text())
    home = tmp_path / 'home'
    policy = registry['model_policy']
    for profile in registry['kanban_assignment_policy']['spawnable_profiles']:
        folder = home if profile == 'default' else home / 'profiles' / profile
        folder.mkdir(parents=True, exist_ok=True)
        role = policy['profiles'].get(profile)
        (folder / 'config.yaml').write_text(yaml.safe_dump({'model': {'default': policy['models'][role] if role else 'separate-lane', 'provider': policy['provider']}}))
    board = home / 'kanban/boards/chris-home'
    board.mkdir(parents=True)
    (board / 'kanban.db').touch()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_ROLE_REGISTRY', str(registry_path))
    spec = importlib.util.spec_from_file_location('tested_margot_model_policy_helper', root / 'scripts/safe_delegate_card.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, policy, home


def package_for(module, owner, department, **overrides):
    argv = ['--board', 'chris-home', '--tenant', 'synthetic', '--source-surface', 'telegram:100', '--return-surface', 'telegram:100', '--department', department, '--title', 'Prepare a bounded draft artifact', '--body', 'Produce a synthetic local artifact and verification evidence.', '--complexity', 'complex', '--package-author', owner, '--package-author-tier', 'astra']
    args = module.parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    route = module.resolve_route(args)
    return module.build_delegation_package(args, route), route


@pytest.mark.parametrize('owner', ['lauren','maya','steven','geneva','riley','scarlett','dakota','harper','julia','sloane','parker'])
def test_head_packages_own_work_and_worker_cannot_self_accept(routing, owner):
    module, policy, _ = routing
    department, worker = module.SOL_OWNER_TERRA_MAKER_DEPARTMENTS[owner]
    package, route = package_for(module, owner, department)
    assert module.validate_delegation_package(package, route) == []
    assignments = package['model_routing']['assignments']
    for phase, profile, role in [('planner',owner,'department_owner'),('executor',worker,'worker'),('reviewer',owner,'department_owner')]:
        assert assignments[phase] == {'profile':profile,'role':role,'model':policy['models'][role],'provider':policy['provider']}
    assert package['routing_contract']['required_acceptor_profile'] == owner != worker
    assert package['routing_contract']['max_child_depth'] == 0
    assert package['irreversible_external_action_gates']['review_authorizes_action'] is False
    assert module.delegation_callback_payload(package)['model_routing'] == package['model_routing']
    if department in module.FABLE_SECONDARY_REVIEW_DEPARTMENTS:
        assert package['required_secondary_reviewer_profile'] == 'fable-review'


def test_unrelated_head_cannot_satisfy_senior_package_gate(routing):
    module, _, _ = routing
    with pytest.raises(ValueError, match='package author'):
        package_for(module, 'lauren', 'engineering_systems')


def test_profile_model_drift_is_rejected_before_dispatch(routing):
    module, _, home = routing
    (home/'profiles/engineering-wren/config.yaml').write_text('model:\n  provider: openai-codex\n  default: wrong-model\n')
    with pytest.raises(ValueError, match='model policy drift'):
        package_for(module, 'scarlett', 'engineering_systems')


def test_mutated_model_receipt_is_rejected(routing):
    module, _, _ = routing
    package, route = package_for(module, 'scarlett', 'engineering_systems')
    package['model_routing']['assignments']['reviewer']['model'] = 'worker-model'
    assert any('model routing' in error for error in module.validate_delegation_package(package, route))


def test_downgraded_critical_output_cannot_be_approved(routing):
    module, _, _ = routing
    with pytest.raises(ValueError, match='cannot approve'):
        package_for(module, 'scarlett', 'engineering_systems', complexity='critical', fallback_degraded_mode=True, approval_status='approved')


def test_legacy_persisted_packages_remain_compatible(routing, monkeypatch):
    module, _, _ = routing
    registry = copy.deepcopy(module.load_registry())
    registry.pop('model_policy')
    monkeypatch.setattr(module, 'load_registry', lambda *args: registry)
    package, route = package_for(module, 'claire', 'engineering_systems')
    assert 'model_routing' not in package
    assert module.validate_delegation_package(package, route) == []


@pytest.mark.parametrize('topic', [9,11,13,15,17,19,21,23,25,27,29])
def test_canonical_topic_reaches_head_before_worker_execution(routing, topic):
    from gateway.department_topic_router import CANONICAL_MARGOT_OS_CHAT_ID, dispatch_department_topic, build_safe_delegate_command
    module, policy, _ = routing
    outcome = dispatch_department_topic(chat_id=CANONICAL_MARGOT_OS_CHAT_ID, thread_id=topic, message_id=100000+topic, message_text='Prepare a synthetic draft artifact for review.')
    command = build_safe_delegate_command(outcome.envelope, board='chris-home')
    args = module.parse_args([x for x in command[2:] if x != '--execute'])
    route = module.resolve_route(args)
    package = module.build_delegation_package(args, route)
    assert module.validate_delegation_package(package, route) == []
    assert policy['profiles'][route.assignee] == 'department_owner'
    assert package['model_routing']['assignments']['executor']['model'] == policy['models']['department_owner']
    assert package['irreversible_external_action_gates']['review_authorizes_action'] is False
    assert set(outcome.envelope['approval_gates']).issubset(package['irreversible_external_action_gates']['gates'])

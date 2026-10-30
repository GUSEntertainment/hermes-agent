"""Prepared workers use a lean subset of their existing tool authority."""
from copy import deepcopy

import pytest

from hermes_cli.owner_work_preparation import select_toolsets


def test_selects_declared_and_mcp_names_without_unrelated_configured_tools():
    available = ['file', 'terminal', 'browser', 'kanban', 'skills', 'github', 'email']
    requirements = {'toolsets': ['file', 'file'], 'mcp': {'github': ['search']}}
    original = deepcopy(requirements)
    assert select_toolsets(available, requirements) == ['file', 'github', 'kanban', 'skills']
    assert requirements == original
    assert available == ['file', 'terminal', 'browser', 'kanban', 'skills', 'github', 'email']


@pytest.mark.parametrize('requirements, expected', [
    ({'commands': ['python -m pytest']}, ['terminal']),
    ({'terminal_network': True}, ['terminal']),
    ({'localhost_server': True}, ['terminal']),
    ({'browser_session': True}, ['browser']),
    ({'commands': ['curl localhost'], 'browser_session': True}, ['browser', 'terminal']),
])
def test_infers_only_tools_required_by_declared_work(requirements, expected):
    assert select_toolsets(['file', 'terminal', 'browser'], requirements) == expected


@pytest.mark.parametrize('available, expected', [
    ([], []), (['kanban'], ['kanban']), (['skills'], ['skills']),
    (['terminal', 'skills', 'kanban'], ['kanban', 'skills']),
])
def test_control_and_loading_tools_are_optional_existing_authority(available, expected):
    assert select_toolsets(available, {'terminal_network': False, 'localhost_server': False,
                                     'browser_session': False}) == expected


@pytest.mark.parametrize('requirements, missing', [
    ({'toolsets': ['terminal']}, 'terminal'),
    ({'mcp': {'private_store': ['read']}}, 'private_store'),
    ({'commands': ['python report.py']}, 'terminal'),
    ({'terminal_network': True}, 'terminal'),
    ({'localhost_server': True}, 'terminal'),
    ({'browser_session': True}, 'browser'),
])
def test_missing_authority_fails_before_worker_start(requirements, missing):
    with pytest.raises(ValueError, match='unavailable toolsets: ' + missing) as exc:
        select_toolsets(['file', 'kanban', 'skills'], requirements)
    assert 'before starting' in str(exc.value)


def test_missing_set_is_sorted_and_does_not_select_a_tool_alias():
    with pytest.raises(ValueError, match='unavailable toolsets: browser, github'):
        select_toolsets(['mcp_github', 'browser_use'],
                        {'toolsets': ['github'], 'browser_session': True})


@pytest.mark.parametrize('available, requirements', [
    ('terminal', {}), ([None], {}), ([''], {}),
    ([], None), ([], {'toolsets': 'terminal'}),
    ([], {'mcp': ['github']}), ([], {'mcp': {'': []}}),
    ([], {'commands': 'python'}), ([], {'commands': ['']}),
    ([], {'terminal_network': 'false'}), ([], {'localhost_server': 1}),
    ([], {'browser_session': None}),
])
def test_malformed_selector_input_fails_closed(available, requirements):
    with pytest.raises(ValueError):
        select_toolsets(available, requirements)


def test_deterministic_order_and_no_implicit_file_permission():
    assert select_toolsets({'skills', 'kanban', 'terminal'},
                           {'commands': ['python'], 'read_paths': ['/work/input']}) == [
                               'kanban', 'skills', 'terminal']

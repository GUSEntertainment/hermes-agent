"""Synthetic-only regression for multiplex terminal selection. No network."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.josh_board_authority import _execution
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools import terminal_tool as tt


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, '_terminal_config_bridge_attempted', True)
    monkeypatch.setattr(tt, '_private_worker_environment', lambda: None)
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setenv('TERMINAL_SSH_HOST', 'ambient-owner.invalid')
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    root = tmp_path / 'homes'
    for name in ('connor', 'josh'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('terminal:\n  backend: ssh\n  ssh_host: '+name+'.invalid\n  ssh_user: synthetic\n  cwd: "~"\n')
    return root


def scoped(root, name, call, principal=None):
    token = _execution.set((name, name if principal is None else principal))
    home = set_hermes_home_override(root / 'profiles' / name)
    try:
        return call()
    finally:
        reset_hermes_home_override(home)
        _execution.reset(token)


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_caller_backend_never_uses_ambient_owner(lanes, name):
    config = scoped(lanes, name, tt._get_env_config)
    assert config['env_type'] == 'ssh'
    assert config['ssh_host'] == name + '.invalid'
    assert config['cwd'] == '~'
    assert os.environ['TERMINAL_ENV'] == 'local'


def test_caller_cache_is_separate_even_same_task_id(lanes):
    a = scoped(lanes, 'connor', lambda: tt._resolve_container_task_id('same'))
    b = scoped(lanes, 'josh', lambda: tt._resolve_container_task_id('same'))
    assert a != b
    assert a != 'default' and b != 'default'


def test_unknown_does_not_fall_back_to_local(lanes):
    with pytest.raises(PermissionError):
        scoped(lanes, 'unknown', tt._get_env_config)


def test_missing_remote_config_does_not_fall_back(lanes):
    (lanes / 'profiles' / 'connor' / 'config.yaml').unlink()
    with pytest.raises(PermissionError):
        scoped(lanes, 'connor', tt._get_env_config)


def test_parallel_backend_selection(lanes):
    def call(name):
        return scoped(lanes, name, tt._get_env_config)['ssh_host']
    names = ['connor', 'josh'] * 10
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(call, names)) == [n+'.invalid' for n in names]


def test_owner_local_still_available(lanes):
    token = _execution.set((None, 'chris'))
    try:
        assert tt._get_env_config()['env_type'] == 'local'
    finally:
        _execution.reset(token)


@pytest.mark.parametrize('name', ['connor', 'josh', 'unknown'])
def test_real_native_file_read_cannot_reuse_owner_environment(lanes, tmp_path, monkeypatch, name, record_property):
    from tools import file_tools as ft
    sentinel = tmp_path / 'owner-sentinel.txt'
    marker = 'SYNTHETIC-CALLER-ISOLATION-TEST-ONLY'
    sentinel.write_text(marker)
    token = _execution.set((None, 'chris'))
    try:
        owner_result = json.loads(ft.read_file_tool(str(sentinel), task_id='owner-turn'))
        assert marker in owner_result['content']
    finally:
        _execution.reset(token)
    calls = []
    def unavailable_remote(env_type, *args, **kwargs):
        calls.append(env_type)
        assert kwargs['ssh_config']['sync_files'] is False
        assert kwargs['ssh_config']['host'] == name + '.invalid'
        raise ConnectionError('Synthetic remote deliberately unavailable; no network attempted')
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', unavailable_remote)
    result = scoped(lanes, name, lambda: ft.read_file_tool(str(sentinel), task_id=name+'-turn'))
    record_property('native_receipt', json.dumps({'caller': name, 'tool': 'read_file_tool', 'owner_result': owner_result, 'caller_result': json.loads(result), 'backend_creation_attempts': calls, 'remote_transport': 'deliberately unavailable test double; no network', 'live_gateway': False}))
    assert marker not in result
    assert 'error' in json.loads(result)
    assert calls == ([] if name == 'unknown' else ['ssh'])
    if name != 'unknown':
        assert 'Synthetic remote deliberately unavailable' in result


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_remote_native_tools_remain_eligible(lanes, name):
    from gateway.host_tool_authority import tool_allowed
    for tool in ['terminal', 'read_file', 'write_file', 'patch', 'search_files', 'execute_code', 'web_search', 'web_extract', 'todo']:
        assert scoped(lanes, name, lambda: tool_allowed(tool))


@pytest.mark.parametrize('name', ['connor', 'josh', 'unknown'])
@pytest.mark.parametrize('tool', ['browser_navigate', 'process', 'delegate_task', 'cronjob', 'vision_analyze', 'session_search', 'skill_view', 'mcp__filesystem__read_file', 'mcp__filesystem__read_resource', 'unreviewed_plugin'])
def test_ambient_routes_withheld_at_dispatch(lanes, name, tool):
    from model_tools import handle_function_call
    result = scoped(lanes, name, lambda: handle_function_call(tool, {}))
    assert json.loads(result) == {'error': 'Tool unavailable'}


def test_remote_key_changes_with_configuration(lanes):
    first = scoped(lanes, 'connor', lambda: tt._resolve_container_task_id('same'))
    config = lanes / 'profiles' / 'connor' / 'config.yaml'
    config.write_text(config.read_text().replace('connor.invalid', 'second.invalid'))
    second = scoped(lanes, 'connor', lambda: tt._resolve_container_task_id('same'))
    assert first != second


def test_remote_bridge_never_mutates_process_environment(lanes, monkeypatch):
    monkeypatch.setattr(tt, '_terminal_config_bridge_attempted', False)
    before = dict(os.environ)
    scoped(lanes, 'connor', tt._ensure_terminal_env_bridged)
    assert dict(os.environ) == before
    assert not tt._terminal_config_bridge_attempted


def test_host_sync_and_ambient_key_are_not_inherited(lanes, monkeypatch):
    monkeypatch.setenv('TERMINAL_SSH_KEY', '/synthetic/owner-key-never-read')
    monkeypatch.setenv('TERMINAL_SSH_SYNC_FILES', 'true')
    config = scoped(lanes, 'connor', tt._get_env_config)
    assert config['ssh_key'] == ''
    assert config['ssh_sync_files'] is False


def test_code_rpc_thread_keeps_caller_scope(lanes):
    from tools.thread_context import propagate_context_to_thread
    def submit():
        target = propagate_context_to_thread(tt._get_env_config)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(target).result()
    assert scoped(lanes, 'connor', submit)['ssh_host'] == 'connor.invalid'


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_code_environment_uses_scoped_ssh_without_host_sync(lanes, monkeypatch, name):
    from tools.code_execution_tool import _get_or_create_env
    created = []
    class SyntheticRemote:
        pass
    remote = SyntheticRemote()
    def create(**kwargs):
        created.append(kwargs)
        return remote
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', create)
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    env, kind = scoped(lanes, name, lambda: _get_or_create_env('code-turn'))
    assert env is remote and kind == 'ssh'
    assert len(created) == 1
    assert created[0]['ssh_config']['host'] == name + '.invalid'
    assert created[0]['ssh_config']['sync_files'] is False


def test_owner_named_department_lane_is_not_revoked(lanes):
    from gateway.host_tool_authority import tool_allowed
    assert scoped(lanes, 'scarlett', lambda: tool_allowed('terminal'), principal='chris')


@pytest.mark.parametrize('path', ['~/allowed.txt', 'relative/allowed.txt', '/remote/allowed.txt'])
def test_scoped_remote_paths_never_use_host_home(lanes, path):
    from tools.file_tools import _resolve_path_for_task
    assert str(scoped(lanes, 'connor', lambda: _resolve_path_for_task(path))) == path


def test_scoped_remote_path_does_not_dereference_host_symlink(lanes, tmp_path):
    from tools.file_tools import _resolve_path_for_task
    target = tmp_path / 'owner-only-synthetic'
    target.write_text('synthetic only')
    link = tmp_path / 'same-name-on-remote'
    link.symlink_to(target)
    assert str(scoped(lanes, 'connor', lambda: _resolve_path_for_task(str(link)))) == str(link)

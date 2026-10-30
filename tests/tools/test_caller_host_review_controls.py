"""R1/R2 synthetic controls: backend doubles, never real SSH or private files."""
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.tools.test_caller_host_isolation import lanes, scoped
from tools import terminal_tool as tt
from cron import lifecycle_guard as lg


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_lookup_mutation_and_teardown_cannot_select_other_caller(lanes, monkeypatch, name):
    owner = SimpleNamespace(cwd='/owner', cleanup=lambda: pytest.fail('owner cleanup'))
    remote = SimpleNamespace(cwd='/remote', cleanup=lambda: None)
    key = scoped(lanes, name, lambda: tt._resolve_container_task_id('session'))
    monkeypatch.setattr(tt, '_active_environments', {'caller:chris:host': owner, key: remote})
    monkeypatch.setattr(tt, '_session_cwd', {})
    monkeypatch.setattr(tt, '_task_env_overrides', {})
    scoped(lanes, 'owner', lambda: tt.register_task_env_overrides('collision', {'cwd': '/owner'}), principal='chris')
    assert scoped(lanes, name, lambda: tt.get_session_cwd('collision')) is None
    assert scoped(lanes, name, lambda: tt.resolve_task_overrides('collision')) == {}
    assert scoped(lanes, name, lambda: tt.get_active_env('caller:chris:host')) is remote
    scoped(lanes, name, lambda: tt.register_task_env_overrides('caller:chris:host', {'cwd': '/remote-new'}))
    assert owner.cwd == '/owner'
    assert remote.cwd == '/remote-new'
    scoped(lanes, name, lambda: tt.cleanup_vm('caller:chris:host'))
    assert tt._active_environments == {'caller:chris:host': owner}
    assert scoped(lanes, name, lambda: tt.get_active_env('caller:chris:host')) is None


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_native_remote_positive_with_owner_cache_warm(lanes, monkeypatch, name, record_property):
    owner = SimpleNamespace(cwd='/owner', execute=lambda *a, **k: pytest.fail('owner executed'))
    class Remote:
        cwd = '/remote'
        def execute(self, command, **kwargs):
            return {'returncode': 0, 'output': 'SYNTHETIC-REMOTE-SUCCESS'}
    monkeypatch.setattr(tt, '_active_environments', {'caller:chris:host': owner})
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', lambda **kw: Remote())
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    result = json.loads(scoped(lanes, name, lambda: tt.terminal_tool(command='printf SYNTHETIC-REMOTE-SUCCESS', task_id='caller:chris:host')))
    record_property('native_receipt', json.dumps({'caller': name, 'route': 'terminal', 'result': result, 'transport': 'synthetic remote double', 'live_gateway': False}))
    assert result['exit_code'] == 0
    assert result['output'] == 'SYNTHETIC-REMOTE-SUCCESS'


@pytest.mark.parametrize('name', ['connor', 'josh'])
@pytest.mark.parametrize('remote_dangerous', [False, True])
def test_native_preflight_uses_remote_not_conflicting_host(lanes, tmp_path, monkeypatch, name, remote_dangerous, record_property):
    from tools import process_registry as pr
    script = tmp_path / 'synthetic-conflict.sh'
    script.write_text('printf safe' if remote_dangerous else 'hermes gateway restart')
    commands = []
    host_probes = []
    original_stat, original_resolve, original_read = Path.stat, Path.resolve, Path.read_bytes
    def stat_probe(path, *a, **kw):
        if path == script: host_probes.append('stat')
        return original_stat(path, *a, **kw)
    def resolve_probe(path, *a, **kw):
        if path == script: host_probes.append('resolve')
        return original_resolve(path, *a, **kw)
    def read_probe(path, *a, **kw):
        if path == script: host_probes.append('read')
        return original_read(path, *a, **kw)
    class Remote:
        cwd = '/remote'
        def execute(self, command, **kwargs):
            commands.append(command)
            if 'head -c' in command:
                return {'returncode': 0, 'output': 'hermes gateway restart' if remote_dangerous else 'printf safe'}
            return {'returncode': 0, 'output': 'REMOTE-SCRIPT-RAN'}
    monkeypatch.setattr(Path, 'stat', stat_probe)
    monkeypatch.setattr(Path, 'resolve', resolve_probe)
    monkeypatch.setattr(Path, 'read_bytes', read_probe)
    monkeypatch.setattr(pr, '_is_supervised_gateway_process', lambda: True)
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', lambda **kw: Remote())
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    result = json.loads(scoped(lanes, name, lambda: tt.terminal_tool(command='bash ' + shlex.quote(str(script)), task_id='conflict')))
    record_property('native_receipt', json.dumps({'caller': name, 'route': 'terminal lifecycle preflight', 'remote_dangerous': remote_dangerous, 'host_probes': host_probes, 'result': result, 'transport': 'remote double', 'live_gateway': False}))
    assert not host_probes
    assert ('Blocked:' in (result.get('error') or '')) == remote_dangerous
    assert any('head -c' in c for c in commands)
    assert ('REMOTE-SCRIPT-RAN' == result['output']) is (not remote_dangerous)


def test_remote_nested_resolution_is_lexical_and_guard_stays_active(monkeypatch):
    probes = []
    def forbidden(*a, **kw):
        probes.append('host filesystem touched')
        raise AssertionError('host I/O forbidden')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'expanduser', forbidden)
    monkeypatch.setattr(Path, 'cwd', forbidden)
    monkeypatch.setattr(lg, '_read_referenced_script', forbidden)
    seen = []
    scripts = {'~/outer.sh': 'bash inner.sh', '~/inner.sh': 'hermes gateway restart'}
    def remote(path):
        seen.append(path)
        return scripts[path]
    assert lg.contains_gateway_lifecycle_command_or_referenced_script('bash ~/outer.sh', cwd='/remote', read_remote_script=remote, remote_only=True)
    assert seen == ['~/outer.sh', '~/inner.sh']
    assert not probes
    assert not lg.contains_gateway_lifecycle_command_or_referenced_script('printf safe', remote_only=True)
    assert lg.contains_gateway_lifecycle_command_or_referenced_script('bash unavailable.sh', remote_only=True)


def test_owner_local_script_protection_preserved(tmp_path):
    script = tmp_path / 'owner.sh'
    script.write_text('hermes gateway restart')
    assert lg.contains_gateway_lifecycle_command_or_referenced_script('bash ' + shlex.quote(str(script)))
    script.write_text('printf safe')
    assert not lg.contains_gateway_lifecycle_command_or_referenced_script('bash ' + shlex.quote(str(script)))

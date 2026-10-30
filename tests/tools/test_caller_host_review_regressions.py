"""Independent synthetic-only review probes. No live gateway/network."""
import json
import shlex
from pathlib import Path

import pytest
from tests.tools.test_caller_host_isolation import lanes, scoped
from tools import terminal_tool as tt


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_raw_legacy_cache_cannot_supply_host_environment(lanes, tmp_path, monkeypatch, name, record_property):
    sentinel = tmp_path / 'synthetic-owner.txt'
    marker = 'GRACE-SYNTHETIC-OWNER-ONLY'
    sentinel.write_text(marker)
    # Warm the real owner cache through its ordinary native file route.
    from tools import file_tools as ft
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(ft, '_file_ops_cache', {})
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    owner_result = scoped(lanes, 'owner', lambda: ft.read_file_tool(str(sentinel), task_id='owner-normal-session'), principal='chris')
    assert marker in owner_result
    assert 'caller:chris:host' in tt._active_environments
    local = tt._active_environments['caller:chris:host']
    def unavailable_remote(**kwargs):
        assert kwargs['env_type'] == 'ssh'
        raise ConnectionError('Synthetic remote unavailable; no network attempted')
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', unavailable_remote)
    try:
        # /v1/runs accepts body.session_id and passes it as task_id.
        result = scoped(lanes, name, lambda: tt.terminal_tool(command='cat ' + shlex.quote(str(sentinel)), task_id='caller:chris:host', timeout=10))
        record_property('review_receipt', json.dumps({'caller': name, 'route': 'native terminal task_id colliding with naturally warmed owner cache', 'owner_result': json.loads(owner_result), 'result': json.loads(result), 'synthetic_only': True, 'live_gateway': False}))
        assert marker not in result, result
    finally:
        local.cleanup()


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_remote_terminal_preflight_does_not_read_host_script(lanes, tmp_path, monkeypatch, name, record_property):
    from tools import process_registry as pr
    script = tmp_path / 'synthetic-script.sh'
    # Text only. The gateway lifecycle command is never executed.
    script.write_text('hermes gateway restart\n')
    reads = []
    import os
    original_open = os.open
    def audit_open(path, *args, **kwargs):
        if str(path) == str(script):
            reads.append(str(path))
        return original_open(path, *args, **kwargs)
    class Remote:
        cwd = '/remote'
        def execute(self, *args, **kwargs):
            return {'returncode': 1, 'output': 'synthetic remote: no such file'}
    monkeypatch.setattr(os, 'open', audit_open)
    monkeypatch.setattr(pr, '_is_supervised_gateway_process', lambda: True)
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', lambda **kwargs: Remote())
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    result = scoped(lanes, name, lambda: tt.terminal_tool(command='bash ' + shlex.quote(str(script)), task_id='synthetic-preflight', timeout=10))
    record_property('review_receipt', json.dumps({'caller': name, 'route': 'native terminal supervised preflight', 'host_script_reads': reads, 'result': json.loads(result), 'synthetic_only': True, 'remote': 'no-file test double', 'live_gateway': False}))
    assert not reads, result

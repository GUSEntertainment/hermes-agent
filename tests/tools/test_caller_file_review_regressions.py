"""Grace's round-2 synthetic regressions, retained in the candidate suite."""
import json
from pathlib import Path
import pytest
from tests.tools.test_caller_host_isolation import lanes, scoped
from tools import file_tools as ft
from tools import terminal_tool as tt


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_remote_pdf_write_does_not_disclose_host_existence(lanes, tmp_path, monkeypatch, name, record_property):
    present = tmp_path / 'synthetic-present.pdf'
    missing = tmp_path / 'synthetic-absent.pdf'
    present.write_text('SYNTHETIC PDF PLACEHOLDER ONLY')
    calls = []
    def no_network(**kwargs):
        calls.append(kwargs['env_type'])
        raise ConnectionError('Synthetic remote unavailable; no writes or network')
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', no_network)
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(ft, '_file_ops_cache', {})
    probes = []
    original = Path.stat
    def stat(path, *a, **kw):
        if path in (present, missing):
            probes.append(str(path))
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, 'stat', stat)
    results = [json.loads(scoped(lanes, name, lambda p=p: ft.write_file_tool(str(p), '%PDF-synthetic', task_id='pdf-probe'))) for p in (present, missing)]
    record_property('native_receipt', json.dumps({'caller': name, 'route': 'write_file pdf precheck', 'results': results, 'host_stat_probes': probes, 'remote_creation': calls, 'live_gateway': False}))
    assert not probes, results
    assert all('Refusing to overwrite existing PDF' not in r.get('error', '') for r in results)


@pytest.mark.parametrize('name', ['connor', 'josh'])
@pytest.mark.parametrize('negative', [False, True])
def test_remote_read_cannot_reuse_owner_bookkeeping(lanes, tmp_path, monkeypatch, name, negative, record_property):
    actual = tmp_path / 'synthetic-confidential-contract.txt'
    actual.write_text('SYNTHETIC OWNER CONTENT')
    path = tmp_path / 'synthetic-confidential-contrac.txt' if negative else actual
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(ft, '_file_ops_cache', {})
    monkeypatch.setattr(ft, '_read_tracker', {})
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    owner = json.loads(scoped(lanes, 'owner', lambda: ft.read_file_tool(str(path), task_id='shared-client-session'), principal='chris'))
    if negative:
        assert 'synthetic-confidential-contract.txt' in json.dumps(owner), owner
    else:
        assert 'SYNTHETIC OWNER CONTENT' in owner['content']
    commands = []
    class Remote:
        cwd = '/remote'
        def execute(self, command, **kw):
            commands.append(command)
            return {'returncode': 1, 'output': 'Synthetic remote has no file'}
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', lambda **kw: Remote())
    result = json.loads(scoped(lanes, name, lambda: ft.read_file_tool(str(path), task_id='shared-client-session')))
    record_property('native_receipt', json.dumps({'caller': name, 'route': 'read_file colliding bookkeeping', 'negative_cache': negative, 'owner_result': owner, 'result': result, 'remote_commands': commands, 'live_gateway': False}))
    assert not result.get('dedup'), result
    if negative:
        assert 'synthetic-confidential-contract.txt' not in json.dumps(result), result
    assert commands, result

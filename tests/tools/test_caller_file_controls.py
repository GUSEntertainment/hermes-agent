"""Native tools with a synthetic mapped-shell SSH double, never real SSH.

The remote tree is separate from the host canary tree. Shell commands execute
for real against the remote tree; Python host probes on the host tree are logged.
"""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.tools.test_caller_host_isolation import lanes, scoped
from tools import file_tools as ft, terminal_tool as tt, file_state


@pytest.fixture
def remote_fs(lanes, tmp_path, monkeypatch):
    host = tmp_path / 'host-canaries'
    remote = tmp_path / 'remote-canaries'
    host.mkdir()
    remote.mkdir()
    commands, probes = [], []

    class SyntheticSSH:
        cwd = str(remote)
        _hermes_backend_name = 'ssh'
        def execute(self, command, **kw):
            commands.append(command)
            # Explicit transport double, not evidence of a real remote host.
            mapped = command.replace(str(host), str(remote))
            result = subprocess.run(mapped, shell=True, cwd=str(remote),
                                    input=kw.get('stdin_data'), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env={**os.environ, 'HOME': str(remote)}, timeout=30)
            return {'returncode': result.returncode, 'output': result.stdout}

    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(ft, '_file_ops_cache', {})
    monkeypatch.setattr(ft, '_read_tracker', {})
    monkeypatch.setattr(ft, '_patch_failure_tracker', {})
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    monkeypatch.setattr('tools.terminal_tool_backends._create_environment', lambda **kw: SyntheticSSH())
    monkeypatch.setattr(file_state, '_registry', file_state.FileStateRegistry())

    def start_probes():
        for op in ('stat', 'lstat', 'readlink', 'open'):
            original = getattr(os, op)
            def wrapped(path, *a, _original=original, _op=op, **kw):
                if not isinstance(path, int) and str(path).startswith(str(host)):
                    import traceback
                    probes.append((_op, str(path), [(f.name, f.lineno) for f in traceback.extract_stack(limit=7)[:-1]]))
                return _original(path, *a, **kw)
            monkeypatch.setattr(os, op, wrapped)
    return host, remote, commands, probes, start_probes


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_remote_native_read_write_patch_search_no_host_probe(lanes, remote_fs, name, record_property):
    host, remote, commands, probes, start = remote_fs
    (host / 'notes.txt').write_text('SYNTHETIC HOST NEVER RETURN')
    (remote / 'notes.txt').write_text('SYNTHETIC REMOTE ALLOWED')
    start()
    receipts = []
    def run():
        for _ in range(5):
            r = json.loads(ft.read_file_tool(str(host / 'notes.txt'), task_id='same'))
            receipts.append(r)
            assert 'SYNTHETIC REMOTE ALLOWED' in r.get('content', ''), r
        r = json.loads(ft.write_file_tool(str(host / 'new.txt'), 'REMOTE NEW', task_id='same'))
        receipts.append(r)
        assert not r.get('error'), r
        r = json.loads(ft.patch_tool(path=str(host / 'new.txt'), old_string='NEW', new_string='EDIT', task_id='same'))
        receipts.append(r)
        assert not r.get('error'), r
        r = json.loads(ft.search_tool('REMOTE', path=str(host), task_id='same'))
        receipts.append(r)
        assert not r.get('error'), r
    scoped(lanes, name, run)
    record_property('native_receipt', json.dumps({'caller': name, 'results': receipts, 'host_probes': probes, 'transport': 'mapped local shell double; no network', 'live_gateway': False}))
    assert commands
    assert not probes, sorted({repr(p[2]) for p in probes})
    assert (remote / 'new.txt').read_text() == 'REMOTE EDIT'


@pytest.mark.parametrize('name', ['connor', 'josh'])
@pytest.mark.parametrize('operation', ['write', 'replace', 'v4a'])
@pytest.mark.parametrize('remote_exists', [True, False])
def test_pdf_protection_follows_backend_not_host(lanes, remote_fs, name, operation, remote_exists, record_property):
    host, remote, commands, probes, start = remote_fs
    path = host / 'sample.pdf'
    if remote_exists:
        (remote / 'sample.pdf').write_text('%PDF-SYNTHETIC REMOTE')
    else:
        path.write_text('%PDF-SYNTHETIC HOST')
    start()
    def run():
        if operation == 'write':
            return ft.write_file_tool(str(path), '%PDF-NEW', task_id='pdf')
        if operation == 'replace':
            return ft.patch_tool(path=str(path), old_string='REMOTE', new_string='NEW', task_id='pdf')
        return ft.patch_tool(mode='patch', patch=f'*** Begin Patch\n*** Add File: {path}\n+%PDF-NEW\n*** End Patch', task_id='pdf')
    result = json.loads(scoped(lanes, name, run))
    record_property('native_receipt', json.dumps({'caller': name, 'operation': operation, 'remote_exists': remote_exists, 'result': result, 'host_probes': probes, 'transport': 'mapped local shell double', 'live_gateway': False}))
    if remote_exists:
        assert 'Refusing to overwrite existing PDF' in result.get('error', ''), result
    elif operation == 'replace':
        assert 'error' in result and 'Refusing to overwrite existing PDF' not in result['error']
    else:
        assert not result.get('error'), result
        assert (remote / 'sample.pdf').read_text().strip() == '%PDF-NEW'
    assert commands
    assert not probes, sorted({repr(p[2]) for p in probes})


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_scoped_bookkeeping_reset_and_history_cannot_touch_owner(lanes, tmp_path, monkeypatch, name):
    path = str(tmp_path / 'synthetic.txt')
    monkeypatch.setattr(ft, '_read_tracker', {})
    monkeypatch.setattr(ft, '_patch_failure_tracker', {})
    owner_key = scoped(lanes, 'owner', lambda: ft._file_bookkeeping_key('same'), principal='chris')
    ft._read_tracker[owner_key] = {'last_key': 'owner', 'consecutive': 3, 'dedup': {(path, 1, 2000): 42}, 'dedup_hits': {}, 'not_found': {('read', path): (42, 'OWNER-ONLY')}, 'read_history': {(path, 1, 2000)}}
    owner = copy.deepcopy(ft._read_tracker)
    owner_ops = object()
    monkeypatch.setattr(ft, '_file_ops_cache', {'caller:chris:host': owner_ops})
    def run():
        assert ft._file_bookkeeping_key('same') != owner_key
        assert ft._file_bookkeeping_key(str(owner_key)) != owner_key
        ft.reset_file_dedup('same')
        ft.reset_file_dedup()
        ft.notify_other_tool_call('same')
        __import__('tools.file_tools_read_tracking', fromlist=['x'])._invalidate_dedup_for_path(path, 'same')
        ft._update_read_timestamp(path, 'same')
        assert ft._check_file_staleness(path, 'same') is None
        ft.clear_file_ops_cache('caller:chris:host')
        ft.clear_file_ops_cache()
        assert file_state.known_reads('same') == []
        assert file_state.writes_since('same', 0, [path]) == {}
    scoped(lanes, name, run)
    assert ft._read_tracker == owner
    assert ft._file_ops_cache['caller:chris:host'] is owner_ops
    assert scoped(lanes, 'owner', lambda: ft._record_patch_failure('same', path), principal='chris') == 1
    assert scoped(lanes, name, lambda: ft._record_patch_failure('same', path)) == 1
    scoped(lanes, name, lambda: ft._reset_patch_failures('same', [path]))
    assert scoped(lanes, 'owner', lambda: ft._record_patch_failure('same', path), principal='chris') == 2


def test_bookkeeping_binds_profile_config_and_raw_session(lanes):
    first = scoped(lanes, 'connor', lambda: ft._file_bookkeeping_key('same'))
    assert first != scoped(lanes, 'josh', lambda: ft._file_bookkeeping_key('same'))
    assert first != scoped(lanes, 'connor', lambda: ft._file_bookkeeping_key('other'))
    config = lanes / 'profiles' / 'connor' / 'config.yaml'
    config.write_text(config.read_text().replace('connor.invalid', 'other.invalid'))
    assert first != scoped(lanes, 'connor', lambda: ft._file_bookkeeping_key('same'))
    with pytest.raises(PermissionError):
        scoped(lanes, 'unknown', lambda: ft._file_bookkeeping_key('same'))


@pytest.mark.parametrize('name', ['connor', 'josh'])
@pytest.mark.parametrize('path', ['relative.json', '~/relative.json'])
def test_remote_relative_write_lint_and_read(lanes, remote_fs, name, path, record_property):
    host, remote, commands, probes, start = remote_fs
    start()
    write = json.loads(scoped(lanes, name, lambda: ft.write_file_tool(path, '{"synthetic": true}', task_id='relative')))
    read = json.loads(scoped(lanes, name, lambda: ft.read_file_tool(path, task_id='relative')))
    record_property('native_receipt', json.dumps({'caller': name, 'write': write, 'read': read, 'transport': 'mapped local shell double', 'live_gateway': False}))
    assert not write.get('error'), write
    assert write.get('verified') is True
    assert 'synthetic' in read.get('content', ''), read
    assert json.loads((remote / 'relative.json').read_text()) == {'synthetic': True}
    assert not probes


@pytest.mark.parametrize('name', ['connor', 'josh'])
def test_remote_sensitive_literal_guards_do_not_disappear(lanes, name):
    from agent.file_safety import get_read_block_error, get_write_denied_error
    for path in ('~/.ssh/id_ed25519', '/remote/.hermes/auth.json', '/remote/.env'):
        assert scoped(lanes, name, lambda: get_read_block_error(path))
        assert scoped(lanes, name, lambda: get_write_denied_error(path))
    assert scoped(lanes, name, lambda: __import__('tools.file_tools_write_guards', fromlist=['x'])._protected_instruction_reason('/remote/AGENTS.md', enabled=True, extra_patterns=[]))


def test_owner_native_access_and_dedup_preserved(lanes, tmp_path, monkeypatch, record_property):
    monkeypatch.setattr(tt, '_active_environments', {})
    monkeypatch.setattr(ft, '_file_ops_cache', {})
    monkeypatch.setattr(ft, '_read_tracker', {})
    monkeypatch.setattr(tt, '_start_cleanup_thread', lambda: None)
    path = tmp_path / 'synthetic-owner.txt'
    pdf = tmp_path / 'synthetic-owner.pdf'
    def run():
        written = json.loads(ft.write_file_tool(str(path), 'SYNTHETIC OWNER', task_id='owner'))
        read = json.loads(ft.read_file_tool(str(path), task_id='owner'))
        dedup = json.loads(ft.read_file_tool(str(path), task_id='owner'))
        created_pdf = json.loads(ft.write_file_tool(str(pdf), '%PDF-SYNTHETIC', task_id='owner'))
        denied_pdf = json.loads(ft.write_file_tool(str(pdf), 'not a PDF', task_id='owner'))
        assert not written.get('error'), written
        assert 'SYNTHETIC OWNER' in read['content']
        assert dedup.get('dedup')
        assert not created_pdf.get('error'), created_pdf
        assert 'Refusing to overwrite existing PDF' in denied_pdf['error']
        record_property('native_receipt', json.dumps({'caller': 'chris', 'write': written, 'read': read, 'dedup': dedup, 'pdf_create': created_pdf, 'pdf_protect': denied_pdf, 'live_gateway': False}))
    scoped(lanes, 'owner', run, principal='chris')


def test_unknown_native_file_routes_fail_closed(lanes, tmp_path):
    path = str(tmp_path / 'synthetic-unknown.txt')
    for call in (lambda: ft.read_file_tool(path), lambda: ft.write_file_tool(path, 'synthetic'), lambda: ft.patch_tool(path=path, old_string='a', new_string='b'), lambda: ft.search_tool('synthetic', path=path)):
        result = json.loads(scoped(lanes, 'unknown', call))
        assert result.get('error') == 'Caller-scoped terminal unavailable', result

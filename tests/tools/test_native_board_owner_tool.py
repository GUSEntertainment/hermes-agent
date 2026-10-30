import json

import pytest

from tools import kanban_tools as kt
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    for key in ('HERMES_PROFILE', 'HERMES_KANBAN_TASK', 'HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(kt, '_profile_has_kanban_toolset', lambda: True)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def test_native_inspection_provides_exact_control_evidence(board):
    tid = kb.create_task(board, title='Local receipt', assignee='maker', triage=True)
    result = json.loads(kt._handle_show({'task_id': tid}))
    native = result['board_administration']['native']
    assert native['tool'] == 'kanban_manage'
    assert len(native['expected_state']) == 64
    assert len(native['expected_task_sha256']) == 64
    assert kt._check_board_owner_mode()


@pytest.mark.parametrize('profile', ['josh','connor','claire'])
def test_non_owner_never_reaches_subprocess(board, monkeypatch, profile):
    monkeypatch.setenv('HERMES_PROFILE', profile)
    monkeypatch.setattr(kt, '_invoke_board_controller', lambda args: pytest.fail('Unauthorized controller call'))
    assert not kt._check_board_owner_mode()
    assert 'error' in json.loads(kt._handle_manage({'task_id':'t_dummy','action':'archive'}))


def test_authenticated_member_context_denied_even_with_default_process(board, monkeypatch):
    from gateway import josh_board_authority as authority
    token = authority._execution.set(('default','connor'))
    try:
        monkeypatch.setattr(kt, '_invoke_board_controller', lambda args: pytest.fail('Cross-account controller call'))
        assert 'error' in json.loads(kt._handle_manage({'task_id':'t_dummy','action':'archive'}))
    finally:
        authority._execution.reset(token)


def test_owner_calls_controller_with_real_board_admission(board, monkeypatch):
    seen = []
    monkeypatch.setattr(kt, '_invoke_board_controller', lambda args: seen.append(args) or {'ok':True,'status':'archived'})
    result = json.loads(kt._handle_manage({'task_id':'t_dummy','action':'archive','board':None}))
    assert result['ok'] and len(seen) == 1

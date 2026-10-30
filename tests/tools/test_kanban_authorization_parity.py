"""Independent acceptance checks: authorized work and callback boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'fixture.db'))
    monkeypatch.setenv('HERMES_PROFILE', 'builder')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing() as conn:
        yield kb, conn


@pytest.mark.parametrize('surface', ['tool', 'cli'])
def test_repeat_broad_consent_does_not_park_authorized_implementation(board, monkeypatch, surface):
    from hermes_cli import kanban
    from tools import kanban_tools
    kb, conn = board
    body = kb.append_authorization_continuity_to_body(
        'Fix CSV import preserving quoted commas and run the parser tests.',
        {'authorized': True, 'authorized_by': 'Chris', 'scope': 'local CSV parser implementation and tests'},
    )
    task_id = kb.create_task(conn, title='Fix CSV quoted commas', body=body, assignee='builder')
    task = kb.claim_task(conn, task_id, claimer='builder:fixture')
    monkeypatch.setenv('HERMES_KANBAN_TASK', task_id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    reason = 'Need Chris approval to start implementing the CSV fix.'
    if surface == 'tool':
        result = json.loads(kanban_tools._handle_block({'kind': 'needs_input', 'reason': reason}))
        assert not result.get('ok')
    else:
        result = kanban._cmd_block(SimpleNamespace(task_id=task_id, ids=None, kind='needs_input', reason=[reason]))
        assert result == 1
    assert kb.get_task(conn, task_id).status == 'running'


@pytest.mark.parametrize('surface', ['tool', 'cli'])
def test_genuine_exact_new_action_remains_held(board,monkeypatch,surface):
    from hermes_cli import kanban
    from tools import kanban_tools
    kb,conn=board
    body=kb.append_authorization_continuity_to_body('Prepare an internal draft; external send is separate.',{'authorized':True,'authorized_by':'Chris','scope':'internal draft and verification only'})
    tid=kb.create_task(conn,title='Draft',body=body,assignee='builder')
    task=kb.claim_task(conn,tid,claimer='builder:fixture')
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid);monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(task.current_run_id))
    decision='Whether to send the exact reviewed draft to the named client; no send is authorized yet.'
    if surface=='tool':
        out=json.loads(kanban_tools._handle_block({'kind':'needs_input','reason':'Internal draft is prepared','authorization_checked':True,'new_decision':decision}))
        assert out.get('ok') is True
    else:
        assert kanban._cmd_block(SimpleNamespace(task_id=tid,ids=None,kind='needs_input',reason=['Internal draft is prepared'],authorization_checked=True,new_decision=decision))==0
    assert kb.get_task(conn,tid).status=='blocked'
    assert decision in kb.latest_run(conn,tid).summary


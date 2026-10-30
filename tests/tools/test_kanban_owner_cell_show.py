"""Regression from native Grace review: required state was absent from show."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban, owner_cell
from tools import kanban_tools
from tests.hermes_cli.test_worker_readiness import board, contract


@pytest.mark.parametrize('version',[1,2])
def test_cli_and_tool_show_current_authoritative_owner_cell_state(board,capsys,version):
    conn,_=board
    tid=kb.create_task(conn,title='Reviewed artifact',assignee='worker',owner_cell_contract=contract())
    if version==2:
        with kb.write_txn(conn):
            owner_cell.update_state(conn,tid,expected_version=1,facts=['Current authoritative state'])
    before=conn.total_changes
    # v0.21.5 upstream rejects unknown tool params: a client-supplied state
    # version is refused outright (stronger than silently ignoring it).
    spoof=json.loads(kanban_tools._handle_show({'task_id':tid,'current_state_version':9999}))
    assert 'unknown parameter' in spoof['error'] and 'current_state_version' in spoof['error']
    tool=json.loads(kanban_tools._handle_show({'task_id':tid}))
    assert kanban._cmd_show(SimpleNamespace(task_id=tid,json=True))==0
    cli=json.loads(capsys.readouterr().out)
    assert cli['owner_cell']==tool['owner_cell']==kb.owner_cell_read_state(conn,tid)
    assert tool['owner_cell']['current_state_version']==version
    assert set(tool['owner_cell'])=={'goal_id','current_state_version','phase','owner_profile','runtime_profile','required_reviewer_profile','review_state_version','review_artifact_ref','review_artifact_digest'}
    assert conn.total_changes==before


def test_ordinary_task_has_no_fabricated_owner_cell_state(board,capsys):
    conn,_=board
    tid=kb.create_task(conn,title='Ordinary task',assignee='worker')
    assert json.loads(kanban_tools._handle_show({'task_id':tid}))['owner_cell'] is None
    assert kanban._cmd_show(SimpleNamespace(task_id=tid,json=True))==0
    assert json.loads(capsys.readouterr().out)['owner_cell'] is None


def test_text_show_reports_version_without_review_authority(board,capsys):
    conn,_=board
    tid=kb.create_task(conn,title='Review state',assignee='worker',owner_cell_contract=contract())
    assert kanban._cmd_show(SimpleNamespace(task_id=tid,json=False))==0
    output=capsys.readouterr().out
    assert 'owner cell state version: 1 (execute)' in output
    assert 'review_decision_capability' not in output

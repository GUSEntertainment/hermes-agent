"""Native member board reads retain exact identity and database boundaries."""
import json
import sqlite3

import pytest

from gateway import app_business_authority, josh_board_authority, member_worker_policy
from hermes_cli import board_owner_manage, kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def boards(tmp_path, monkeypatch):
    root = (tmp_path / 'hermes').resolve()
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(root))
    for name in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_HOME', 'HERMES_KANBAN_BOARD',
                 'HERMES_KANBAN_TASK', 'HERMES_SESSION_SOURCE', 'HERMES_PROFILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(board_owner_manage, 'ROOT', root)
    monkeypatch.setattr(member_worker_policy, 'authenticated_private_policy', lambda: None)
    generation = {'fingerprint': 'a' * 64}
    monkeypatch.setattr(app_business_authority, 'canonical_enrollment', lambda principal, root:
                        dict(owner=principal, profile=principal, board=principal+'-home', **generation))
    monkeypatch.setattr(kt, '_profile_has_kanban_toolset', lambda: True)
    ids = {}
    for owner in ('connor', 'joshgarcia', 'chris'):
        kb.init_db(board=owner+'-home')
        with kb.connect_closing(board=owner+'-home') as conn:
            ids[owner] = kb.create_task(conn, title=owner+' private fixture', assignee='maker', triage=True)
    token = josh_board_authority._execution.set(None)
    try:
        yield root, ids, generation
    finally:
        josh_board_authority._execution.reset(token)


@pytest.mark.parametrize('member', ['connor', 'joshgarcia'])
def test_member_reads_own_board_and_cannot_select_foreign_board(boards, member):
    root, ids, generation = boards
    token = josh_board_authority._execution.set((member, member))
    try:
        result = json.loads(kt._handle_show({'task_id': ids[member]}))
        assert result['task']['title'] == member+' private fixture'
        with pytest.raises(PermissionError, match='ownership'):
            kt._connect(board='chris-home')
        result = json.loads(kt._handle_show({'task_id': ids['chris']}))
        assert 'error' in result
        assert 'chris private fixture' not in json.dumps(result)
    finally:
        josh_board_authority._execution.reset(token)


@pytest.mark.parametrize('override', ['HERMES_KANBAN_DB', 'HERMES_KANBAN_HOME'])
def test_member_cannot_read_an_ambient_database_override(boards, monkeypatch, override):
    root, ids, generation = boards
    path = root / 'kanban' / 'boards' / 'chris-home' / 'kanban.db'
    monkeypatch.setenv(override, str(path if override.endswith('_DB') else root / 'elsewhere'))
    token = josh_board_authority._execution.set(('connor', 'connor'))
    try:
        with pytest.raises(PermissionError, match='override denied'):
            kt._connect()
    finally:
        josh_board_authority._execution.reset(token)


def test_member_closes_connection_if_enrollment_changes_on_open(boards, monkeypatch):
    root, ids, generation = boards
    original = kb.connect
    seen = []
    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        seen.append(conn)
        generation['fingerprint'] = 'b' * 64
        return conn
    monkeypatch.setattr(kb, 'connect', connect)
    token = josh_board_authority._execution.set(('connor', 'connor'))
    try:
        with pytest.raises(PermissionError, match='enrollment changed'):
            kt._connect()
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            seen[0].execute('SELECT 1')
    finally:
        josh_board_authority._execution.reset(token)


def test_private_worker_preserves_existing_task_admission(boards, monkeypatch):
    root, ids, generation = boards
    monkeypatch.setattr(member_worker_policy, 'authenticated_private_policy',
                        lambda: dict(board='joshgarcia-home', task=ids['joshgarcia']))
    monkeypatch.setattr(board_owner_manage, 'require_owner', lambda: pytest.fail('Worker must retain task route'))
    token = josh_board_authority._execution.set(('joshgarcia', 'joshgarcia'))
    try:
        _, conn = kt._connect()
        try:
            assert kb.get_task(conn, ids['joshgarcia']) is not None
        finally:
            conn.close()
        with pytest.raises(PermissionError, match='private task'):
            kt._connect('chris-home')
    finally:
        josh_board_authority._execution.reset(token)


def test_root_cli_retains_existing_board_route(boards, monkeypatch):
    root, ids, generation = boards
    monkeypatch.setattr(board_owner_manage, 'require_owner', lambda: pytest.fail('Root CLI route must be unchanged'))
    _, conn = kt._connect('chris-home')
    try:
        assert kb.get_task(conn, ids['chris']) is not None
    finally:
        conn.close()

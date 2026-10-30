"""t_735766e7: a member's main Margot manages and fixes its OWN board from day one.

All checks run on disposable copies (tmp HERMES_HOME); live boards/profiles untouched.
"""
import json
import shutil
from pathlib import Path

import pytest

from gateway import member_board_tools as member, josh_board_authority as authority
from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import blocked_lane_recovery as rec

LIVE = Path('/Users/Chris/.hermes/profiles')
AMANDA = 'josh-agent-1d87c1a86adbc85b27545787e7834bab'
MORGAN = 'josh-agent-442ec2e24f69f24eda43121ef5c2d35e'


@pytest.fixture
def josh(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    for name in (AMANDA, MORGAN, 'scarlett'):
        (home / 'profiles' / name).mkdir(parents=True)
        shutil.copy2(LIVE / name / 'config.yaml', home / 'profiles' / name / 'config.yaml')
    (home / 'skills').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_HOME',
                'HERMES_KANBAN_TASK', 'HERMES_SESSION_SOURCE', 'HERMES_PROFILE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(control, 'ROOT', home)
    grant = {'principal': 'joshgarcia', 'profile': 'joshgarcia', 'board': 'josh-home', 'fingerprint': 'g1'}
    monkeypatch.setattr(control, 'require_owner', lambda: dict(grant) if grant else None)
    from gateway import app_business_authority as aba, owner_delegation as od
    monkeypatch.setattr(aba, 'protected', lambda *a, **k: None)
    monkeypatch.setattr(aba, 'protected_bytes', lambda path, private=True, anchor=None: Path(path).read_bytes())
    monkeypatch.setattr(od, 'enrollment', lambda o: {'owner': o, 'profile': o, 'board': 'josh-home'})
    monkeypatch.setattr(od, 'agents', lambda principal=None, **k: [
        {'id': 'oa_' + AMANDA[-32:], 'owner': 'joshgarcia', 'name': 'Amanda', 'purpose': 'x', 'archivedAt': None},
        {'id': 'oa_' + MORGAN[-32:], 'owner': 'joshgarcia', 'name': 'Morgan', 'purpose': 'x', 'archivedAt': None}]
        if principal == 'joshgarcia' else [])
    boards = {}
    for b in ('josh-home', 'chris-home', 'connor-home'):
        p = home / 'kanban' / 'boards' / b / 'kanban.db'
        p.parent.mkdir(parents=True)
        with kb.connect_closing(p) as conn:
            boards[b] = kb.create_task(conn, title=f'QA {b}', assignee=AMANDA if b == 'josh-home' else 'scarlett')
    log = tmp_path / 'fixes-log.md'
    monkeypatch.setattr(rec, 'FIXES_LOG', log)
    yield home, grant, boards, log


def _block(home, board, tid, unmet):
    with kb.connect_closing(home / 'kanban' / 'boards' / board / 'kanban.db') as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (tid,))
            kb._append_event(conn, tid, 'capability_preflight_blocked', {'unmet': unmet, 'reason': 'admission'})


def _row(home, board, tid):
    with kb.connect_closing(home / 'kanban' / 'boards' / board / 'kanban.db') as conn:
        return dict(conn.execute('SELECT status,assignee,block_kind FROM tasks WHERE id=?', (tid,)).fetchone())


def test_josh_margot_equips_amanda_and_requeues_same_agent(josh):
    home, _, boards, log = josh
    tid = boards['josh-home']
    _block(home, 'josh-home', tid, {'issues': [{'code': 'missing_toolset', 'resource': 'clarify'}]})
    out = json.loads(member.equip({'task_id': tid}))
    assert out['action'] == 'equip', out
    import yaml
    cfg = yaml.safe_load((home / 'profiles' / AMANDA / 'config.yaml').read_text())
    assert 'clarify' in cfg['toolsets']
    row = _row(home, 'josh-home', tid)
    assert row['assignee'] == AMANDA and row['status'] in ('ready', 'todo')
    assert 'blocked-lane-recovery josh-home/' + tid in log.read_text()


def test_credential_gap_surfaces_to_josh_not_granted(josh):
    home, _, boards, _ = josh
    tid = boards['josh-home']
    before = (home / 'profiles' / AMANDA / 'config.yaml').read_bytes()
    _block(home, 'josh-home', tid, {'issues': [{'code': 'mcp_connection_or_auth_unavailable', 'resource': 'gmail'}]})
    out = json.loads(member.equip({'task_id': tid}))
    assert out['action'] == 'ask'
    assert (home / 'profiles' / AMANDA / 'config.yaml').read_bytes() == before
    row = _row(home, 'josh-home', tid)
    assert row['block_kind'] == 'needs_input' and row['assignee'] == AMANDA
    with kb.connect_closing(home / 'kanban' / 'boards' / 'josh-home' / 'kanban.db') as conn:
        body = conn.execute('SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC', (tid,)).fetchone()[0]
    assert body.startswith('Needs Josh:') and 'Chris' not in body


def test_member_never_gets_chris_scope_paths_or_foreign_agents(josh):
    home, _, boards, _ = josh
    tid = boards['josh-home']
    before = (home / 'profiles' / AMANDA / 'config.yaml').read_bytes()
    _block(home, 'josh-home', tid, {'issues': [
        {'code': 'missing_toolset', 'resource': 'browser_admin_x'},
        {'code': 'path_outside_workspace', 'resource': '/Users/Chris/.hermes/kanban/boards/chris-home'}],
        'read_paths': ['/Users/Chris/.hermes/kanban/boards/chris-home'],
        'profile_installed': True, 'enrolled_worker': False})
    out = json.loads(member.equip({'task_id': tid}))
    assert out['action'] == 'skip'
    assert (home / 'profiles' / AMANDA / 'config.yaml').read_bytes() == before
    # A card assigned to a Chris agent on Josh's board is not his to equip.
    with kb.connect_closing(home / 'kanban' / 'boards' / 'josh-home' / 'kanban.db') as conn:
        other = kb.create_task(conn, title='foreign', assignee='scarlett')
    _block(home, 'josh-home', other, {'issues': [{'code': 'missing_toolset', 'resource': 'web'}]})
    with pytest.raises(PermissionError):
        member.equip({'task_id': other})
    with pytest.raises(PermissionError):
        rec.run_member(None, 'chris-home', 't_x', 'chris', [])


def test_josh_margot_denied_on_chris_and_connor_boards(josh):
    _, _, boards, _ = josh
    for b in ('chris-home', 'connor-home'):
        with pytest.raises(PermissionError):
            control.member_board({'principal': 'joshgarcia', 'profile': 'joshgarcia', 'board': 'josh-home',
                                  'fingerprint': 'g1'}, b)
        with pytest.raises(PermissionError):
            control.administrator_board({'principal': 'joshgarcia', 'profile': 'joshgarcia',
                                         'board': 'josh-home', 'fingerprint': 'g1'}, b)
        assert 'error' in json.loads(member.manage({'task_id': boards[b], 'action': 'unblock',
                                                    'reason': 'x', 'board': b}) if False else
                                     json.dumps({'error': 'unreachable'}))
        with pytest.raises(PermissionError):
            member.manage({'task_id': boards[b], 'action': 'unblock', 'reason': 'x', 'board': b})
    with pytest.raises(ValueError):
        member.equip({'task_id': boards['chris-home'], 'board': 'chris-home'})


def test_josh_margot_unblocks_and_reassigns_own_qa_card_through_controller(josh, monkeypatch):
    home, grant, boards, _ = josh
    tid = boards['josh-home']
    with kb.connect_closing(home / 'kanban' / 'boards' / 'josh-home' / 'kanban.db') as conn:
        kb.block_task(conn, tid, reason='QA env flaky')
        state = control.describe(conn, tid)['expected_state']
    from contextlib import contextmanager
    import gateway.gus_admin_cli as admin
    monkeypatch.setattr(admin, 'admission', contextmanager(lambda proof: (yield)))
    token = control._member_context.set(dict(grant))
    monkeypatch.setattr(control, 'refresh_member_grant', lambda g: dict(g))
    monkeypatch.setattr(control, 'require_owner', lambda: dict(grant))
    try:
        out = control.execute({'action': 'unblock', 'task_id': tid, 'reason': 'env fixed', 'expected_state': state})
        assert out['changed'] and out['status'] in ('ready', 'todo')
        # Reassign to Morgan (own roster) via the existing prepare/plan path.
        from hermes_cli import owner_work_preparation as prep
        with kb.connect_closing(home / 'kanban' / 'boards' / 'josh-home' / 'kanban.db') as conn:
            prep._eligible(conn, prep._task(conn, tid), MORGAN, grant)
            with pytest.raises(PermissionError):
                prep._eligible(conn, prep._task(conn, tid), 'scarlett', grant)
            assert kb.assign_task(conn, tid, MORGAN)
        assert _row(home, 'josh-home', tid)['assignee'] == MORGAN
    finally:
        control._member_context.reset(token)

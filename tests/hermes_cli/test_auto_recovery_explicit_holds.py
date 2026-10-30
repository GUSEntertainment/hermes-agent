"""Independent synthetic review; shipped isolation fixtures loaded as plugin.
No live DB writes, CLI mutations, network, or worker launches.
"""
import hashlib
import json
import runpy
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import auto_same_owner_recovery as auto
from hermes_cli import kanban_completion as completion

@pytest.fixture
def conn(tmp_path):
    with kb.connect_closing(db_path=tmp_path / 'synthetic.db') as c:
        assert str(tmp_path) in c.execute('PRAGMA database_list').fetchone()[2]
        yield c


def stopped(conn, body='Repair the local index.'):
    tid = kb.create_task(conn, title='Repair local index', body=body, assignee='riley')
    kb.recompute_ready(conn)
    assert kb.block_task(conn, tid, kind='capability', reason='Missing local fixture')
    return tid


def test_source_identity():
    for mod in (auto, completion):
        p = Path(mod.__file__)
        print('SOURCE', p, hashlib.sha256(p.read_bytes()).hexdigest())


@pytest.mark.parametrize('hold', ['Do not proceed until Chris approves.', 'Do not restart until Chris approves.', 'Do not proceed until Chris approves the payment.'])
@pytest.mark.parametrize('location', ['body', 'comment'])
def test_unresolved_hold(conn, hold, location):
    tid = stopped(conn, hold if location == 'body' else 'Repair the local index.')
    if location == 'comment':
        kb.add_comment(conn, tid, 'default', hold)
    before = kb.get_task(conn, tid)
    report = auto.run(conn, now=int(time.time())+7200)
    after = kb.get_task(conn, tid)
    print('HOLD', location, hold, report, 'state', before.status, after.status)
    assert (after.status, after.block_kind) == (before.status, before.block_kind)


def test_intervening_hold_atomic_recheck(conn):
    tid = stopped(conn)
    now = int(time.time())+7200
    selected = next(x for x in auto.candidates(conn, now) if x[0] == tid)
    assert selected[1] == 'recover'
    kb.add_comment(conn, tid, 'default', 'Do not proceed until Chris approves the payment.')
    before = kb.get_task(conn, tid)
    assert auto.recover(conn, tid, selected[2], now) is None
    after = kb.get_task(conn, tid)
    assert (before.status, before.block_kind) == (after.status, after.block_kind)


def test_cron_does_not_fall_through_after_recurrence_hold(conn, monkeypatch):
    tid = stopped(conn)
    now = int(time.time())+7200
    assert auto.run(conn, now=now)[0]['action'] == 'recovered'
    assert kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, kind='capability', reason='Missing local fixture')
    now += 7200
    assert auto.run(conn, now=now, dry_run=True)[0]['action'] == 'hold'
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    import sqlite3
    import subprocess
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(f'file:{database}?mode=ro', uri=True))
    monkeypatch.setattr(time, 'time', lambda: now)
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if 'hermes_cli.auto_same_owner_recovery' in cmd:
            report = auto.run(conn, now=now)
            # Match unchanged module CLI's non-verbose projection.
            return SimpleNamespace(returncode=0, stdout=json.dumps([r for r in report if r['action'] in ('recovered','would_recover')]), stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='synthetic boundary: no CLI mutation executed')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    runpy.run_path('/Users/Chris/.hermes/scripts/kanban-auto-retry-blocked.py', run_name='__main__')
    fallbacks = [c for c in calls if c[0] == 'hermes']
    print('CRON_FALLBACKS', fallbacks)
    assert not fallbacks

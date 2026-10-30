"""Provenance-aware unresolved-obligation guard for automatic recovery (t_df999f30 r6)."""
import time
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import auto_same_owner_recovery as auto

LATER = lambda: int(time.time()) + 7200

@pytest.fixture
def conn(tmp_path):
    with kb.connect_closing(db_path=tmp_path / 'b.db') as c:
        yield c

def stopped(conn, body='Repair the local index.', reason='Missing local fixture'):
    tid = kb.create_task(conn, title='Repair local index', body=body, assignee='riley')
    kb.recompute_ready(conn)
    assert kb.block_task(conn, tid, kind='capability', reason=reason)
    return tid

HOLDS = ['Restart is prohibited.', 'Do not automatically resume.', 'Wait for Chris before proceeding.',
         'Do not proceed.', "Don\u2019t restart\nuntil Chris approves.", 'Not allowed to deploy yet.',
         'Hold off on the send.', 'Needs Chris approval.', 'Proceeding is forbidden.']

@pytest.mark.parametrize('text', HOLDS)
@pytest.mark.parametrize('where', ['body', 'comment'])
def test_unresolved_hold_preserved(conn, text, where):
    tid = stopped(conn, text if where == 'body' else 'Repair the local index.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', text)
    auto.run(conn, now=LATER())
    assert kb.get_task(conn, tid).status == 'blocked'
    assert kb.claim_task(conn, tid) is None

def test_ordinary_recovers(conn):
    tid = stopped(conn)
    auto.run(conn, now=LATER())
    assert kb.get_task(conn, tid).status == 'ready' and kb.claim_task(conn, tid)

@pytest.mark.parametrize('where', ['body', 'comment'])
def test_answered_hold_not_resurrected(conn, where):
    tid = stopped(conn, 'Do not proceed.' if where == 'body' else 'Repair.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', 'Do not proceed.')
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    auto.run(conn, now=LATER())
    assert kb.get_task(conn, tid).status == 'ready'

def test_new_hold_after_release_still_holds(conn):
    tid = stopped(conn, 'Do not proceed.')
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, 'default', 'Wait for Chris before restarting.')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    auto.run(conn, now=LATER())
    assert kb.get_task(conn, tid).status == 'blocked'

def test_hold_in_later_stop_reason_after_release_holds(conn):
    tid = stopped(conn)
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, kind='capability', reason='Fixture missing; do not proceed without Chris approval')
    auto.run(conn, now=LATER())
    assert kb.get_task(conn, tid).status == 'blocked'

def test_automatic_recovery_never_answers_hold(conn):
    tid = stopped(conn)
    later = LATER()
    chosen = next(x for x in auto.candidates(conn, later) if x[0] == tid)
    kb.add_comment(conn, tid, 'default', 'Restart is prohibited.')
    assert auto.recover(conn, tid, chosen[2], later) is None
    assert kb.get_task(conn, tid).status == 'blocked'

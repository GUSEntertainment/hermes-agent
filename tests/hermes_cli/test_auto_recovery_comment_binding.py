"""Round-9 (t_df999f30): comment provenance by durable row boundary, and
negation bound to governed head verbs (not tokens anywhere in the clause)."""
import json
import time

import pytest

from hermes_cli import auto_same_owner_recovery as auto
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    with kb.connect_closing(db_path=tmp_path / 'k.db') as c:
        yield c


def _stopped(c, body='Repair local index.'):
    tid = kb.create_task(c, title='Repair local index', body=body, assignee='riley')
    kb.recompute_ready(c)
    assert kb.block_task(c, tid, kind='capability', reason='Missing local fixture')
    return tid


def _recovers(c, tid):
    auto.run(c, now=int(time.time()) + 7200)
    return kb.get_task(c, tid).status == 'ready' and kb.claim_task(c, tid) is not None


def test_unblock_records_comment_boundary(conn):
    tid = _stopped(conn)
    cid = kb.add_comment(conn, tid, 'default', 'note')
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='unblocked' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert json.loads(row[0])['comment_boundary'] == cid


@pytest.mark.parametrize('lookalike', [False, True])
@pytest.mark.parametrize('new_hold', [False, True])
def test_specify_audit_lookalike_cannot_shift_new_hold(conn, lookalike, new_hold):
    tid = _stopped(conn)
    for _ in range(10):
        n = conn.execute('SELECT block_recurrences FROM tasks WHERE id=?', (tid,)).fetchone()[0]
        if n >= kb.TECHNICAL_BLOCK_RECURRENCE_LIMIT - 1:
            break
        assert kb.unblock_task(conn, tid, preserve_failure_history=True)
        assert kb.block_task(conn, tid, kind='capability', reason='Missing another local fixture')
    kb.add_comment(conn, tid, 'default', 'Specified \u2014 updated assignee and promoted to todo.'
                   if lookalike else 'Earlier note.')
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    kb.add_comment(conn, tid, 'default', 'Do not proceed.' if new_hold else 'Fixture available.')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing next local fixture')
    assert kb.specify_triage_task(conn, tid, assignee='riley-two', author='default')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing final local fixture')
    assert _recovers(conn, tid) is (not new_hold)


def test_answered_comment_hold_recovers_after_release(conn):
    tid = _stopped(conn)
    kb.add_comment(conn, tid, 'default', 'Do not proceed.')
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    assert _recovers(conn, tid)


@pytest.mark.parametrize('text', [
    'Do not modify unrelated runtime files.',
    'Do not modify unrelated workspaces.',
    'Do not remove test coverage for restart behavior.',
    'Do not modify unrelated files.',
])
@pytest.mark.parametrize('where', ['body', 'comment'])
def test_scoped_constraints_recover(conn, text, where):
    tid = _stopped(conn, text if where == 'body' else 'Repair local index.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', text)
    assert _recovers(conn, tid)


@pytest.mark.parametrize('text', [
    'Do not silently restart.',
    'Do not modify unrelated files or restart.',
    'Do not modify unrelated files, and do not proceed.',
    'Do not touch config and then quietly redeploy.',
    'Do not proceed.',
    "Don't restart until Chris approves.",
])
def test_governed_negations_hold(conn, text):
    assert not _recovers(conn, _stopped(conn, text))

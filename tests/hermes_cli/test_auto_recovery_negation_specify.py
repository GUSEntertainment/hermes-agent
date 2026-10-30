"""t_df999f30 round 8: coordinated/modified negations and no-op specify provenance."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import auto_same_owner_recovery as auto


@pytest.fixture
def conn(tmp_path):
    with kb.connect_closing(db_path=tmp_path / 'k.db') as c:
        yield c


def _stopped(c, body='Repair local index.'):
    tid = kb.create_task(c, title='Repair local index', body=body, assignee='riley')
    kb.recompute_ready(c)
    assert kb.block_task(c, tid, kind='capability', reason='Missing local fixture')
    return tid


def _status_after_run(c, tid):
    auto.run(c, now=int(time.time()) + 7200)
    return kb.get_task(c, tid).status


@pytest.mark.parametrize('text', ['Do not modify unrelated files or restart.',
                                  'Do not silently restart.',
                                  'Never quietly deploy or merge.'])
@pytest.mark.parametrize('where', ['body', 'comment'])
def test_coordinated_and_modified_negation_holds(conn, text, where):
    tid = _stopped(conn, text if where == 'body' else 'Repair local index.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', text)
    assert _status_after_run(conn, tid) == 'blocked'


def test_atomic_recheck_sees_coordinated_negation(conn):
    tid = _stopped(conn)
    _, verdict, stop = auto.candidates(conn, now=int(time.time()) + 7200)[0]
    assert verdict == 'recover'
    kb.add_comment(conn, tid, 'default', 'Do not silently restart.')
    assert auto.recover(conn, tid, stop) is None


@pytest.mark.parametrize('text', ['Do not modify unrelated files.',
                                  'Do not repeat the same failed approach.'])
def test_pure_scope_constraint_recovers(conn, text):
    assert _status_after_run(conn, _stopped(conn, text)) == 'ready'


@pytest.mark.parametrize('where', ['body', 'comment'])
@pytest.mark.parametrize('spec', ['none', 'assignee'])
def test_answered_hold_survives_nontext_specify(conn, where, spec):
    tid = _stopped(conn, 'Do not proceed.' if where == 'body' else 'Repair local index.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', 'Do not proceed.')
    for _ in range(10):
        assert kb.unblock_task(conn, tid, preserve_failure_history=True)
        assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
        if kb.get_task(conn, tid).status == 'triage':
            break
    assert kb.get_task(conn, tid).status == 'triage'
    kw = {} if spec == 'none' else {'assignee': 'riley-two', 'author': 'default'}
    assert kb.specify_triage_task(conn, tid, **kw)
    assert kb.block_task(conn, tid, kind='capability', reason='Missing final local fixture')
    assert _status_after_run(conn, tid) == 'ready'


def test_new_hold_after_specify_still_holds(conn):
    tid = _stopped(conn)
    for _ in range(10):
        kb.unblock_task(conn, tid, preserve_failure_history=True)
        kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
        if kb.get_task(conn, tid).status == 'triage':
            break
    kb.specify_triage_task(conn, tid, assignee='riley-two', author='default')
    kb.add_comment(conn, tid, 'default', 'Do not proceed.')
    kb.block_task(conn, tid, kind='capability', reason='Missing final local fixture')
    before = kb.get_task(conn, tid).status
    assert before in ('blocked', 'triage')
    assert _status_after_run(conn, tid) == before
    assert kb.claim_task(conn, tid) is None

"""r7: per-field provenance, all text producers, scoped negations (t_df999f30)."""
import hashlib
import time
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import auto_same_owner_recovery as auto

@pytest.fixture
def conn(tmp_path):
    with kb.connect_closing(db_path=tmp_path / 'synthetic.db') as c:
        assert str(tmp_path) in c.execute('PRAGMA database_list').fetchone()[2]
        yield c

def stopped(c, body='Repair local index.', title='Repair local index'):
    tid = kb.create_task(c, title=title, body=body, assignee='riley')
    kb.recompute_ready(c)
    assert kb.block_task(c, tid, kind='capability', reason='Missing local fixture')
    return tid

def run_check(c, tid, held):
    before = kb.get_task(c, tid)
    report = auto.run(c, now=int(time.time()) + 7200)
    after = kb.get_task(c, tid)
    claim = kb.claim_task(c, tid)
    print('RESULT', report, 'STATE', before.status, before.block_kind, after.status, after.block_kind, 'CLAIM', bool(claim))
    if held:
        assert (after.status, after.block_kind) == (before.status, before.block_kind)
        assert claim is None
    else:
        assert after.status == 'ready' and claim is not None

def test_source_identity():
    p = Path(auto.__file__)
    print('SOURCE', str(p), hashlib.sha256(p.read_bytes()).hexdigest())

@pytest.mark.parametrize('producer', ['edit', 'repair'])
@pytest.mark.parametrize('new_body', ['Do not proceed.', 'Repair the updated local index.'])
def test_new_body_after_core_release(conn, producer, new_body):
    tid = stopped(conn)
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    if producer == 'edit':
        assert kb.edit_task(conn, tid, body=new_body)
    else:
        assert kb.repair_idle_task_instructions(conn, tid, expected_body='Repair local index.',
            body=new_body, author='default', reason='Clarify current instruction')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    run_check(conn, tid, held=new_body == 'Do not proceed.')

@pytest.mark.parametrize('hold_field', ['body', 'title'])
@pytest.mark.parametrize('edit_other', [False, True])
def test_answered_field_survives_unrelated_edit(conn, hold_field, edit_other):
    tid = stopped(conn, **{hold_field: 'Do not proceed.'})
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    if edit_other:
        other = 'title' if hold_field == 'body' else 'body'
        assert kb.edit_task(conn, tid, **{other: 'Repair the updated local index.'})
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    run_check(conn, tid, held=False)

@pytest.mark.parametrize('text', ['Repair the local index. Do not modify unrelated files.',
                                  'Repair the local index; do not repeat the same failed approach.'])
@pytest.mark.parametrize('where', ['body', 'comment'])
def test_ordinary_scope_constraint_not_global_stop(conn, text, where):
    tid = stopped(conn, body=text if where == 'body' else 'Repair local index.')
    if where == 'comment':
        kb.add_comment(conn, tid, 'default', text)
    run_check(conn, tid, held=False)

def test_new_comment_hold_after_release(conn):
    tid = stopped(conn)
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    kb.add_comment(conn, tid, 'default', 'Do not proceed.')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    run_check(conn, tid, held=True)

def test_fresh_body_hold(conn):
    tid = stopped(conn, body='Do not proceed.')
    run_check(conn, tid, held=True)


@pytest.mark.parametrize('text', ['Do not retry.', 'Do not proceed without Chris.', "Don't touch this until Ava confirms.", 'Never deploy.'])
def test_progress_negations_still_hold(conn, text):
    tid = stopped(conn, body=text)
    run_check(conn, tid, held=True)


def test_dashboard_patch_null_payload_counts_as_new_text(conn):
    tid = stopped(conn)
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body='Do not proceed.' WHERE id=?", (tid,))
        conn.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?, 'edited', NULL, ?)", (tid, int(time.time())))
    assert kb.block_task(conn, tid, kind='capability', reason='Missing different local fixture')
    run_check(conn, tid, held=True)

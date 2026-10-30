"""Review-only isolated transition probes; never opens a live board."""
from pathlib import Path
import hashlib
import pytest
from hermes_cli import kanban_db as kb
from gateway import owner_attention as attention

@pytest.mark.parametrize('location', ['body', 'fenced_body', 'comment', 'stop_reason'])
def test_explicit_hold_preserved(tmp_path, monkeypatch, location):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    hold = 'Do not proceed until Chris approves the payment.'
    body = hold if location == 'body' else ('```text\n' + hold + '\n```' if location == 'fenced_body' else '')
    with kb.connect_closing(tmp_path / 'synthetic.db') as conn:
        tid = kb.create_task(conn, title='Ordinary technical repair', body=body, assignee='riley')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer='riley') is not None
        if location == 'comment':
            kb.add_comment(conn, tid, author='user', body=hold)
        reason = 'worker capability admission failed' + ('; ' + hold if location == 'stop_reason' else '')
        kb.block_task(conn, tid, kind='capability', reason=reason)
        event = [e for e in kb.list_events(conn, tid) if e.kind == 'blocked'][-1]
        attention._route_to_operator(conn, tid, event.id, None)
        actual = kb.get_task(conn, tid)
        print('LOCATION', location, 'STATUS', actual.status, 'OWNER', actual.assignee)
        assert actual.status == 'blocked'
        assert actual.assignee == 'riley'

def test_import_identity():
    for module in (kb, attention):
        path = Path(module.__file__).resolve()
        print('IMPORTED', path, hashlib.sha256(path.read_bytes()).hexdigest())

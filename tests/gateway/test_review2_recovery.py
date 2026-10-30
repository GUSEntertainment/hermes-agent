"""Independent review probes; only disposable pytest databases."""
import hashlib
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from gateway import owner_attention as attention

@pytest.mark.parametrize('hold', [None, 'Do not proceed until Chris approves the payment.', 'Do not send external email.'])
def test_technical_retry_keeps_action_holds(tmp_path, monkeypatch, hold):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    with kb.connect_closing(tmp_path / 'synthetic.db') as conn:
        tid = kb.create_task(conn, title='Ordinary technical repair', assignee='riley')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer='riley') is not None
        if hold:
            kb.add_comment(conn, tid, author='user', body=hold)
        kb.block_task(conn, tid, kind='capability', reason='worker capability admission failed')
        ev = [e for e in kb.list_events(conn, tid) if e.kind == 'blocked'][-1]
        attention._route_to_operator(conn, tid, ev.id, None)
        actual = kb.get_task(conn, tid).status
        print('PROBE', repr(hold), actual)
        assert actual == ('blocked' if hold else 'ready')

def test_import_identity():
    for module in (kb, attention):
        p = Path(module.__file__).resolve()
        print('IMPORTED', str(p), hashlib.sha256(p.read_bytes()).hexdigest())

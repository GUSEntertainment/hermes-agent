"""Independent checks at producer/consumer boundaries of the frozen bundle."""
import json
from pathlib import Path

from hermes_cli import kanban_db as kb, worker_readiness as wr
from tests.hermes_cli.test_worker_readiness import board, contract, startup_failure


def test_variable_stderr_does_not_reset_unchanged_source_repair_budget(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Authorized CSV implementation', assignee='worker', owner_cell_contract=contract())
    for attempt in range(3):
        run = startup_failure(conn, tid, observed=False)
        # Same source/failure, but routine traceback paths differ between runs.
        raw = f'Traceback from /tmp/worker-{attempt}/startup.py\nRuntimeError: same startup failure\n'.encode()
        payload = {'returncode': 1, **kb._safe_private_worker_startup_diagnostic(raw)}
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_startup_diagnostic', payload, run_id=run)
        recovered = wr.recover_failed_startups(conn)
        assert recovered == ([tid] if attempt < 2 else []), 'Variable stderr was counted as a new source revision'
        if attempt < 2:
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_queued' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            prep_id = json.loads(row[0])['preparation_task']
            assert kb.complete_task(conn, prep_id, metadata={'readiness_repair': {'requirements': {}, 'evidence': 'Verified bounded repair attempt; no source changes.'}})


def test_admission_moves_active_source_ahead_of_older_pythonpath(board, tmp_path, monkeypatch):
    conn, _ = board
    stale = tmp_path / 'older-release'
    package = stale / 'agent'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('raise RuntimeError("older release selected")\n')
    active = Path(kb.__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH', str(stale) + ':' + str(active))
    tid = kb.create_task(conn, title='Exact release admission', assignee='worker')
    admission = kb._build_worker_admission(kb.get_task(conn, tid), 'worker')
    assert admission.contract['profile'] == 'worker'


def test_resubscribing_without_metadata_preserves_explicit_topic_routing(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Explicit content notification', assignee='worker')
    destination = dict(task_id=tid, platform='telegram', chat_id='fixture', thread_id='42')
    metadata = {'thread_id': '42', 'direct_messages_topic_id': '42', 'telegram_dm_topic_reply_fallback': True, 'telegram_reply_to_message_id': '17'}
    kb.add_notify_sub(conn, **destination, delivery_metadata=metadata, inherit_children=True)
    kb.add_notify_sub(conn, **destination)
    row = kb.list_notify_subs(conn, tid, include_internal_metadata=True)[0]
    assert all(row['delivery_metadata'].get(k) == v for k, v in metadata.items()), 'Idempotent subscription erased topic routing'
    assert row['delivery_metadata']['_kanban_inherit_to_children'] is True


def test_stderr_eof_before_nonzero_exit_retains_startup_diagnostic(board):
    import subprocess
    import sys
    import time
    conn, _ = board
    tid = kb.create_task(conn, title='Joined private startup fixture', assignee='worker')
    task = kb.claim_task(conn, tid)
    # A runtime can close stderr during cleanup shortly before its exit status
    # becomes observable. EOF itself is not proof of successful startup.
    code = 'import os,time,sys; os.write(2,b"RuntimeError: startup failed\\n"); os.close(2); time.sleep(.3); sys.exit(7)'
    proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    kb._monitor_private_worker_startup_stderr(board=None, task_id=tid, run_id=task.current_run_id, proc=proc)
    assert proc.wait(timeout=5) == 7
    deadline = time.monotonic() + 1
    found = None
    while time.monotonic() < deadline:
        found = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_startup_diagnostic'", (tid,)).fetchone()
        if found:
            break
        time.sleep(.02)
    assert found is not None, 'stderr EOF discarded a subsequent nonzero startup exit'

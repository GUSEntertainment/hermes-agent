"""Real child exits must survive the private stderr observer and reach dispatch."""
import subprocess
import sys
import time
import pytest
from hermes_cli import kanban_db as kb

@pytest.mark.parametrize('exit_code,kind', [(7,'nonzero_exit'),(0,'clean_exit'),(75,'rate_limited')])
def test_late_child_exit_is_classified_without_private_text(tmp_path,monkeypatch,exit_code,kind):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(kb,'PRIVATE_WORKER_STARTUP_DIAGNOSTIC_SECONDS',0.01)
    conn=kb.connect()
    tid=kb.create_task(conn,title='Synthetic late exit',body='Synthetic fixture only',assignee='fixture')
    task=kb.claim_task(conn,tid)
    proc=subprocess.Popen([sys.executable,'-c',f'import sys,time; time.sleep(.1); sys.stderr.write("RuntimeError: private.person@example.com\\n"); sys.exit({exit_code})'],stderr=subprocess.PIPE,stdout=subprocess.DEVNULL)
    kb._set_worker_pid(conn,tid,proc.pid)
    kb._monitor_private_worker_startup_stderr(board=None,task_id=tid,run_id=task.current_run_id,proc=proc)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        rows=conn.execute("select payload from task_events where task_id=? and kind='worker_exit_diagnostic'",(tid,)).fetchall()
        if rows:break
        time.sleep(.02)
    assert rows, 'late exit diagnostic missing'
    assert 'private.person' not in str([tuple(r) for r in rows])
    assert kb._classify_worker_exit(proc.pid)==(kind,exit_code)
    conn.close()

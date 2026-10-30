"""Local startup probe timeouts stay bounded, visible and distinct from denial."""
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from hermes_cli import admission_timing, kanban_db as kb, worker_readiness
from tests.hermes_cli.test_worker_skill_sync_admission import board


@pytest.mark.parametrize('lane', ['ready', 'review'])
@pytest.mark.parametrize('stage', ['profile_skill_sync', 'discovery_subprocess'])
def test_timeout_blocks_once_without_worker_or_repair_and_preserves_lane(board, monkeypatch, lane, stage):
    conn, task, *_ = board
    if lane == 'review':
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task.id,))
    calls = []
    def probe(argv, **kwargs):
        current = 'profile_skill_sync' if argv[-1] == kb._WORKER_SKILL_SYNC_CODE else 'discovery_subprocess'
        calls.append(current)
        assert kwargs['timeout'] == kb.KANBAN_SKILL_PREFLIGHT_TIMEOUT_SECONDS == 180
        if current == stage:
            raise subprocess.TimeoutExpired(['SECRET_COMMAND'], 60, output='SECRET_OUTPUT', stderr='SECRET_ERROR')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(kb.subprocess, 'run', probe)
    monkeypatch.setattr(kb, '_memory_pressure_level', lambda *a, **kw: 'unknown')
    monkeypatch.setattr(worker_readiness, 'queue_preparation', lambda *a, **kw: pytest.fail('No model repair for a local timeout'))
    spawned=[]
    def spawn(*args, admission_contract=None, **kwargs):
        spawned.append(args)
    result = kb.dispatch_once(conn, spawn_fn=spawn)
    current = kb.get_task(conn, task.id)
    assert result.capability_blocked == [task.id]
    assert current.status == 'blocked' and current.block_kind == 'transient'
    assert current.claim_lock is None and current.current_run_id is None and current.worker_pid is None
    assert not spawned
    assert 'Temporary worker startup check timed out' in current.last_failure_error
    assert 'No worker started' in current.last_failure_error
    row=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
    payload=json.loads(row[0])
    assert payload['kind']=='transient' and payload['source_status']==lane
    assert payload['probe_timeout']['stage']==stage
    assert payload['probe_timeout']['timeout_seconds']==kb.KANBAN_SKILL_PREFLIGHT_TIMEOUT_SECONDS==180
    assert payload['probe_timeout']['elapsed_seconds']>=0
    assert 'SECRET' not in row[0]+current.last_failure_error
    assert kb._resume_status_from_events(conn, task.id)==lane
    assert kb._has_sticky_block(conn,task.id)
    assert kb.recompute_ready(conn)==0
    calls_before=list(calls)
    kb.dispatch_once(conn, spawn_fn=spawn)
    assert calls==calls_before
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked'", (task.id,)).fetchone()[0]==1


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_real_unavailable_provider_remains_capability_denial(board, monkeypatch, lane):
    conn, task, *_ = board
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=?,provider_override=?,model_override=? WHERE id=?',
                     (lane,'unavailable-provider','unavailable-model',task.id))
    task=kb.get_task(conn,task.id)
    with pytest.raises(kb.WorkerAdmissionError) as result:
        kb._build_worker_admission(task,'worker')
    error=result.value
    assert error.probe_timeout is None
    assert kb._block_worker_admission(conn,task,lane,error.admission,probe_timeout=error.probe_timeout)
    current=kb.get_task(conn,task.id)
    assert current.block_kind=='capability'
    payload=json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()[0])
    assert payload['reason']==kb.CAPABILITY_BLOCK_REASON
    assert 'probe_timeout' not in payload
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0


def test_timeout_hold_loses_stale_task_cas(board):
    conn, task, *_=board
    admission=kb._rejected_worker_admission(task,'worker',None,str(board[3]))
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET title=? WHERE id=?',('New owner instruction',task.id))
    assert not kb._block_worker_admission(conn,task,'ready',admission,probe_timeout={
        'stage':'profile_skill_sync','timeout_seconds':60,'elapsed_seconds':60.1})
    assert kb.get_task(conn,task.id).status=='ready'
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='capability_preflight_blocked'").fetchone()[0]==0


def test_skill_sync_timing_is_allowlisted_and_nonsecret(caplog):
    with caplog.at_level(logging.INFO, logger='hermes.admission_timing'):
        admission_timing.emit('profile_skill_sync',60.125,'failed','t_12345678')
    payload=json.loads(caplog.records[-1].message.split(admission_timing.PREFIX,1)[1])
    assert payload=={'phase':'profile_skill_sync','seconds':60.125,'outcome':'failed','task':'t_12345678'}

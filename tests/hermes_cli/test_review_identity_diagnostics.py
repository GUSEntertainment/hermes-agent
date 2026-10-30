"""Safe predicate names preserve all process-bound review rejection gates."""
import os
import pytest
from hermes_cli import kanban_db as kb

@pytest.fixture
def claimed(tmp_path):
    c=kb.connect(tmp_path/'board.db');tid=kb.create_task(c,title='Identity diagnostics',assignee='maker');m=kb.claim_task(c,tid)
    assert kb.request_review(c,tid,reviewer='reviewer',summary='ready',expected_run_id=m.current_run_id,review_decision_contract=kb.REVIEW_DECISION_CONTRACT_V1)
    r=kb.claim_review_task(c,tid);assert kb._set_worker_pid(c,tid,os.getpid())
    yield c,tid,r
    c.close()

@pytest.mark.parametrize('case',['task_start_mismatch','run_start_mismatch','claim_expiry_mismatch','profile_mismatch','caller_pid_mismatch','expected_run_mismatch','run_source_not_review','worker_process_unavailable'])
def test_named_rejection_retains_authority_and_state(claimed,monkeypatch,case):
    c,tid,r=claimed;expected=r.current_run_id
    if case in {'task_start_mismatch','run_start_mismatch'}:
        table='tasks' if case.startswith('task_') else 'task_runs';value=tid if table=='tasks' else r.current_run_id
        c.execute(f'UPDATE {table} SET worker_process_start=worker_process_start+1 WHERE id=?',(value,));c.commit()
    elif case=='claim_expiry_mismatch':c.execute('UPDATE task_runs SET claim_expires=claim_expires+1 WHERE id=?',(r.current_run_id,));c.commit()
    elif case=='profile_mismatch':c.execute('UPDATE tasks SET assignee=? WHERE id=?',('different-reviewer',tid));c.commit()
    elif case=='caller_pid_mismatch':
        pid=os.getpid();monkeypatch.setattr(kb.os,'getpid',lambda:pid+1)
    elif case=='expected_run_mismatch':expected+=1
    elif case=='run_source_not_review':c.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND run_id=? AND kind='claimed'",(tid,r.current_run_id));c.commit()
    elif case=='worker_process_unavailable':monkeypatch.setattr(kb,'_worker_process_identity',lambda pid:None)
    before=[tuple(x) for x in c.execute('SELECT * FROM tasks')]
    with pytest.raises(ValueError,match='review decision capability does not match active review run') as err:kb._validate_review_decision_capability(c,tid,r.review_decision_capability,expected_run_id=expected)
    assert case in str(err.value);assert r.review_decision_capability not in str(err.value)
    assert before==[tuple(x) for x in c.execute('SELECT * FROM tasks')];assert kb._has_review_decision_capability(c,tid,r.review_decision_capability)

def test_valid_live_identity_still_passes_without_consumption(claimed):
    c,tid,r=claimed
    assert kb._validate_review_decision_capability(c,tid,r.review_decision_capability,expected_run_id=r.current_run_id)['run_id']==r.current_run_id
    assert kb._has_review_decision_capability(c,tid,r.review_decision_capability)


def test_real_clock_adjustment_between_dispatcher_and_reviewer_keeps_identity(claimed,monkeypatch):
    import psutil
    if not hasattr(psutil._psplatform,'INIT_BOOT_TIME'):
        pytest.skip('macOS wall-clock adjustment path')
    c,tid,r=claimed
    raw=psutil.Process(os.getpid())._ident[1]
    boot=psutil.boot_time()
    monkeypatch.setattr(psutil._psplatform,'INIT_BOOT_TIME',boot+1)
    assert psutil.Process(os.getpid()).create_time()==raw+1
    assert kb._set_worker_pid(c,tid,os.getpid())
    # A fresh reviewer imports psutil after the adjustment, unlike the gateway.
    monkeypatch.setattr(psutil._psplatform,'INIT_BOOT_TIME',boot)
    assert psutil.Process(os.getpid()).create_time()==raw
    assert kb._validate_review_decision_capability(c,tid,r.review_decision_capability,expected_run_id=r.current_run_id)['run_id']==r.current_run_id


def test_recycled_pid_with_different_raw_start_is_rejected(claimed,monkeypatch):
    import psutil
    c,tid,r=claimed;pid=os.getpid();old=psutil.Process(pid)._ident
    class ReusedProcess:
        _ident=(pid,old[1]+0.001)
        def is_running(self):return True
        def status(self):return 'running'
        def create_time(self):return old[1]  # A matching presentation value is not authority.
    monkeypatch.setattr(psutil,'Process',lambda value:ReusedProcess())
    with pytest.raises(ValueError,match='task_start_mismatch'):
        kb._validate_review_decision_capability(c,tid,r.review_decision_capability,expected_run_id=r.current_run_id)
    assert kb._has_review_decision_capability(c,tid,r.review_decision_capability)


def test_missing_stable_identity_fails_closed(monkeypatch):
    import psutil
    class UnsupportedProcess:
        def is_running(self):return True
        def status(self):return 'running'
        def create_time(self):return 12345.0
    monkeypatch.setattr(psutil,'Process',lambda value:UnsupportedProcess())
    assert kb._worker_process_identity(os.getpid()) is None

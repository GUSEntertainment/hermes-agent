"""Native ended-maker review recovery through real owner/member control; offline."""
import json

import pytest

from hermes_cli import board_owner_manage as control, kanban_db as kb
from hermes_cli import kanban_completion as completion, owner_work_start as start
from tests.hermes_cli.test_ordinary_review_recovery import held, member_scope

BUDGET = {'max_runs_per_day': 3, 'max_attempts_per_input': 1,
          'worker_max_turns': 24, 'worker_run_seconds': 600,
          'worker_total_tokens': 400000, 'worker_request_tokens': 128000}
INSTRUCTION = 'Bundled skills now sync before admission. Review the existing maker artifacts; preserve findings and do not send.'


@pytest.fixture
def native(held):
    conn = held['conn']
    tid = kb.create_task(conn, title='Review existing transcripts', body='Independently inspect the existing output. Do not send.', assignee='claire')
    with kb.write_txn(conn):
        maker = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES(?,'claire','running',1)",(tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='running',current_run_id=?,worker_budget_json=? WHERE id=?", (maker,json.dumps({'execution_budget':BUDGET}),tid))
    completion.record_admission(conn,tid,completion.task_limits({},kb.get_task(conn,tid)))
    assert kb.request_review(conn,tid,summary='Existing complete transcript artifacts',reviewer='grace',
        metadata={'artifacts':[{'path':str(held['artifact']),'sha256':kb._profile_config_digest(str(held['profile_home']))}]},expected_run_id=maker)
    with kb.write_txn(conn):
        reviewer = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES(?,'grace','running',3)",(tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='running',current_run_id=? WHERE id=?",(reviewer,tid))
        kb._append_event(conn,tid,'claimed',{'source_status':'review'},run_id=reviewer)
    completion.record_admission(conn,tid,completion.task_limits({},kb.get_task(conn,tid)))
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET status='blocked',outcome='blocked',ended_at=4 WHERE id=?",(reviewer,))
        conn.execute("UPDATE tasks SET status='blocked',current_run_id=NULL,block_kind='capability',block_recurrences=1,consecutive_failures=2,last_failure_error='Profile changed' WHERE id=?",(tid,))
        kb._append_event(conn,tid,'blocked',{'source_status':'review','reason':'Profile changed before review; do not send.'},run_id=reviewer)
    kb.add_comment(conn,tid,'grace','Preserve prior concerns; startup failed before inspection.')
    return {**held,'task':tid,'maker':maker,'run':reviewer}


def request(native, **kw):
    return control.execute(dict(action='work',task_id=native['task'],board=native['board'],reason=INSTRUCTION,**kw))


def snapshot(native):
    conn,tid=native['conn'],native['task']
    return {t:[dict(r) for r in conn.execute(f'SELECT * FROM {t} WHERE '+('id' if t=='tasks' else 'task_id')+'=?',(tid,))]
            for t in ('tasks','task_runs','task_events','task_comments','owner_cell_goals','review_verdicts')}


@pytest.mark.parametrize('status',['blocked','triage'])
def test_native_review_preview_apply_keeps_maker_and_reviewer(native,status):
    conn,tid=native['conn'],native['task']
    conn.execute('UPDATE tasks SET status=? WHERE id=?',(status,tid));conn.commit()
    before=snapshot(native)
    assert request(native,dry_run=True)['proposed_status']=='review'
    assert snapshot(native)==before
    result=request(native)
    assert result['status']=='review' and result['work_order']['phase']=='review'
    after=snapshot(native)
    for name in ('task_runs','task_comments','owner_cell_goals','review_verdicts'):
        assert after[name]==before[name]
    assert after['tasks'][0]['assignee']=='grace'
    for key in ('block_kind','block_recurrences','consecutive_failures','last_failure_error','body'):
        assert after['tasks'][0][key]==before['tasks'][0][key]
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('unblocked','triage_resumed')",(tid,)).fetchone()
    assert start.allows_claim(conn,kb.get_task(conn,tid))
    assert completion._independent_review_binding(conn,tid)['producer_run_id']==native['maker']


def test_new_review_instruction_one_attempt_keeps_daily_and_same_input_limits(native):
    conn,tid=native['conn'],native['task']
    request(native)
    caps=completion.task_limits({},kb.get_task(conn,tid))
    assert completion.admission_reason(conn,tid,caps) is None
    assert completion.admission_reason(conn,tid,{**caps,'_combined_max_runs_per_day':2})=='completion_daily_run_budget'
    completion.record_admission(conn,tid,caps)
    assert completion.admission_reason(conn,tid,caps)=='completion_daily_run_budget'
    assert completion.admission_reason(conn,tid,{**caps,'max_runs_per_day':4})=='completion_repeated_input_budget'
    with kb.write_txn(conn):
        run=conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES(?,'grace','blocked',5,6,'blocked')",(tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?",(tid,))
        kb._append_event(conn,tid,'blocked',{'source_status':'review','reason':'Another startup failure'},run_id=run)
    before=snapshot(native)
    with pytest.raises(ValueError,match='already received an attempt'):
        request(native)
    assert snapshot(native)==before
    assert len([e for e in before['task_events'] if e['kind']=='completion_admission'])==3


@pytest.mark.parametrize('damage',['active','maker_outcome','wrong_reviewer','missing_handoff','superseded','dependency'])
def test_native_review_rejects_invalid_binding_without_mutation(native,damage):
    conn,tid=native['conn'],native['task']
    if damage=='active':conn.execute('UPDATE tasks SET worker_pid=99 WHERE id=?',(tid,))
    if damage=='maker_outcome':conn.execute("UPDATE task_runs SET outcome='failed' WHERE id=?",(native['maker'],))
    if damage=='wrong_reviewer':conn.execute("UPDATE tasks SET assignee='claire' WHERE id=?",(tid,))
    if damage=='missing_handoff':conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='review_requested'",(tid,))
    if damage=='superseded':kb._append_event(conn,tid,'changes_requested',{'reason':'Actual artifact revision needed'})
    if damage=='dependency':
        conn.commit(); parent=kb.create_task(conn,title='Real pending prerequisite',assignee='claire');kb.link_tasks(conn,parent,tid)
    conn.commit();before=snapshot(native)
    with pytest.raises(ValueError):request(native)
    assert snapshot(native)==before


def test_member_native_review_uses_only_enrolled_board_and_rechecks_grant(native,monkeypatch):
    grant,token=member_scope(native,monkeypatch)
    try:
        before=snapshot(native)
        with pytest.raises(PermissionError,match='Board outside'):
            control.execute(dict(action='work',task_id=native['task'],board='another-board',reason=INSTRUCTION))
        assert snapshot(native)==before
        assert request(native)['status']=='review'
    finally:control._member_context.reset(token)


def test_native_recovery_refuses_profile_change_during_admission(native,monkeypatch):
    def changed(*a,**kw):
        value=native['build'](*a,**kw)
        (native['profile_home']/'config.yaml').write_text('model: changed\n')
        return value
    monkeypatch.setattr(kb,'_build_worker_admission',changed)
    before=snapshot(native)
    with pytest.raises(ValueError,match='Fresh exact'):request(native)
    assert snapshot(native)==before


def test_legacy_admission_after_current_work_order_still_consumes_revision(native):
    conn,tid=native['conn'],native['task']
    request(native)
    caps=completion.task_limits({},kb.get_task(conn,tid))
    completion.record_admission(conn,tid,caps)
    with kb.write_txn(conn):
        # Older receipts had no revision field. Even if launch-surface inputs
        # later change, their matching review binding still consumes this order.
        row=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='completion_admission' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
        receipt=json.loads(row['payload']);receipt.pop('owner_work_revision')
        receipt['input_fingerprint']='legacy-fingerprint'
        conn.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps(receipt),row['id']))
    assert completion.admission_reason(conn,tid,{**caps,'max_runs_per_day':4})=='completion_repeated_input_budget'

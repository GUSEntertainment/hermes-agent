"""Native ordinary review recovery on disposable boards; no model or worker runs."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import board_owner_manage as control, kanban_db as kb, owner_cell as oc
from hermes_cli import owner_board_recovery as recovery
from tests.hermes_cli.test_owner_cell_qa_regressions import contract


@pytest.fixture
def held(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(control, 'ROOT', home)
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_HOME', 'HERMES_KANBAN_BOARD',
                'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_PROFILE', 'HERMES_KANBAN_TASK',
                'HERMES_KANBAN_RUN_ID', 'HERMES_SESSION_SOURCE', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(oc, '_installed_profile', lambda value: value)
    profile_home = home / 'profiles' / 'grace'
    profile_home.mkdir(parents=True)
    (profile_home / 'config.yaml').write_text('model:\n  default: synthetic\n')
    board = 'member-home'
    kb.init_db(board=board)
    conn = kb.connect(board=board)
    artifact = tmp_path / 'review.txt'
    artifact.write_text('Synthetic artifact with unresolved reviewer finding.')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    tid = kb.create_task(conn, title='Synthetic ordinary review', body='Existing authorized local work.',
        assignee='claire', owner_cell_contract=contract(owner_profile='claire',
        runtime_profile='claire', security_realm='private', approval_gates=['external_send']))
    assert kb.request_review(conn, tid, summary='Synthetic handoff', reviewer='grace',
        metadata={'owner_cell_artifact': {'ref': str(artifact), 'digest': digest}})
    oc.record_verdict(conn, tid, reviewer_profile='grace', state_version=1,
        artifact_ref=str(artifact), artifact_digest=digest, verdict='REQUEST_CHANGES',
        defects=['Existing unresolved finding must survive.'])
    with kb.write_txn(conn):
        conn.execute("UPDATE owner_cell_goals SET review_loop_count=1 WHERE task_id=?", (tid,))
        conn.execute("UPDATE tasks SET status='triage',block_kind='capability',block_recurrences=2,consecutive_failures=1 WHERE id=?", (tid,))
        run_id = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES(?,'grace','blocked',1,2,'blocked')", (tid,)).lastrowid
        kb._append_event(conn, tid, 'block_loop_detected', {'source_status':'review', 'kind':'capability',
            'recurrences':kb.BLOCK_RECURRENCE_LIMIT, 'limit':kb.BLOCK_RECURRENCE_LIMIT,
            'reason':'Synthetic reviewer access failure, now repaired.'}, run_id=run_id)
    comment = kb.add_comment(conn, tid, 'grace', 'Existing findings require a corrected artifact and fresh review.')
    calls = []

    def build(task, profile, *, board=None, **kwargs):
        # Stub discovery only. The real transition still checks current Task,
        # configured profile and database identity with a real WorkerAdmission.
        assert task.status == 'review' and task.assignee == profile == 'grace'
        calls.append((task.id, profile, board))
        profile_digest = kb._profile_config_digest(str(profile_home))
        value = dict(version='hermes.worker-admission/v1', status='accepted', task=task.id,
            profile=profile, profile_home=str(profile_home), profile_config_digest=profile_digest,
            board_database=kb._database_identity_contract(kb.kanban_db_path(board=board)))
        digest = hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
        return kb.WorkerAdmission(value, digest, kb._task_admission_snapshot(task), profile_digest)

    monkeypatch.setattr(kb, '_build_worker_admission', build)
    yield dict(conn=conn, task=tid, board=board, artifact=artifact, run=run_id, comment=comment,
               build=build, calls=calls, profile_home=profile_home)
    conn.close()


def args(held, dry=False):
    conn, tid = held['conn'], held['task']
    view = control.describe(conn, tid)
    evidence = {key:view[key] for key in ('expected_task_sha256','expected_goal_sha256',
        'expected_stop_event_id','expected_latest_comment_id')}
    evidence['evidence_comment_ids'] = [held['comment']]
    return dict(action='recover_review',task_id=tid,board=held['board'],
        expected_state=view['expected_state'],evidence=evidence,dry_run=dry,
        reason='Owner resumes the existing bound review after correcting access; findings remain unresolved.')


def state(held):
    conn, tid = held['conn'], held['task']
    return {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} WHERE '+
        ('id' if table=='tasks' else 'task_id')+'=?', (tid,))]
        for table in ('tasks','owner_cell_goals','review_verdicts','task_runs','task_events','task_comments')}


def test_native_preview_and_apply_preserve_everything_except_status_and_receipt(held):
    before = state(held)
    assert control.execute(args(held, dry=True))['status'] == 'triage'
    assert state(held) == before
    result = control.execute(args(held))
    assert result['status'] == 'review'
    after = state(held)
    assert after['tasks'] == [dict(before['tasks'][0], status='review')]
    for table in ('owner_cell_goals','review_verdicts','task_runs','task_comments'):
        assert after[table] == before[table]
    assert after['task_events'][:-1] == before['task_events']
    assert after['task_events'][-1]['kind'] == 'review_triage_resumed'
    assert held['calls'] == [(held['task'],'grace',held['board'])] * 2
    assert kb.request_changes(held['conn'], held['task'], reason='No live reviewer claim')[0] is False
    with pytest.raises(oc.OwnerCellGuardError, match='PASS'):
        oc.assert_completion_allowed(held['conn'], held['task'])


@pytest.mark.parametrize('changed', ['artifact','comment','goal','claim','dependency','source_run','stop_event','profile_after_admission','rejected_admission','foreign_admission'])
def test_native_recovery_refuses_stale_or_unadmitted_inputs_without_mutation(held, monkeypatch, changed):
    conn, tid = held['conn'], held['task']
    request = args(held)
    if changed == 'artifact': held['artifact'].write_text('Changed artifact')
    elif changed == 'comment': kb.add_comment(conn, tid, 'claire', 'New direction')
    elif changed == 'goal': conn.execute('UPDATE owner_cell_goals SET review_loop_count=2 WHERE task_id=?',(tid,))
    elif changed == 'claim': conn.execute("UPDATE tasks SET claim_lock='active' WHERE id=?",(tid,))
    elif changed == 'dependency':
        parent = kb.create_task(conn,title='Unfinished prerequisite',assignee='claire')
        kb.link_tasks(conn,parent,tid)
    elif changed == 'source_run': conn.execute("UPDATE task_runs SET outcome='failed' WHERE id=?",(held['run'],))
    elif changed == 'stop_event': conn.execute("UPDATE task_events SET payload='{}' WHERE id=?",(request['evidence']['expected_stop_event_id'],))
    else:
        def probe(*a, **kw):
            admission = held['build'](*a, **kw)
            if changed == 'profile_after_admission':
                (held['profile_home']/'config.yaml').write_text('model:\n  default: changed\n')
            elif changed == 'rejected_admission': raise kb.WorkerAdmissionError(admission)
            else: admission.contract['board_database'] = {'path':'foreign'}
            return admission
        monkeypatch.setattr(kb,'_build_worker_admission',probe)
    conn.commit()
    before = state(held)
    with pytest.raises((ValueError,PermissionError,kb.WorkerAdmissionError,oc.OwnerCellGuardError)):
        control.execute(request)
    assert state(held) == before


def member_scope(held, monkeypatch):
    grant = dict(principal='newmember',profile='newmember',board=held['board'],fingerprint='a'*64)
    monkeypatch.setattr(control,'refresh_member_grant',lambda value: dict(grant))
    return grant, control._member_context.set(grant)


def test_signed_member_ordinary_review_is_own_board_only(held, monkeypatch):
    grant, token = member_scope(held, monkeypatch)
    try:
        request = args(held)
        request['board'] = 'another-member-home'
        with pytest.raises(PermissionError,match='outside authenticated'):
            control.execute(request)
        assert not held['calls']
        assert control.execute(args(held))['status'] == 'review'
        assert held['calls'] == [(held['task'],'grace',grant['board'])]
    finally:
        control._member_context.reset(token)


@pytest.mark.parametrize('changed', ['database_override','revoked_during_admission','worker','cron','unsigned_profile'])
def test_member_recovery_rechecks_scope_and_rejects_worker_identity(held,monkeypatch,changed,tmp_path):
    grant, token = member_scope(held, monkeypatch)
    try:
        request = args(held)
        if changed == 'database_override': monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'other.db'))
        elif changed == 'worker': monkeypatch.setenv('HERMES_KANBAN_TASK',held['task'])
        elif changed == 'cron': monkeypatch.setenv('HERMES_SESSION_SOURCE','cron')
        elif changed == 'unsigned_profile':
            control._member_context.reset(token)
            token = None
            monkeypatch.setenv('HERMES_PROFILE','newmember')
        else:
            def revoke(*a,**kw):
                result = held['build'](*a,**kw)
                grant['fingerprint']='b'*64
                return result
            monkeypatch.setattr(kb,'_build_worker_admission',revoke)
        before = state(held)
        with pytest.raises(PermissionError): control.execute(request)
        assert state(held) == before
    finally:
        if token is not None: control._member_context.reset(token)


def test_composed_owner_work_transaction_can_roll_back_recovery(held):
    """The work shortcut can atomically recover then fail without a half-release."""
    request = args(held)
    before = state(held)
    token = control._expected.set(request)
    try:
        with pytest.raises(RuntimeError, match='synthetic later work failure'):
            with kb.write_txn(held['conn']):
                receipt = recovery.recover_review(held['conn'],held['task'],
                    actor='default',reason=request['reason'],dry_run=False,**request['evidence'])
                assert receipt['status'] == 'review'
                raise RuntimeError('synthetic later work failure')
        assert state(held) == before
    finally:
        control._expected.reset(token)


@pytest.mark.parametrize('abort', [True, False])
def test_blocked_review_unblock_composes_without_partial_release(held, abort):
    conn, tid = held['conn'], held['task']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'blocked', {'source_status':'review','kind':'capability',
            'reason':'Synthetic reviewer access was repaired.'}, run_id=held['run'])
    before = state(held)
    try:
        with kb.write_txn(conn):
            assert kb.unblock_task(conn, tid, preserve_failure_history=True)
            assert kb.get_task(conn, tid).status == 'review'
            assert kb.get_task(conn, tid).assignee == 'grace'
            if abort:
                raise RuntimeError('synthetic outer rollback')
    except RuntimeError as exc:
        assert abort and str(exc) == 'synthetic outer rollback'
    after = state(held)
    if abort:
        assert after == before
    else:
        assert after['tasks'] == [dict(before['tasks'][0], status='review')]
        for table in ('owner_cell_goals','review_verdicts','task_runs','task_comments'):
            assert after[table] == before[table]
        assert after['task_events'][:-1] == before['task_events']
        assert after['task_events'][-1]['kind'] == 'unblocked'


@pytest.mark.parametrize('abort', [False, True])
def test_native_blocked_review_preserves_action_holds_and_outer_atomicity(held, abort):
    from gateway.gus_admin_cli import unresolved_hold
    conn, tid = held['conn'], held['task']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'blocked', {'source_status':'review','kind':'capability',
            'reason':'Synthetic reviewer access was repaired.'}, run_id=held['run'])
    kb.add_comment(conn, tid, 'claire', 'External send requires Chris approval; prepare and review locally only.')
    restriction = unresolved_hold(conn, kb.get_task(conn, tid))
    assert restriction
    before = state(held)
    if not abort:
        assert control.execute(args(held, dry=True))['status'] == 'blocked'
        assert state(held) == before
        assert control.execute(args(held))['status'] == 'review'
        after = state(held)
        assert after['tasks'] == [dict(before['tasks'][0], status='review')]
        for table in ('owner_cell_goals','review_verdicts','task_runs','task_comments'):
            assert after[table] == before[table]
        assert after['task_events'][:-1] == before['task_events']
        assert after['task_events'][-1]['kind'] == 'review_work_resumed'
        assert unresolved_hold(conn, kb.get_task(conn, tid)) == restriction
    else:
        request = args(held)
        token = control._expected.set(request)
        try:
            with pytest.raises(RuntimeError, match='synthetic outer failure'):
                with kb.write_txn(conn):
                    recovery.recover_review(conn, tid, actor='default', reason=request['reason'],
                        dry_run=False, **request['evidence'])
                    raise RuntimeError('synthetic outer failure')
            assert state(held) == before
        finally:
            control._expected.reset(token)


def test_review_preserving_mode_is_unavailable_without_bound_owner_admission(held):
    conn, tid = held['conn'], held['task']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    before = state(held)
    with pytest.raises(ValueError, match='bound idle owner review'):
        kb.unblock_task(conn, tid, preserve_action_holds=True)
    assert state(held) == before


@pytest.mark.parametrize('internal_guard',[True,False])
def test_recovery_exposes_only_bounded_internal_readiness_guard(held,monkeypatch,internal_guard):
    def reject(*args,**kwargs):
        try:
            if internal_guard:
                raise oc.OwnerCellGuardError('Review readiness requires the current assigned reviewer')
            raise RuntimeError('PRIVATE_PROVIDER_DETAIL_SENTINEL')
        except Exception as cause:
            raise kb.WorkerAdmissionError() from cause
    monkeypatch.setattr(kb,'_build_worker_admission',reject)
    before=state(held)
    if internal_guard:
        with pytest.raises(ValueError,match='Reviewer readiness: Review readiness requires the current assigned reviewer'):
            control.execute(args(held))
    else:
        with pytest.raises(kb.WorkerAdmissionError) as error:
            control.execute(args(held))
        assert 'PRIVATE_PROVIDER_DETAIL_SENTINEL' not in str(error.value)
    assert state(held)==before

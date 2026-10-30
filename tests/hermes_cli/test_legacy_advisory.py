"""Real SQLite lifecycle coverage for pre-owner-workflow review tasks."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, owner_cell as oc, owner_workflow as ow
from tests.hermes_cli.test_owner_cell_qa_regressions import contract, pass_metadata, disposable_conn

PACKET = 'Synthetic unsigned draft: source checks pass; missing confirmations remain explicit. Review posture only.'


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    c = disposable_conn(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_PROFILE'):
        monkeypatch.delenv(key, raising=False)
    for name in ('scarlett', 'maker', 'claude-review', 'fable-review'):
        (Path(os.environ['HERMES_HOME']) / 'profiles' / name).mkdir(parents=True)
    monkeypatch.setattr(ow, 'settings', lambda: {'departments': {'engineering_systems': {
        'owner': 'scarlett', 'advisory_reviewers': ['claude-review'], 'allow_explicit_fable_waiver': True}}})
    v = contract(runtime_profile='maker', required_reviewer_profile='scarlett',
                 owner_acceptance={'mode': 'accountable_owner'},
                 delegation_roles={'maker': 'maker', 'reviewer': 'scarlett'},
                 approval_gates=['no-external-send'])
    tid = kb.create_task(c, title='Legacy source-backed draft', body='Finish the original bounded draft.',
                         assignee='maker', workspace_kind='dir', workspace_path=str(tmp_path), owner_cell_contract=v)
    f = tmp_path / 'draft.txt'; f.write_text('Synthetic draft; no personal data.\n')
    h = hashlib.sha256(f.read_bytes()).hexdigest()
    assert kb.request_review(c, tid, summary='Draft prepared', reviewer='scarlett',
                             metadata={'owner_cell_artifact': {'ref': str(f), 'digest': h}})
    c.execute('UPDATE owner_cell_goals SET review_loop_count=8 WHERE task_id=?', (tid,)); c.commit()
    before = c.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()[0]
    yield c, tid, f, h, before, monkeypatch
    c.close()


def owner(x):
    c, tid, _, _, _, m = x
    t = kb.claim_review_task(c, tid); assert t
    kb._set_worker_pid(c, tid, os.getpid())
    m.setenv('HERMES_KANBAN_TASK', tid); m.setenv('HERMES_KANBAN_RUN_ID', str(t.current_run_id)); m.setenv('HERMES_PROFILE', 'scarlett')
    return t


def request(x, t, **kw):
    c, tid, *_ = x
    args = dict(expected_run_id=t.current_run_id, review_decision_capability=t.review_decision_capability,
                profile='claude-review', packet=PACKET, summary='Required bounded review')
    args.update(kw)
    return ow.request_advisory(c, tid, **args)


def finish_advisor(x, aid):
    c, _, _, _, _, m = x
    t = kb.claim_task(c, aid); assert t
    kb._set_worker_pid(c, aid, os.getpid())
    m.setenv('HERMES_KANBAN_TASK', aid); m.setenv('HERMES_KANBAN_RUN_ID', str(t.current_run_id)); m.setenv('HERMES_PROFILE', 'claude-review')
    with pytest.raises(oc.OwnerCellGuardError, match='required advisory tool call'):
        kb.complete_task(c, aid, summary='No invocation yet', expected_run_id=t.current_run_id, fire_lifecycle_hook=False)
    # Synthetic external result, through the actual constrained-plugin recorder.
    ow.record_advisory_result(tool='claude_opus_review', packet=PACKET, result='No contradiction in synthetic draft posture.')
    assert kb.complete_task(c, aid, summary='Synthetic findings returned', expected_run_id=t.current_run_id, fire_lifecycle_hook=False)
    kb.recompute_ready(c)


def complete(x, t):
    c, tid, f, h, *_ = x
    return kb.complete_task(c, tid, summary='Reviewed exact draft and findings; local outcome verified',
        metadata=pass_metadata(ref=str(f), digest=h), expected_run_id=t.current_run_id,
        review_decision_capability=t.review_decision_capability, fire_lifecycle_hook=False)


def test_legacy_original_task_completes_after_advisory_without_migration(legacy):
    c, tid, f, h, before, _ = legacy
    t = owner(legacy)
    assert ow.state(c, tid) is None
    assert ow.describe(c, tid)['legacy_advisory_only']
    assert 'Do not migrate' in ow.context(c, tid)
    with pytest.raises(oc.OwnerCellGuardError, match='advisory review.*missing'):
        complete(legacy, t)
    aid = request(legacy, t)['advisory_task']
    child = kb.get_task(c, aid)
    assert str(f) not in child.body and 'source-backed' not in child.body
    assert kb.get_task(c, tid).status == 'todo'
    finish_advisor(legacy, aid)
    assert kb.get_task(c, tid).status == 'review'
    assert complete(legacy, owner(legacy))
    assert kb.get_task(c, tid).status == 'done'
    row = c.execute('SELECT contract_json,review_loop_count FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()
    assert row['contract_json'] == before and row['review_loop_count'] == 8


def test_failed_advisory_returns_to_same_review_and_retry_finishes(legacy):
    c, tid, _, _, before, _ = legacy
    t = owner(legacy); aid = request(legacy, t)['advisory_task']
    a = kb.claim_task(c, aid)
    kb.block_task(c, aid, reason='Synthetic temporarily unavailable tool', kind='capability', expected_run_id=a.current_run_id)
    ow.recover_advisory_failures(c)
    assert kb.get_task(c, tid).status == 'review'
    assert c.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()[0] == 'review'
    t = owner(legacy)
    assert t.review_decision_capability
    with pytest.raises(oc.OwnerCellGuardError, match='unfinished'):
        complete(legacy, t)
    aid2 = request(legacy, t)['advisory_task']; assert aid2 != aid
    ow.recover_advisory_failures(c)
    assert kb.get_task(c, tid).status == 'todo'  # Old failed attempt cannot wake the owner again.
    finish_advisor(legacy, aid2)
    assert complete(legacy, owner(legacy))
    assert c.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()[0] == before


def test_successful_native_advisory_reused_without_second_paid_call(legacy):
    c, tid, *_ = legacy
    aid=request(legacy,owner(legacy))['advisory_task']
    finish_advisor(legacy,aid)
    result=request(legacy,owner(legacy))
    assert result['reused'] and result['advisory_task']==aid
    assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==2
    kb.recompute_ready(c)
    assert complete(legacy,owner(legacy))


@pytest.mark.parametrize('bad', ['no_token', 'wrong_token', 'wrong_run', 'changed_bytes', 'unrequested_profile', 'private_packet'])
def test_invalid_legacy_advisory_creates_no_child_and_preserves_run(legacy, bad):
    c, tid, f, _, before, _ = legacy; t = owner(legacy)
    kwargs = {'no_token': {'review_decision_capability': None}, 'wrong_token': {'review_decision_capability': 'wrong'},
              'wrong_run': {'expected_run_id': t.current_run_id + 1}, 'changed_bytes': {},
              'unrequested_profile': {'profile': 'fable-review', 'confirm_manual': True},
              'private_packet': {'packet': 'Private source /Users/Someone/secret.pdf must never be exported'}}[bad]
    if bad == 'changed_bytes': f.write_text('Changed artifact')
    with pytest.raises((oc.OwnerCellGuardError, PermissionError, ValueError)):
        request(legacy, t, **kwargs)
    assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert kb.get_task(c, tid).current_run_id == t.current_run_id
    assert c.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()[0] == before


@pytest.mark.parametrize('changed', ['body', 'contract'])
def test_old_advisory_receipt_cannot_complete_changed_scope(legacy, changed):
    c, tid, *_ = legacy; aid = request(legacy, owner(legacy))['advisory_task']
    finish_advisor(legacy, aid)
    if changed == 'body':
        c.execute('UPDATE tasks SET body=body || ? WHERE id=?', (' New scope.', tid))
    else:
        r=c.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?',(tid,)).fetchone()[0]
        v=json.loads(r); v['approval_gates'].append('additional-gate')
        c.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?',(json.dumps(v),tid))
    c.commit()
    with pytest.raises(oc.OwnerCellGuardError, match='different artifact or task scope'):
        complete(legacy, owner(legacy))


def test_repeated_advisory_failure_does_not_reset_limit(legacy):
    c, tid, *_ = legacy
    for _ in range(2):
        aid=request(legacy, owner(legacy))['advisory_task']; a=kb.claim_task(c,aid)
        kb.block_task(c,aid,reason='Synthetic same failure',kind='capability',expected_run_id=a.current_run_id)
        ow.recover_advisory_failures(c)
    with pytest.raises(oc.OwnerCellGuardError,match='repeated advisory failure'):
        request(legacy,owner(legacy))
    assert c.execute('SELECT review_loop_count FROM owner_cell_goals WHERE task_id=?',(tid,)).fetchone()[0]==8


def test_legacy_fable_explicit_authorization_and_waiver_binding(legacy):
    c, tid, f, _, _, m = legacy
    m.setattr(ow,'settings',lambda:{'departments':{'engineering_systems':{'owner':'scarlett',
        'advisory_reviewers':['fable-review'],'allow_explicit_fable_waiver':True}}})
    t=owner(legacy)
    with pytest.raises(oc.OwnerCellGuardError,match='explicit Chris authorization'):
        request(legacy,t,profile='fable-review')
    kw=dict(expected_run_id=t.current_run_id,review_decision_capability=t.review_decision_capability,
            profile='fable-review',decision='waive',confirm_manual=True,authorization_ref='synthetic-user-1',
            authorization_text='Waive only this synthetic advisory for this exact draft.',summary='Explicit test waiver')
    assert ow.record_advisory_waiver(c,tid,**kw)
    assert ow.advisory_waiver_status(c,tid)
    assert complete(legacy,t)


def test_configured_optional_advisor_is_required_once_requested(legacy):
    c, tid, _, _, _, m = legacy
    m.setattr(ow,'settings',lambda:{'departments':{'engineering_systems':{'owner':'scarlett',
        'advisory_reviewers':[], 'optional_advisory_reviewers':['claude-review']}}})
    t=owner(legacy)
    assert ow.describe(c,tid)['advisory_reviewers']==[]
    assert ow.describe(c,tid)['available_advisory_reviewers']==['claude-review']
    aid=request(legacy,t)['advisory_task']
    a=kb.claim_task(c,aid)
    kb.block_task(c,aid,reason='Synthetic temporary failure',kind='capability',expected_run_id=a.current_run_id)
    ow.recover_advisory_failures(c)
    t=owner(legacy)
    with pytest.raises(oc.OwnerCellGuardError,match='unfinished'):
        complete(legacy,t)
    aid2=request(legacy,t)['advisory_task']
    finish_advisor(legacy,aid2)
    assert complete(legacy,owner(legacy))

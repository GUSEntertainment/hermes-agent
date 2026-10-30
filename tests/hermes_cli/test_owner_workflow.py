"""Failure-injection acceptance for the department-owned same-task loop."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, owner_cell as oc, owner_workflow as ow
from tools import kanban_tools as kt
from agent.kanban_stop import build_kanban_stop_nudge
from tests.hermes_cli.test_owner_cell_qa_regressions import contract, pass_metadata


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'; home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_TASK', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)
    home.joinpath('config.yaml').write_text('kanban:\n  worker_readiness:\n    enabled: true\n')
    for profile in ('scarlett', 'maker', 'grace'):  # upstream #106163 requires installed reviewer profiles
        folder = home / 'profiles' / profile; folder.mkdir(parents=True)
        folder.joinpath('config.yaml').write_text('model:\n  provider: openai-codex\n  default: test-model\nplatform_toolsets:\n  cli: [file, kanban]\nsecurity:\n  workspace_confinement:\n    enabled: true\n')
    workspace = tmp_path / 'work'; workspace.mkdir()
    brief = workspace / 'brief.md'; brief.write_text('Produce result.txt with all requested fields. Preserve source facts.\n')
    conn = kb.connect()
    value = contract(runtime_profile='maker', owner_acceptance={'mode': 'accountable_owner'}, required_reviewer_profile='scarlett',
                     delegation_roles={'maker': 'maker', 'reviewer': 'scarlett'},
                     owner_workflow={'version': 1, 'max_identical_recoveries': 2, 'advisory_reviewers': []})
    tid = kb.create_task(conn, title='Produce a verified document', body='Retain the supplied address and event date.',
                         assignee='maker', workspace_kind='dir', workspace_path=str(workspace), owner_cell_contract=value)
    yield conn, tid, workspace, brief, monkeypatch
    conn.close()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def independent_production(workflow):
    conn, _, workspace, brief, monkeypatch = workflow
    value = contract(goal_id='independent-repair', objective='Stage separately reviewed infrastructure repair',
                     approval_gates=['deploy', 'restart', 'security'])
    tid = kb.create_task(conn, title='Stage a repair with independent review', body='Stage only.',
        assignee='scarlett', workspace_kind='dir', workspace_path=str(workspace), owner_cell_contract=value)
    return conn, tid, workspace, brief, monkeypatch


@pytest.mark.parametrize('action', ['maker', 'owner'])
def test_extra_handoff_continues_producer_then_requires_independent_review(independent_production, action):
    conn, tid, workspace, _, _ = independent_production
    producer = claimed(independent_production)
    before = dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone())
    count = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    run_count = conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]
    result = json.loads(kt._handle_owner_handoff({'task_id': tid, 'action': action,
        'summary': 'Finish the repair efficiently', 'acceptance_criteria': ['Tested repair']}))
    assert result['ok'] and result['continue_work'] and not result['handoff_performed'], result
    assert result['producer'] == 'scarlett' and result['reviewer'] == 'grace'
    assert kb.get_task(conn, tid).current_run_id == producer.current_run_id
    assert dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()) == before
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == count
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == run_count
    assert build_kanban_stop_nudge(messages=[{'role': 'tool', 'name': 'kanban_handoff',
        'content': json.dumps(result)}]) is not None
    artifact = workspace / 'repair.txt'; artifact.write_text('Verified isolated repair. No activation.\n')
    # Direct production grants neither final acceptance nor a reviewer identity swap.
    rejected = json.loads(kt._handle_request_review({'task_id': tid, 'reviewer': 'scarlett',
        'summary': 'Self approval attempt', 'metadata': {'owner_cell_artifact':
            {'ref': str(artifact), 'digest': digest(artifact)}}}))
    assert not rejected.get('ok'), rejected
    result = json.loads(kt._handle_request_review({'task_id': tid, 'reviewer': 'grace',
        'summary': 'Repair and tests staged, awaiting independent review; no activation.',
        'metadata': {'owner_cell_artifact': {'ref': str(artifact), 'digest': digest(artifact)}}}))
    assert result['ok'] and result['status'] == 'review', result
    reviewer = claimed(independent_production, reviewer=True)
    assert reviewer.assignee == 'grace'
    assert kb.complete_task(conn, tid, summary='Independent verification passed for staged repair only.',
        expected_run_id=reviewer.current_run_id, review_decision_capability=reviewer.review_decision_capability,
        metadata=pass_metadata(ref=str(artifact), digest=digest(artifact)), fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).status == 'done'
    final = json.loads(conn.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()[0])
    assert final['approval_gates'] == ['deploy', 'restart', 'security']
    assert final['required_reviewer_profile'] == 'grace'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == count


@pytest.mark.parametrize('invalid', ['stale_run', 'wrong_profile', 'review_phase'])
def test_direct_producer_fallback_refuses_other_authority(independent_production, invalid):
    conn, tid, _, _, monkeypatch = independent_production
    producer = claimed(independent_production)
    if invalid == 'stale_run':
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(producer.current_run_id + 100))
    elif invalid == 'wrong_profile':
        monkeypatch.setenv('HERMES_PROFILE', 'grace')
    else:
        assert kb.request_review(conn, tid, summary='staged repair', reviewer='grace',
            metadata={'owner_cell_artifact': {'ref': 'fixture', 'digest': 'digest'}},
            expected_run_id=producer.current_run_id)
        claimed(independent_production, reviewer=True)
    before = dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone())
    result = json.loads(kt._handle_owner_handoff({'task_id': tid, 'action': 'owner',
        'summary': 'Cannot bypass review', 'acceptance_criteria': ['Tested repair']}))
    assert not result.get('ok'), result
    assert dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()) == before


def claimed(workflow, reviewer=False):
    conn, tid, _, _, monkeypatch = workflow
    task = kb.claim_review_task(conn, tid) if reviewer else kb.claim_task(conn, tid)
    assert task is not None
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE', task.assignee)
    if reviewer:
        kb._set_worker_pid(conn, tid, os.getpid())
    return task


def handoff(workflow, requirements=None, repair=None):
    conn, tid, _, brief, _ = workflow
    return json.loads(kt._handle_owner_handoff({'task_id': tid, 'brief_ref': str(brief), 'brief_digest': digest(brief),
        'summary': 'The owner verified the executable brief and acceptance checks.',
        'acceptance_criteria': ['Contains the source address', 'Contains the event date'],
        'requirements': requirements or {'toolsets': ['file', 'kanban'], 'read_paths': [str(brief)]}, 'repair': repair}))


def review(workflow, artifact):
    conn, tid, _, _, _ = workflow
    task = kb.get_task(conn, tid)
    assert kb.request_review(conn, tid, summary='Produced artifact verified against the brief', reviewer='scarlett',
                             metadata={'owner_cell_artifact': {'ref': str(artifact), 'digest': digest(artifact)}},
                             expected_run_id=task.current_run_id)
    return claimed(workflow, reviewer=True)


def test_missing_input_then_failed_review_then_verified_delivery_on_one_card(workflow):
    conn, tid, workspace, brief, _ = workflow
    assert kb.get_task(conn, tid).assignee == 'scarlett'
    assert ow.describe(conn, tid)['phase'] == 'owner_intake'
    claimed(workflow)
    source = workspace / 'source.txt'
    failed = handoff(workflow, {'toolsets': ['file', 'kanban'], 'read_paths': [str(source)]})
    assert failed['code'] == 'maker_not_ready', failed
    assert {'code': 'missing_input', 'resource': str(source)} in failed['issues']
    assert kb.get_task(conn, tid).status == 'running'
    source.write_text('Address: 10 Example Street; Date: 2026-10-01\n')
    passed = handoff(workflow, {'toolsets': ['file', 'kanban'], 'read_paths': [str(source)]})
    assert passed.get('phase') == 'execute', passed
    assert kb.get_task(conn, tid).assignee == 'maker'
    assert build_kanban_stop_nudge(messages=[{'role': 'tool', 'name': 'kanban_handoff', 'content': json.dumps(passed)}]) is None
    assert 'Retain the supplied address and event date.' in kb.get_task(conn, tid).body
    claimed(workflow)
    artifact = workspace / 'result.txt'; artifact.write_text('Date: 2026-10-01\n')
    first_review = review(workflow, artifact)
    assert kb.request_changes(conn, tid, reason='Missing the source address. Add it and preserve the date.',
                              expected_run_id=first_review.current_run_id,
                              review_decision_capability=first_review.review_decision_capability)[0]
    assert kb.get_task(conn, tid).assignee == 'maker'
    claimed(workflow)
    artifact.write_text(source.read_text())
    final_review = review(workflow, artifact)
    version = kb.owner_cell_read_state(conn, tid)['current_state_version']
    assert kb.complete_task(conn, tid, summary='Inspected both fields in the exact artifact.',
        expected_run_id=final_review.current_run_id, review_decision_capability=final_review.review_decision_capability,
        metadata={**pass_metadata(ref=str(artifact), digest=digest(artifact), state_version=version),
                  'owner_acceptance_checks': acceptance_checks()}, fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).status == 'done'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert 'Address: 10 Example Street' in artifact.read_text()


def test_review_corrections_survive_history_noise_and_clear_after_pass(workflow):
    conn, tid, workspace, brief, _ = workflow
    owner, artifact = final_review(workflow)
    reason = 'Fix the summary generator; preserve the accepted date. Rebuild and check the generated output.'
    assert kb.request_changes(conn, tid, reason=reason,
        expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability)[0]
    # Generic context has bounded history. The current correction must survive
    # comment noise and an old controlling brief without becoming optional history.
    for i in range(25):
        kb.add_comment(conn, tid, author='maker', body=f'Historical progress note {i}')
    view = ow.describe(conn, tid)
    assert view['review_corrections']['defects'] == [reason]
    assert view['review_corrections']['artifact_digest'] == digest(artifact)
    context = kb.build_worker_context(conn, tid)
    assert reason in context.split('## Body')[0]
    claimed(workflow)
    artifact.write_text('Corrected summary. Address: 10 Example Street; Date: 2026-10-01\n')
    owner = review(workflow, artifact)
    version = kb.owner_cell_read_state(conn, tid)['current_state_version']
    assert kb.complete_task(conn, tid, summary='Verified corrected output.',
        expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability,
        metadata={**pass_metadata(ref=str(artifact), digest=digest(artifact), state_version=version),
                  'owner_acceptance_checks': acceptance_checks()}, fire_lifecycle_hook=False)
    assert ow.describe(conn, tid)['review_corrections'] is None


def test_unchanged_rework_is_corrected_in_same_run_before_reviewer_dispatch(workflow):
    conn, tid, workspace, _, _ = workflow
    owner, artifact = final_review(workflow)
    assert kb.request_changes(conn, tid, reason='Add the missing summary and regenerate the master.',
        expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability)[0]
    maker = claimed(workflow)
    before_events = conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
    # Renaming identical rejected bytes is not progress either.
    renamed = workspace / 'renamed.txt'; renamed.write_bytes(artifact.read_bytes())
    args = {'task_id': tid, 'reviewer': 'scarlett', 'summary': 'Ready again',
            'metadata': {'owner_cell_artifact': {'ref': str(renamed), 'digest': digest(renamed)}}}
    result = json.loads(kt._handle_request_review(args))
    assert 'unchanged' in str(result).lower(), result
    assert 'same run' in str(result).lower(), result
    assert kb.get_task(conn, tid).status == 'running'
    assert kb.get_task(conn, tid).current_run_id == maker.current_run_id
    assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == before_events
    assert kb.claim_review_task(conn, tid) is None
    args['metadata']['owner_cell_artifact']['digest'] = '0' * 64
    result = json.loads(kt._handle_request_review(args))
    assert 'does not match current file bytes' in str(result), result
    assert kb.get_task(conn, tid).current_run_id == maker.current_run_id
    renamed.write_text(artifact.read_text() + 'Summary: address and event date retained.\n')
    args['metadata']['owner_cell_artifact']['digest'] = digest(renamed)
    result = json.loads(kt._handle_request_review(args))
    assert result.get('status') == 'review', result
    assert kb.latest_run(conn, tid).id == maker.current_run_id
    assert kb.latest_run(conn, tid).outcome == 'review_requested'


def test_new_owner_handoff_can_reconsider_old_artifact_without_losing_review_notes(workflow):
    conn, tid, _, brief, _ = workflow
    owner, artifact = final_review(workflow)
    assert kb.request_changes(conn, tid, reason='Verify the accepted source date before rewriting.',
        expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability)[0]
    maker = claimed(workflow)
    assert kb.block_task(conn, tid, kind='capability', reason='Owner must reconcile conflicting source instructions.',
                         expected_run_id=maker.current_run_id)
    claimed(workflow)
    brief.write_text('Verified original source: the existing artifact has the correct date. Recheck it unchanged.\n')
    assert handoff(workflow, repair={
        'cause': 'Review used an outdated date.', 'change': 'Verified and corrected the source instruction.',
        'verification': 'Compared both source versions.', 'prevention': 'Use the current dated source.',
    }).get('phase') == 'execute'
    claimed(workflow)
    assert ow.describe(conn, tid)['review_corrections']['defects']
    owner = review(workflow, artifact)
    assert owner.assignee == 'scarlett'
    assert kb.get_task(conn, tid).status != 'done'  # New handoff is not final acceptance.


@pytest.fixture
def economical_workflow(workflow):
    conn, tid, _, _, _ = workflow
    value = ow.state(conn, tid)[1]
    value['owner_role_alias'] = 'research'
    conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?', (json.dumps(value), tid))
    conn.commit()
    return workflow


def owner_metadata(artifact):
    return {'owner_final_artifact': {'ref': str(artifact), 'digest': digest(artifact)},
            'owner_acceptance_checks': acceptance_checks()}


def test_small_job_owner_finishes_in_one_native_run_without_maker(economical_workflow):
    conn, tid, workspace, _, _ = economical_workflow
    owner = claimed(economical_workflow)
    original = ow.state(conn, tid)[1]
    result = json.loads(kt._handle_owner_handoff({'action': 'owner', 'task_id': tid,
        'summary': 'Small factual answer; direct delivery costs less than delegation.',
        'acceptance_criteria': [x['criterion'] for x in acceptance_checks()]}))
    assert result.get('phase') == 'owner_complete', result
    assert result['run_id'] == owner.current_run_id
    assert build_kanban_stop_nudge(messages=[{'role': 'tool', 'name': 'kanban_handoff',
                                             'content': json.dumps(result)}]) is not None
    after = ow.state(conn, tid)[1]
    for field in ('approval_gates', 'security_realm', 'callback', 'targets', 'risk_class'):
        assert after[field] == original[field]
    artifact = workspace / 'answer.md'; artifact.write_text('Address: 10 Example Street; Date: 2026-10-01\n')
    assert kb.complete_task(conn, tid, summary='Compared both source fields.', metadata=owner_metadata(artifact),
        expected_run_id=owner.current_run_id, fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).status == 'done'
    assert len(kb.list_runs(conn, tid)) == 1
    assert conn.execute('SELECT COUNT(*) FROM review_verdicts WHERE task_id=?', (tid,)).fetchone()[0] == 0


def test_second_correction_automatically_retains_owner_and_completes(economical_workflow):
    conn, tid, workspace, _, _ = economical_workflow
    owner, artifact = final_review(economical_workflow)
    assert kb.request_changes(conn, tid, reason='Consolidated first corrections: address and summary.',
        expected_run_id=owner.current_run_id, review_decision_capability=owner.review_decision_capability)[0]
    claimed(economical_workflow)
    artifact.write_text('Address: 10 Example Street; Date: 2026-10-01; summary still missing\n')
    owner = review(economical_workflow, artifact)
    count = len(kb.list_runs(conn, tid))
    assert kb.request_changes(conn, tid, reason='Summary still missing.',
        expected_run_id=owner.current_run_id, review_decision_capability=owner.review_decision_capability)[0]
    assert kb.get_task(conn, tid).current_run_id == owner.current_run_id
    assert kb.get_task(conn, tid).assignee == 'scarlett'
    assert ow.describe(conn, tid)['phase'] == 'owner_complete'
    assert ow.correction_count(conn, tid) == 1
    assert kb.claim_task(conn, tid) is None
    # Owner fixes the remaining defect without a new maker or review process.
    artifact.write_text('Address: 10 Example Street; Date: 2026-10-01; Summary: verified source fields.\n')
    assert kb.complete_task(conn, tid, summary='Owner corrected and verified the final source fields.',
        metadata=owner_metadata(artifact), expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability, fire_lifecycle_hook=False)
    assert len(kb.list_runs(conn, tid)) == count


def test_owner_completion_rejects_false_evidence_stale_run_and_worker(economical_workflow):
    conn, tid, workspace, _, _ = economical_workflow
    owner = claimed(economical_workflow)
    with pytest.raises(oc.OwnerCellGuardError, match='currently claimed'):
        ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id + 1,
            summary='stale', acceptance_criteria=['source fields'])
    assert handoff(economical_workflow).get('phase') == 'execute'
    maker = claimed(economical_workflow)
    with pytest.raises(oc.OwnerCellGuardError, match='currently claimed'):
        ow.start_owner_completion(conn, tid, expected_run_id=maker.current_run_id,
            summary='worker cannot promote itself', acceptance_criteria=['source fields'])
    artifact = workspace / 'answer.md'; artifact.write_text('source fields')
    owner = review(economical_workflow, artifact)
    ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability, summary='Owner will finish this small correction.')
    meta = owner_metadata(artifact); meta['owner_final_artifact']['digest'] = '0' * 64
    with pytest.raises(oc.OwnerCellGuardError, match='owner_final_artifact'):
        kb.complete_task(conn, tid, summary='false hash', metadata=meta,
            expected_run_id=owner.current_run_id, review_decision_capability=owner.review_decision_capability)
    assert kb.get_task(conn, tid).status == 'running'


def test_specialist_and_engineering_review_cannot_be_replaced(workflow):
    conn, tid, _, _, _ = workflow
    owner = claimed(workflow)
    with pytest.raises(oc.OwnerCellGuardError, match='specialist review'):
        ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id,
            summary='Do not bypass engineering review.', acceptance_criteria=['original criteria'])
    assert ow.describe(conn, tid)['phase'] == 'owner_intake'


def test_takeover_result_does_not_falsely_end_the_owner_run(economical_workflow):
    _, tid, _, _, _ = economical_workflow
    claimed(economical_workflow)
    result = {'ok': True, 'task_id': tid, 'status': 'running', 'continue_work': True}
    assert build_kanban_stop_nudge(messages=[{'role': 'tool', 'name': 'kanban_request_changes',
                                             'content': json.dumps(result)}]) is not None


def test_recovery_cannot_reset_spent_maker_correction_budget(economical_workflow):
    conn, tid, _, _, _ = economical_workflow
    owner, artifact = final_review(economical_workflow)
    assert kb.request_changes(conn, tid, reason='First consolidated correction.',
        expected_run_id=owner.current_run_id, review_decision_capability=owner.review_decision_capability)[0]
    maker = claimed(economical_workflow)
    assert kb.block_task(conn, tid, kind='capability', reason='Owner can resolve remaining source issue.',
        expected_run_id=maker.current_run_id)
    owner = claimed(economical_workflow)
    result = handoff(economical_workflow)
    assert result.get('phase') == 'owner_complete', result
    assert kb.get_task(conn, tid).current_run_id == owner.current_run_id
    assert kb.get_task(conn, tid).assignee == 'scarlett'


def test_direct_owner_recovery_resumes_without_delegating(economical_workflow):
    conn, tid, _, _, _ = economical_workflow
    owner = claimed(economical_workflow)
    ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id,
        summary='Owner finishes', acceptance_criteria=[x['criterion'] for x in acceptance_checks()])
    assert kb.block_task(conn, tid, kind='transient', reason='Recoverable local interruption.',
        expected_run_id=owner.current_run_id)
    resumed = claimed(economical_workflow)
    result = handoff(economical_workflow)
    assert result.get('phase') == 'owner_complete', result
    assert kb.get_task(conn, tid).current_run_id == resumed.current_run_id


def test_owner_bound_file_survives_scratch_cleanup(economical_workflow):
    conn, tid, _, _, _ = economical_workflow
    workspace = kb.workspaces_root() / tid; workspace.mkdir(parents=True)
    conn.execute("UPDATE tasks SET workspace_kind='scratch',workspace_path=? WHERE id=?", (str(workspace), tid))
    conn.commit()
    owner = claimed(economical_workflow)
    ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id,
        summary='One small answer', acceptance_criteria=[x['criterion'] for x in acceptance_checks()])
    artifact = workspace / 'answer.md'; artifact.write_text('Address: 10 Example Street; Date: 2026-10-01\n')
    expected = artifact.read_bytes()
    assert kb.complete_task(conn, tid, summary='Verified both fields.', metadata=owner_metadata(artifact),
        expected_run_id=owner.current_run_id, fire_lifecycle_hook=False)
    saved = Path(kb.latest_run(conn, tid).metadata['artifacts'][0])
    assert saved != artifact and saved.read_bytes() == expected
    assert not workspace.exists()


def test_required_advisory_is_not_downgraded(economical_workflow):
    conn, tid, _, _, _ = economical_workflow
    set_advisors(conn, tid, ['fable-review'])
    owner = claimed(economical_workflow)
    with pytest.raises(oc.OwnerCellGuardError, match='specialist review'):
        ow.start_owner_completion(conn, tid, expected_run_id=owner.current_run_id,
            summary='Try direct mode', acceptance_criteria=['original outcome'])


def test_worker_block_returns_to_owner_then_resumes_after_verified_repair(workflow):
    conn, tid, workspace, brief, _ = workflow
    claimed(workflow); assert handoff(workflow).get('phase') == 'execute'
    worker = claimed(workflow)
    assert kb.block_task(conn, tid, kind='capability', reason='The source file moved; need corrected input path.', expected_run_id=worker.current_run_id)
    assert kb.get_task(conn, tid).status == 'ready'
    assert kb.get_task(conn, tid).assignee == 'scarlett'
    assert ow.describe(conn, tid)['phase'] == 'owner_recovery'
    claimed(workflow)
    missing = handoff(workflow)
    assert 'repair.cause' in missing['error']
    repaired = handoff(workflow, repair={'cause': 'The source path was stale.', 'change': 'Restored the scoped input.',
                        'verification': 'Read the restored input and checked required fields.', 'prevention': 'Check source existence before every maker handoff.'})
    assert repaired.get('phase') == 'execute', repaired
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert kb.get_task(conn, tid).assignee == 'maker'


def test_owner_cannot_mistake_brief_for_final_deliverable(workflow):
    conn, tid, _, brief, _ = workflow
    owner = claimed(workflow)
    with pytest.raises(oc.OwnerCellGuardError, match='kanban_handoff'):
        kb.request_review(conn, tid, summary='Brief ready', reviewer='scarlett', expected_run_id=owner.current_run_id,
                          metadata={'owner_cell_artifact': {'ref': str(brief), 'digest': digest(brief)}})
    with pytest.raises(oc.OwnerCellGuardError, match='reviewed final artifact'):
        kb.complete_task(conn, tid, summary='Brief ready', expected_run_id=owner.current_run_id, fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).status == 'running'


def test_stale_owner_handoff_and_changed_artifact_fail_without_releasing_claim(workflow):
    conn, tid, _, brief, _ = workflow
    owner = claimed(workflow)
    with pytest.raises(oc.OwnerCellGuardError):
        ow.handoff(conn, tid, expected_run_id=owner.current_run_id + 1, brief_ref=str(brief), brief_digest=digest(brief),
                   summary='stale', requirements={}, acceptance_criteria=['fields present'])
    out = json.loads(kt._handle_owner_handoff({'task_id': tid, 'brief_ref': str(brief), 'brief_digest': '0'*64,
               'summary': 'stale bytes', 'requirements': {}, 'acceptance_criteria': ['fields present']}))
    assert 'digest' in out['error']
    assert kb.get_task(conn, tid).current_run_id == owner.current_run_id


def test_handoff_is_durable_and_duplicate_delivery_cannot_spawn_another_maker(workflow):
    conn, tid, _, _, _ = workflow
    claimed(workflow); assert handoff(workflow).get('phase') == 'execute'
    reopened = kb.connect()
    assert ow.describe(reopened, tid)['phase'] == 'execute'
    assert kb.get_task(reopened, tid).assignee == 'maker'
    duplicate = handoff(workflow)
    assert 'currently claimed' in duplicate['error']
    assert kb.claim_task(reopened, tid) is not None
    assert kb.claim_task(conn, tid) is None
    reopened.close()


def test_repair_can_launch_when_the_input_it_must_repair_is_missing(workflow):
    conn, tid, workspace, brief, _ = workflow
    claimed(workflow)
    source = workspace / 'source.txt'; source.write_text('source')
    assert handoff(workflow, {'read_paths': [str(source)], 'toolsets': ['file', 'kanban']}).get('phase') == 'execute'
    worker = claimed(workflow)
    source.unlink()
    assert kb.block_task(conn, tid, kind='capability', reason='Required source disappeared', expected_run_id=worker.current_run_id)
    task = kb.get_task(conn, tid)
    admission = kb._build_worker_admission(task, 'scarlett')
    assert admission.contract['skill_dependencies']['readiness']['issues'] == []
    assert 'source.txt' in task.body  # Maker requirements were retained, not erased.


def test_only_explicitly_approved_toolsets_can_be_added_for_the_maker(workflow):
    conn, tid, workspace, brief, monkeypatch = workflow
    home = Path(os.environ['HERMES_HOME'])
    profile_config = home / 'profiles/maker/config.yaml'
    profile_config.write_text(profile_config.read_text().replace('[file, kanban]', '[kanban]'))
    claimed(workflow)
    denied = handoff(workflow)
    assert denied['code'] == 'maker_not_ready'
    assert {'code': 'missing_toolset', 'resource': 'file'} in denied['issues']
    original = (home / 'config.yaml').read_text()
    (home / 'config.yaml').write_text(original + '  owner_workflow:\n    enabled: true\n    approved_toolsets:\n      maker: [file]\n')
    accepted = handoff(workflow)
    assert accepted.get('phase') == 'execute', accepted
    assert '[kanban]' in profile_config.read_text()  # No persistent profile/permission mutation.
    evidence = ow.describe(conn, tid)['brief']['readiness']
    assert 'toolset:file' in evidence['checked']


def acceptance_checks():
    return [{'criterion': text, 'passed': True, 'evidence': 'Read the exact artifact and compared this field with the source.'}
            for text in ['Contains the source address', 'Contains the event date']]


def set_advisors(conn, tid, advisors):
    value = ow.state(conn, tid)[1]
    value['owner_workflow']['advisory_reviewers'] = advisors
    conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?', (json.dumps(value), tid))
    conn.commit()


def final_review(workflow):
    conn, tid, workspace, _, _ = workflow
    claimed(workflow); assert handoff(workflow).get('phase') == 'execute'
    claimed(workflow)
    artifact = workspace / 'result.txt'
    artifact.write_text('Address: 10 Example Street; Date: 2026-10-01\n')
    owner = review(workflow, artifact)
    return owner, artifact


def test_acceptance_rejects_missing_checks_and_changed_final_bytes(workflow):
    conn, tid, _, _, _ = workflow
    owner, artifact = final_review(workflow)
    version = kb.owner_cell_read_state(conn, tid)['current_state_version']
    meta = pass_metadata(ref=str(artifact), digest=digest(artifact), state_version=version)
    with pytest.raises(oc.OwnerCellGuardError, match='owner_acceptance_checks'):
        kb.complete_task(conn, tid, summary='Checked', metadata=meta, expected_run_id=owner.current_run_id,
                         review_decision_capability=owner.review_decision_capability, fire_lifecycle_hook=False)
    artifact.write_text('Changed after review')
    with pytest.raises(oc.OwnerCellGuardError, match='bytes changed'):
        kb.complete_task(conn, tid, summary='Checked', metadata={**meta, 'owner_acceptance_checks': acceptance_checks()},
                         expected_run_id=owner.current_run_id, review_decision_capability=owner.review_decision_capability, fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).status == 'running'


def test_advisory_requires_actual_scoped_call_and_returns_to_owner_review(workflow):
    conn, tid, workspace, _, monkeypatch = workflow
    (Path(os.environ['HERMES_HOME']) / 'profiles/claude-review').mkdir()
    set_advisors(conn, tid, ['claude-review'])
    owner, artifact = final_review(workflow)
    packet = 'Review this synthetic collection plan for missing evidence and arithmetic errors.'
    result = ow.request_advisory(conn, tid, expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability, profile='claude-review', packet=packet, summary='Independent advisory')
    aid = result['advisory_task']
    assert kb.get_task(conn, tid).status == 'todo'
    assert str(workspace) not in kb.get_task(conn, aid).body
    advisory = kb.claim_task(conn, aid)
    assert advisory is not None
    monkeypatch.setenv('HERMES_KANBAN_TASK', aid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(advisory.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE', 'claude-review')
    kb._set_worker_pid(conn, aid, os.getpid())
    with pytest.raises(oc.OwnerCellGuardError, match='required advisory tool call'):
        kb.complete_task(conn, aid, summary='Claimed without using tool', expected_run_id=advisory.current_run_id, fire_lifecycle_hook=False)
    with pytest.raises(PermissionError):
        kt._default_task_id(tid)
    assert kt._default_task_id(aid) == aid
    with pytest.raises(oc.OwnerCellGuardError, match='claimed packet'):
        ow.record_advisory_result(tool='claude_opus_review', packet=packet + ' changed', result='No concerns')
    ow.record_advisory_result(tool='claude_opus_review', packet=packet, result='No arithmetic concerns; verify both original fields.')
    assert kb.complete_task(conn, aid, summary='No arithmetic concerns; verify both original fields.', expected_run_id=advisory.current_run_id, fire_lifecycle_hook=False)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == 'review'
    owner = claimed(workflow, reviewer=True)
    version = kb.owner_cell_read_state(conn, tid)['current_state_version']
    meta = {**pass_metadata(ref=str(artifact), digest=digest(artifact), state_version=version), 'owner_acceptance_checks': acceptance_checks()}
    assert kb.complete_task(conn, tid, summary='Verified final fields and advisory findings', expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability, metadata=meta, fire_lifecycle_hook=False)


def test_failed_advisory_returns_to_owner_without_waiving_gate(workflow):
    conn, tid, _, _, _ = workflow
    (Path(os.environ['HERMES_HOME']) / 'profiles/claude-review').mkdir()
    set_advisors(conn, tid, ['claude-review'])
    owner, artifact = final_review(workflow)
    packet = 'Synthetic advisory packet with no private identities or paths.'
    result = ow.request_advisory(conn, tid, expected_run_id=owner.current_run_id,
        review_decision_capability=owner.review_decision_capability, profile='claude-review', packet=packet, summary='Review')
    advisory = kb.claim_task(conn, result['advisory_task'])
    assert kb.block_task(conn, advisory.id, kind='capability', reason='review tool unavailable', expected_run_id=advisory.current_run_id)
    ow.recover_advisory_failures(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    assert ow.describe(conn, tid)['phase'] == 'owner_recovery'
    with pytest.raises(oc.OwnerCellGuardError, match='reviewed final artifact'):
        ow.assert_advisories_complete(conn, tid, metadata={'owner_acceptance_checks': acceptance_checks()})
    assert ow.describe(conn, tid)['advisory_reviewers'] == ['claude-review']


def test_repeated_system_failure_creates_one_private_safe_engineering_case(workflow):
    conn, tid, _, _, _ = workflow
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n')
    reason = 'kanban_request_token_budget: private.person@example.com /Users/Private/secret.txt'
    # Inject the same failure repeatedly through real claimed-run transitions.
    for attempt in range(3):
        task = claimed(workflow)
        assert kb.block_task(conn, tid, kind='capability', reason=reason, expected_run_id=task.current_run_id)
    assert kb.get_task(conn, tid).status == 'blocked'
    receipt = ow.describe(conn, tid)['engineering_repair']
    assert receipt and receipt['failure_code'] == 'kanban_request_token_budget'
    with kb.connect_closing(board='repairs') as repairs:
        repair = kb.get_task(repairs, receipt['task_id'])
        assert 'private.person' not in repair.body and '/Users/Private' not in repair.body
        assert repair.assignee == 'scarlett'
        assert ow.describe(repairs, repair.id)['phase'] == 'owner_intake'
        ow.reconcile_engineering_repairs(conn)
        assert kb.get_task(conn, tid).status == 'blocked'
        for _ in range(3):
            ow.route_recovery(conn, tid, reason=reason, failure_kind='capability', expected_status='blocked')
        assert repairs.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_explicit_user_input_hold_is_never_auto_recovered(workflow):
    conn, tid, _, _, _ = workflow
    task = claimed(workflow)
    assert kb.block_task(conn, tid, kind='needs_input', reason='Needs a new user decision', expected_run_id=task.current_run_id)
    assert not ow.route_recovery(conn, tid, reason='worker_startup_failed', failure_kind='capability', expected_status='blocked')
    assert kb.get_task(conn, tid).status == 'blocked'


def test_configured_canonical_route_starts_with_head_and_retains_advisory(workflow):
    conn, tid, _, _, _ = workflow
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    departments:\n      engineering_systems: {owner: scarlett, maker: maker}\n')
    original = contract(runtime_profile='scarlett', required_reviewer_profile='claude-review')
    result = ow.augment_contract(original)
    assert result['owner_profile'] == 'scarlett'
    assert result['runtime_profile'] == 'maker'
    assert result['required_reviewer_profile'] == 'scarlett'
    assert result['owner_workflow']['advisory_reviewers'] == ['claude-review']
    shown = json.loads(kt._handle_show({'task_id':tid}))
    assert shown['owner_workflow']['phase'] == 'owner_intake'


def test_engineering_repair_uses_explicit_board_even_inside_pinned_worker(workflow):
    conn, tid, _, _, monkeypatch = workflow
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    for _ in range(3):
        task = claimed(workflow)
        assert kb.block_task(conn, tid, kind='capability', reason='worker_startup_failed', expected_run_id=task.current_run_id)
    receipt = ow.describe(conn, tid)['engineering_repair']
    assert receipt['board'] == 'repairs'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    with kb.connect_closing(kb.board_dir('repairs') / 'kanban.db') as repairs:
        assert kb.get_task(repairs, receipt['task_id']) is not None


def test_mandated_independent_artifact_review_is_not_replaced(workflow):
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    departments:\n      engineering_systems: {owner: scarlett, maker: maker}\n')
    original = contract(required_reviewer_profile='grace')
    assert ow.augment_contract(original) == original


def test_idle_legacy_adoption_preserves_source_scope_and_rejects_stale_inspection(workflow):
    conn, tid, workspace, _, _ = workflow
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    departments:\n      engineering_systems: {owner: scarlett, maker: maker}\n')
    legacy = contract(goal_id='legacy-adoption',objective='Another isolated objective',required_reviewer_profile='claude-review')
    lid = kb.create_task(conn,title='Legacy task',body='Preserve every original source fact and instruction.',assignee='scarlett',owner_cell_contract=legacy)
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(lid,)).fetchone()
    task = kb.get_task(conn,lid)
    bd = hashlib.sha256(task.body.encode()).hexdigest(); cd = hashlib.sha256(row['contract_json'].encode()).hexdigest()
    with pytest.raises(oc.OwnerCellGuardError,match='changed since'):
        ow.adopt_idle_task(conn,lid,expected_body_digest='0'*64,expected_contract_digest=cd)
    result = ow.adopt_idle_task(conn,lid,expected_body_digest=bd,expected_contract_digest=cd)
    assert result['owner']=='scarlett' and result['maker']=='maker'
    assert kb.get_task(conn,lid).body == task.body
    assert ow.state(conn,lid)[1]['callback'] == legacy['callback']
    assert ow.state(conn,lid)[1]['security_realm'] == legacy['security_realm']
    assert ow.describe(conn,lid)['advisory_reviewers']==['claude-review']
    assert ow.describe(conn,lid)['phase']=='owner_intake'


def test_reviewed_engineering_repair_wakes_original_owner_without_completing_job(workflow):
    conn, tid, workspace, _, monkeypatch = workflow
    home = Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  worker_readiness:\n    enabled: true\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n')
    for _ in range(3):
        task=claimed(workflow)
        assert kb.block_task(conn,tid,kind='capability',reason='worker_startup_failed',expected_run_id=task.current_run_id)
    receipt=ow.describe(conn,tid)['engineering_repair']; rid=receipt['task_id']
    repair_db=kb.board_dir('repairs')/'kanban.db'
    with kb.connect_closing(repair_db) as repairs:
        path=workspace/'repair';path.mkdir();brief=path/'brief.md';brief.write_text('Reproduce startup failure and verify repair.')
        kb.set_workspace_path(repairs,rid,path)
        monkeypatch.setenv('HERMES_KANBAN_DB',str(repair_db))
        monkeypatch.setenv('HERMES_KANBAN_BOARD','repairs')
        owner=kb.claim_task(repairs,rid)
        ow.handoff(repairs,rid,expected_run_id=owner.current_run_id,brief_ref=str(brief),brief_digest=digest(brief),
            requirements={'toolsets':['file','kanban']},acceptance_criteria=['Regression passes'],summary='Verified repair brief',board='repairs')
        maker=kb.claim_task(repairs,rid)
        artifact=path/'repair-result.md';artifact.write_text('Synthetic startup reproduction now passes; prevention regression recorded.')
        assert kb.request_review(repairs,rid,summary='Regression and repair evidence',reviewer='scarlett',expected_run_id=maker.current_run_id,
            metadata={'owner_cell_artifact':{'ref':str(artifact),'digest':digest(artifact)}})
        owner=kb.claim_review_task(repairs,rid);kb._set_worker_pid(repairs,rid,os.getpid())
        version=kb.owner_cell_read_state(repairs,rid)['current_state_version']
        assert kb.complete_task(repairs,rid,summary='Inspected regression and repair',expected_run_id=owner.current_run_id,
            review_decision_capability=owner.review_decision_capability,fire_lifecycle_hook=False,
            metadata={**pass_metadata(ref=str(artifact),digest=digest(artifact),state_version=version),
                      'owner_acceptance_checks':[{'criterion':'Regression passes','passed':True,'evidence':'Executed the synthetic reproduction.'}]})
    ow.reconcile_engineering_repairs(conn)
    assert kb.get_task(conn,tid).status=='ready'
    assert kb.get_task(conn,tid).assignee=='scarlett'
    assert ow.describe(conn,tid)['phase']=='owner_recovery'
    assert ow.describe(conn,tid)['engineering_return']['task_id']==rid


def test_adoption_retires_only_failed_readiness_dependency_without_deleting_shared_files(workflow):
    conn, _, workspace, brief, _ = workflow
    home=Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    departments:\n      engineering_systems: {owner: scarlett, maker: maker}\n')
    legacy=contract(goal_id='migration-root',objective='Legacy blocked review fixture',required_reviewer_profile='claude-review')
    root=kb.create_task(conn,title='Legacy root',body='Original facts',assignee='scarlett',owner_cell_contract=legacy,workspace_kind='dir',workspace_path=str(workspace))
    prep=kb.create_task(conn,title='Prepare access',body='Legacy readiness',assignee='scarlett',created_by='worker-readiness',workspace_kind='dir',workspace_path=str(workspace))
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?",(prep,))
    kb._append_event(conn,prep,'readiness_preparation_for',{'task_id':root})
    conn.execute("INSERT INTO task_links(parent_id,child_id,edge_role) VALUES (?,?,'dependency')",(prep,root));conn.commit()
    row=conn.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?',(root,)).fetchone()
    ow.adopt_idle_task(conn,root,expected_body_digest=hashlib.sha256(b'Original facts').hexdigest(),
        expected_contract_digest=hashlib.sha256(row[0].encode()).hexdigest(),superseded_preparations=[prep])
    assert kb.get_task(conn,root).status=='ready'
    assert kb.get_task(conn,prep).status=='blocked'
    assert kb._has_sticky_block(conn,prep)
    assert brief.is_file()
    assert not kb.parent_ids(conn,root)


def test_paraphrased_capability_failure_cannot_reset_recovery_budget(workflow):
    conn,tid,_,_,_=workflow
    home=Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n')
    for reason in ['Worker cannot read the private source','Source access is unavailable for this worker','Still no supported source access']:
        task=claimed(workflow)
        assert kb.block_task(conn,tid,kind='capability',reason=reason,expected_run_id=task.current_run_id)
    assert kb.get_task(conn,tid).status=='blocked'
    assert ow.describe(conn,tid)['engineering_repair']['failure_code']=='unclassified_worker_recovery'
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='owner_recovery_requested'",(tid,)).fetchone()[0]==2


def test_department_advisory_requirement_remains_enforced_with_manual_approval(workflow):
    conn,tid,_,_,_=workflow
    home=Path(os.environ['HERMES_HOME'])
    home.joinpath('config.yaml').write_text('kanban:\n  owner_workflow:\n    enabled: true\n    departments:\n      engineering_systems:\n        owner: scarlett\n        maker: maker\n        advisory_reviewers: [fable-review]\n')
    owner,artifact=final_review(workflow)
    assert ow.describe(conn,tid)['advisory_reviewers']==['fable-review']
    with pytest.raises(oc.OwnerCellGuardError,match='explicit Chris authorization'):
        ow.request_advisory(conn,tid,expected_run_id=owner.current_run_id,review_decision_capability=owner.review_decision_capability,
            profile='fable-review',packet='Synthetic redacted advisory packet for a review.',summary='Review')
    with pytest.raises(oc.OwnerCellGuardError,match='fable-review is missing'):
        ow.assert_advisories_complete(conn,tid,metadata={'owner_acceptance_checks':acceptance_checks()})


def test_failure_kind_changes_do_not_reset_owner_recovery(workflow):
    conn,tid,_,_,_=workflow
    for kind in ('transient','capability'):
        task=claimed(workflow)
        kb.block_task(conn,tid,kind=kind,reason='same unchanged infrastructure failure',expected_run_id=task.current_run_id)
        assert kb.get_task(conn,tid).status=='ready'
    task=claimed(workflow)
    kb.block_task(conn,tid,kind='transient',reason='paraphrased same failure',expected_run_id=task.current_run_id)
    assert kb.get_task(conn,tid).status=='blocked'


def test_same_board_existing_engineering_authority_is_bound_without_duplicate(workflow):
    conn,tid,workspace,_,_=workflow
    existing=kb.create_task(conn,title='Canonical infrastructure repair',body='Synthetic repair scope',assignee='maker',
        workspace_kind='dir',workspace_path=str(workspace),owner_cell_contract=contract(runtime_profile='maker', objective='Repair a separate synthetic infrastructure scope', goal_id='canonical-repair-fixture'))
    Path(os.environ['HERMES_HOME']).joinpath('config.yaml').write_text(
        'kanban:\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n      task_id: '+existing+'\n')
    # The real root board shares its database with the canonical repair.
    target=kb.board_dir('repairs')/'kanban.db'
    target.parent.mkdir(parents=True,exist_ok=True)
    import sqlite3
    copy=sqlite3.connect(target);conn.backup(copy);copy.close()
    with kb.connect_closing(target,board='repairs') as same:
        for _ in range(3):
            task=kb.claim_task(same,tid)
            kb.block_task(same,tid,kind='capability',reason='worker_startup_failed',expected_run_id=task.current_run_id)
        receipt=ow.describe(same,tid)['engineering_repair']
        assert receipt['task_id']==existing
        assert same.execute('select count(*) from tasks').fetchone()[0]==2
        assert same.execute("select count(*) from task_events where task_id=? and kind='owner_repair_incident'",(existing,)).fetchone()[0]==1


def test_archived_repair_is_rebound_to_configured_canonical_authority(workflow):
    conn,tid,workspace,_,_=workflow
    config=Path(os.environ['HERMES_HOME'])/'config.yaml'
    base='kanban:\n  owner_workflow:\n    enabled: true\n    engineering_recovery:\n      board: repairs\n      owner: scarlett\n      maker: maker\n'
    config.write_text(base)
    for _ in range(3):
        task=claimed(workflow)
        kb.block_task(conn,tid,kind='capability',reason='worker_startup_failed',expected_run_id=task.current_run_id)
    old=ow.describe(conn,tid)['engineering_repair']
    with kb.connect_closing(kb.board_dir('repairs')/'kanban.db') as repairs:
        kb.archive_task(repairs,old['task_id'])
        canonical=kb.create_task(repairs,title='Existing repair authority',body='Separate synthetic scope',assignee='maker',
            owner_cell_contract=contract(objective='Canonical infrastructure fixture',goal_id='canonical-fixture',runtime_profile='maker'))
    config.write_text(base+'      task_id: '+canonical+'\n')
    ow.reconcile_engineering_repairs(conn)
    receipt=ow.describe(conn,tid)['engineering_repair']
    assert receipt['task_id']==canonical
    assert receipt['blocked_run_id']==old['blocked_run_id']
    assert ow._repair_wait_is_current(conn,tid,receipt)
    assert kb.get_task(conn,tid).status=='blocked'
    ow.reconcile_engineering_repairs(conn)
    assert conn.execute("select count(*) from task_events where task_id=? and kind='owner_engineering_repair'",(tid,)).fetchone()[0]==2

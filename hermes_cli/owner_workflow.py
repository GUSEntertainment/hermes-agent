"""Department-owned, same-task intake, execution and recovery.

The existing owner-cell contract is the authority. This module adds phases and
bounded recovery to that contract; it never changes credentials or review rights.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from pathlib import Path

from hermes_cli import owner_cell as oc

VERSION = 1
OWNER_PHASES = {'owner_intake', 'owner_recovery'}


def owner_completion_allowed(contract):
    """Ordinary owner acceptance only; never replace a separate specialist review."""
    return (
        contract.get('owner_workflow', {}).get('version') == VERSION
        and not contract['owner_workflow'].get('engineering_case')
        and contract.get('owner_role_alias') not in {
            'engineering_systems', 'accountant_finance', 'legal_compliance',
        }
        and contract.get('required_reviewer_profile') == contract.get('owner_profile')
        and oc.owner_acceptance_allows(contract, contract['owner_profile'])
        and not required_advisors(contract)
    )


def correction_count(conn, task_id):
    # A rewritten brief/state does not replenish the delegation budget.
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='changes_requested'",
                        (task_id,)).fetchone()[0]


def start_owner_completion(conn, task_id, *, expected_run_id, summary,
                           acceptance_criteria=None, review_decision_capability=None):
    """Retain this live owner run for direct delivery; do not invent peer approval."""
    from hermes_cli import kanban_db as kb
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('explain the owner completion decision in summary')
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        current = state(conn, task_id)
        if not task or not current:
            raise oc.OwnerCellGuardError('department-owned task required')
        row, contract = current
        if (task.status != 'running' or not expected_run_id or task.current_run_id != expected_run_id
                or task.assignee != contract['owner_profile']
                or row['phase'] not in OWNER_PHASES | {'review'}):
            raise oc.OwnerCellGuardError('owner completion requires the currently claimed owner run')
        prior_completion = _latest(conn, task_id, 'owner_completion_started')
        resuming = (contract['owner_workflow'].get('owner_completion') is True
                    and prior_completion and prior_completion.get('owner') == contract['owner_profile']
                    and contract['runtime_profile'] == contract['owner_profile']
                    and contract['router_mode'] == 'single')
        if not resuming and not owner_completion_allowed(contract):
            raise oc.OwnerCellGuardError('this task retains its required independent or specialist review')
        if row['phase'] == 'review':
            kb._validate_review_decision_capability(conn, task_id, review_decision_capability,
                                                    expected_run_id=expected_run_id)
        criteria = acceptance_criteria
        if criteria is None:
            criteria = (prior_completion if resuming else
                        (_latest(conn, task_id, 'owner_handoff') or {})).get('acceptance_criteria')
        if not isinstance(criteria, list) or not criteria or any(
                not isinstance(x, str) or not x.strip() for x in criteria):
            raise ValueError('provide concise acceptance_criteria tied to the user-requested outcome')
        owner = contract['owner_profile']
        updated = dict(contract)
        updated.pop('owner_acceptance', None)
        updated.update(runtime_profile=owner, router_mode='single', required_reviewer_profile=None,
                       delegation_roles={'maker': owner, 'reviewer': None},
                       worker_budget={**contract['worker_budget'], 'reviewers': 0},
                       owner_workflow={**contract['owner_workflow'], 'owner_completion': True})
        # Preserve risk classification, targets, callback, approval gates and realm.
        oc.validate_goal_contract(updated)
        version = oc.update_state(conn, task_id, expected_version=row['current_state_version'],
                                  facts=[summary], created_by=owner)
        conn.execute("""UPDATE owner_cell_goals SET contract_json=?, phase='owner_complete',
            runtime_profile=?,router_mode='single',required_reviewer_profile=NULL,reviewer_profile=NULL,
            worker_budget_json=?,review_artifact_ref=NULL,review_artifact_digest=NULL,review_state_version=NULL
            WHERE task_id=?""", (json.dumps(updated, sort_keys=True), owner,
                                 json.dumps(updated['worker_budget']), task_id))
        conn.execute("""UPDATE tasks SET runtime_profile=?,router_mode='single',reviewer_profile=NULL,
            current_state_version=?,worker_budget_json=? WHERE id=?""",
            (owner, version, json.dumps(updated['worker_budget']), task_id))
        receipt = {'owner': owner, 'previous_maker': contract['runtime_profile'],
                   'previous_contract': contract, 'acceptance_criteria': criteria,
                   'reason': summary, 'state_version': version,
                   'corrections_used': correction_count(conn, task_id),
                   'acceptance_mode': 'accountable_owner_completion_not_independent_review'}
        kb._append_event(conn, task_id, 'owner_completion_started', receipt, run_id=expected_run_id)
        return {'task_id': task_id, 'run_id': expected_run_id, 'status': 'running',
                'phase': 'owner_complete', 'owner': owner, 'continue_work': True,
                'next_action': 'Finish in this same run. Preserve requested facts and scope. '
                    'Use kanban_complete with metadata.owner_final_artifact={ref,digest} for the exact '
                    'finished file and owner_acceptance_checks for each criterion. '
                    'Do not claim independent review or delegate another maker.'}


def settings():
    # Installation governance never inherits a named worker's model or tools.
    from hermes_cli.profiles import _get_default_hermes_home
    import yaml
    path = Path(_get_default_hermes_home()) / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = ((config or {}).get('kanban') or {}).get('owner_workflow')
    return raw if isinstance(raw, dict) and raw.get('enabled') is True else {}


def augment_contract(contract, metadata=None):
    """Apply only an operator-configured canonical department/owner/maker pair."""
    cfg = settings()
    rule = (cfg.get('departments') or {}).get(contract['owner_role_alias'])
    if not isinstance(rule, dict):
        return contract
    if rule.get('execution') == 'native_owner':
        # New work stays with its existing routed producer/reviewer contract.
        # Optional native children do not acquire board ownership or review rights.
        return contract
    owner, maker = rule.get('owner'), rule.get('maker')
    if (owner != contract['owner_profile'] or not maker or owner == maker
            or contract['runtime_profile'] not in {owner, maker}):
        return contract
    # A separately mandated artifact reviewer keeps its native authority.
    # This rollout converts canonical owner acceptance and the incompatible
    # text-only advisory lanes, not arbitrary independent QA contracts.
    if contract.get('required_reviewer_profile') not in {None, owner, 'claude-review', 'fable-review'}:
        return contract
    if not oc._installed_profile(owner) or not oc._installed_profile(maker):
        raise oc.OwnerCellGuardError('configured owner workflow requires installed owner and maker')
    metadata = metadata or {}
    advisors = list(dict.fromkeys([
        contract.get('required_reviewer_profile'),
        *[x for x in (metadata.get('reviewer_profiles') or []) if x not in {owner, maker}],
        *list(metadata.get('secondary_reviewer_profiles') or []),
    ]))
    # General peer QA is now accountable-owner acceptance. Retain the
    # separate constrained financial/advisory lanes, rather than assigning
    # those profiles local-artifact acceptance.
    advisors = [x for x in advisors if x in {'claude-review', 'fable-review'}]
    updated = dict(contract)
    updated.update(
        runtime_profile=maker, required_reviewer_profile=owner,
        owner_acceptance={'mode': 'accountable_owner'}, router_mode='reviewed',
        risk_class='material', senior_owner_profile=owner,
        delegation_roles={'maker': maker, 'reviewer': owner},
        owner_workflow={'version': VERSION, 'advisory_reviewers': advisors,
                        'max_identical_recoveries': 2},
    )
    updated['worker_budget'] = {**contract['worker_budget'], 'producers': 1, 'reviewers': 1}
    oc.validate_goal_contract(updated)
    return updated


def state(conn, task_id):
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    contract = json.loads(row['contract_json'])
    if (contract.get('owner_workflow') or {}).get('version') != VERSION:
        return None
    return dict(row), contract


def _advisory_state(conn, task_id):
    """Read an existing owner review without migrating its execution contract.

    Legacy maker/owner-review tasks can use advisory lanes, but may not acquire
    owner production/recovery authority through this compatibility reader.
    """
    current = state(conn, task_id)
    if current:
        return current
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        from hermes_cli.plain_advisory import state as plain_state
        return plain_state(conn, task_id)
    contract = json.loads(row['contract_json'])
    if (contract.get('owner_workflow') or contract.get('router_mode') != 'reviewed'
            or contract.get('required_reviewer_profile') != contract.get('owner_profile')
            or contract.get('runtime_profile') == contract.get('owner_profile')
            or (contract.get('owner_acceptance') or {}).get('mode') != 'accountable_owner'
            or row['phase'] not in {'review', 'completed'}):
        return None
    oc.validate_goal_contract(contract)
    if not available_advisors(contract) and not _latest(conn, task_id, 'owner_advisory_requested'):
        return None
    if (row['owner_profile'] != contract['owner_profile']
            or row['required_reviewer_profile'] != contract['required_reviewer_profile']
            or row['reviewer_profile'] != contract['owner_profile']
            or row['review_state_version'] != row['current_state_version']):
        raise oc.OwnerCellGuardError('legacy advisory requires a coherent existing owner review')
    return dict(row), contract


def initialize(conn, task_id):
    from hermes_cli import kanban_db as kb
    current = state(conn, task_id)
    if not current:
        return
    row, contract = current
    owner = contract['owner_profile']
    conn.execute("UPDATE owner_cell_goals SET phase='owner_intake',runtime_profile=? WHERE task_id=?", (owner, task_id))
    conn.execute('UPDATE tasks SET assignee=?,runtime_profile=? WHERE id=?', (owner, owner, task_id))
    kb._append_event(conn, task_id, 'owner_workflow_started', {'owner': owner, 'maker': contract['runtime_profile']})


def migrate_idle_task_to_native(conn, task_id, *, expected_contract_digest,
        expected_body_digest, expected_state_version, expected_review_count,
        expected_status, actor, reason):
    """Operator migration of an idle task, retaining holds, evidence and budgets.

    Only explicitly configured native departments qualify. Required independent
    reviewers stay assigned; engineering owner-acceptance adds Grace before the
    owner becomes the producer. Legal/finance/advisory contracts remain intact.
    """
    from hermes_cli import kanban_db as kb
    if not actor or not reason:
        raise ValueError('operator actor and migration reason required')
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        if not task or not row or task.status != expected_status or task.status not in {'todo','ready','blocked','triage','review'}:
            raise oc.OwnerCellGuardError('migration requires the expected idle task status')
        if any(getattr(task, key, None) is not None for key in ('current_run_id','worker_pid','claim_lock','claim_expires')):
            raise oc.OwnerCellGuardError('migration cannot interrupt a claimed worker')
        if conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone():
            raise oc.OwnerCellGuardError('migration cannot race an active run')
        if conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?', (task_id,oc._now())).fetchone():
            raise oc.OwnerCellGuardError('migration cannot race a target lease')
        encoded = row['contract_json']
        if (hashlib.sha256(encoded.encode()).hexdigest() != expected_contract_digest
                or hashlib.sha256((task.body or '').encode()).hexdigest() != expected_body_digest
                or row['current_state_version'] != expected_state_version
                or row['review_loop_count'] != expected_review_count):
            raise oc.OwnerCellGuardError('task changed before migration')
        contract = json.loads(encoded)
        rule = (settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
        owner, maker = rule.get('owner'), rule.get('maker')
        if (rule.get('execution') != 'native_owner' or owner != contract['owner_profile']
                or maker != contract['runtime_profile'] or owner == maker
                or contract['owner_role_alias'] in {'legal_compliance','accountant_finance'}
                or rule.get('advisory_reviewers')
                or (contract.get('owner_workflow') or {}).get('advisory_reviewers')):
            raise oc.OwnerCellGuardError('task requires a separately supported migration')
        reviewer = contract.get('required_reviewer_profile')
        if reviewer == owner:
            if not oc.owner_acceptance_allows(contract, owner):
                raise oc.OwnerCellGuardError('cannot replace a required independent review')
            reviewer = 'grace' if contract['owner_role_alias'] in {'engineering_systems','agent_infrastructure','engineering'} else None
        if not oc._installed_profile(owner) or (reviewer and not oc._installed_profile(reviewer)):
            raise oc.OwnerCellGuardError('native producer and reviewer must be installed')
        updated = dict(contract)
        updated.pop('owner_workflow', None)
        updated.pop('owner_acceptance', None)
        updated.update(runtime_profile=owner, required_reviewer_profile=reviewer,
            router_mode='reviewed' if reviewer else 'single',
            delegation_roles={'maker':owner,'reviewer':reviewer},
            worker_budget={**contract['worker_budget'],'reviewers':int(bool(reviewer))},
            native_owner_migration={'previous_maker':maker,'actor':actor})
        oc.validate_goal_contract(updated)
        in_review = row['phase'] == 'review' and bool(reviewer) and bool(row['review_artifact_digest'])
        phase = 'review' if in_review else 'execute'
        runtime = reviewer if in_review else owner
        # An unheld ordinary owner-review becomes a runnable owner turn.
        # Do not leave status=review with no reviewer; genuine holds stay put.
        status = 'ready' if task.status == 'review' and not in_review else task.status
        conn.execute('''UPDATE owner_cell_goals SET contract_json=?,phase=?,runtime_profile=?,
            router_mode=?,required_reviewer_profile=?,reviewer_profile=?,worker_budget_json=? WHERE task_id=?''',
            (json.dumps(updated,sort_keys=True),phase,runtime,updated['router_mode'],reviewer,
             reviewer if in_review else None,json.dumps(updated['worker_budget']),task_id))
        conn.execute('''UPDATE tasks SET assignee=?,runtime_profile=?,router_mode=?,reviewer_profile=?,
            worker_budget_json=?,status=? WHERE id=?''', (runtime,runtime,updated['router_mode'],
            reviewer if in_review else None,json.dumps(updated['worker_budget']),status,task_id))
        receipt={'previous_contract':contract,'previous_phase':row['phase'],'previous_assignee':task.assignee,
            'owner':owner,'previous_maker':maker,'reviewer':reviewer,'phase':phase,'actor':actor,'reason':reason,
            'state_version':expected_state_version,'review_count':expected_review_count,
            'previous_status':task.status,'status':status,'blockers_preserved':True,'body_digest':expected_body_digest}
        kb._append_event(conn,task_id,'native_owner_migrated',receipt)
        return {k:v for k,v in receipt.items() if k!='previous_contract'}


def adopt_idle_task(conn, task_id, *, expected_body_digest, expected_contract_digest,
                    superseded_preparations=()):
    """Operator migration of one inspected idle legacy card, with exact CAS.

    Not exposed as a worker tool. Keep source text, callback, realm, dependencies
    and all historical evidence; invalidate only the old brief review binding.
    """
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        if not task or not row:
            raise oc.OwnerCellGuardError('existing task and owner contract required')
        if (task.status not in {'todo', 'ready', 'review', 'blocked'} or task.current_run_id
                or task.claim_lock or task.worker_pid or task.block_kind == 'needs_input'
                or kb._has_sticky_block(conn, task_id)):
            raise oc.OwnerCellGuardError('only an idle task without an explicit hold may be adopted')
        if (hashlib.sha256((task.body or '').encode()).hexdigest() != expected_body_digest
                or hashlib.sha256(row['contract_json'].encode()).hexdigest() != expected_contract_digest):
            raise oc.OwnerCellGuardError('task changed since operator inspection')
        previous = json.loads(row['contract_json'])
        if previous.get('owner_workflow'):
            raise oc.OwnerCellGuardError('task already has an owner workflow')
        latest_verdict = oc._latest_verdict(conn, task_id)
        if latest_verdict is not None and latest_verdict['verdict'] == 'PASS':
            raise oc.OwnerCellGuardError('an accepted artifact requires separate inspection; adoption refused')
        updated = augment_contract(previous, oc._routing_metadata(task.body))
        if not updated.get('owner_workflow'):
            raise oc.OwnerCellGuardError('task does not match a configured canonical owner workflow')
        # Only explicitly inspected, failed legacy readiness preparations for
        # THIS task may be retired. Preserve their evidence and shared files;
        # archive_task would clean the shared original workspace.
        for prep_id in superseded_preparations:
            prep = kb.get_task(conn, prep_id)
            binding = _latest(conn, prep_id, 'readiness_preparation_for') or {}
            if (not prep or prep.created_by != 'worker-readiness' or prep.status != 'blocked'
                    or prep.current_run_id or prep.claim_lock or prep.worker_pid
                    or prep.block_kind == 'needs_input' or kb._has_sticky_block(conn, prep_id)
                    or binding.get('task_id') != task_id):
                raise oc.OwnerCellGuardError('preparation is not an idle failed readiness dependency of this task')
            edge = conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=? AND edge_role='dependency'", (prep_id, task_id)).fetchone()
            if not edge:
                raise oc.OwnerCellGuardError('readiness dependency changed since inspection')
        for prep_id in superseded_preparations:
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=? AND edge_role='dependency'", (prep_id, task_id))
            conn.execute("UPDATE tasks SET block_kind='dependency' WHERE id=?", (prep_id,))
            kb._append_event(conn, prep_id, 'blocked', {'kind': 'dependency', 'reason': 'Superseded by accountable-owner intake on the original task; retain evidence and shared workspace.', 'original_task': task_id})
        owner = updated['owner_profile']
        version = oc.update_state(conn, task_id, expected_version=row['current_state_version'],
            facts=['Operator restored canonical owner intake. Existing execution brief is planning evidence only; produce and review the requested final deliverable.'],
            created_by='operator-owner-workflow')
        conn.execute("""UPDATE owner_cell_goals SET contract_json=?,phase='owner_intake',runtime_profile=?,
            required_reviewer_profile=?,reviewer_profile=NULL,senior_owner_profile=?,router_mode='reviewed',risk_class='material',
            worker_budget_json=?,review_artifact_ref=NULL,review_artifact_digest=NULL,review_state_version=NULL WHERE task_id=?""",
            (json.dumps(updated, sort_keys=True), owner, owner, owner, json.dumps(updated['worker_budget']), task_id))
        landing = kb._landing_status_after_parents(conn, task_id)
        conn.execute("""UPDATE tasks SET status=?,assignee=?,runtime_profile=?,reviewer_profile=NULL,
            review_decision_digest=NULL,router_mode='reviewed',risk_class='material',worker_budget_json=?,
            current_state_version=?,block_kind=NULL WHERE id=?""",
            (landing, owner, owner, json.dumps(updated['worker_budget']), version, task_id))
        kb._append_event(conn, task_id, 'owner_workflow_adopted', {
            'previous_contract': previous, 'previous_phase': row['phase'],
            'previous_review_artifact_ref': row['review_artifact_ref'],
            'previous_review_artifact_digest': row['review_artifact_digest'],
            'body_digest': expected_body_digest, 'owner': owner, 'maker': updated['runtime_profile'],
            'superseded_preparations': list(superseded_preparations),
            'reason': 'Restore owner intake and separate text-only advisory from final artifact acceptance.'})
        return {'task_id': task_id, 'owner': owner, 'maker': updated['runtime_profile'], 'status': landing}


def _review_admission_requirements(conn, task, profile):
    """Review prerequisites come from the current bound artifact, not maker work.

    This is readiness only: it never mints review authority, changes the saved
    maker requirements, or adds tools to the reviewer's configured capability.
    """
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task.id,)).fetchone()
    if row is None or row['phase'] != 'review':
        return None
    actual = conn.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone()
    # Recovery probes readiness before changing the persisted held status.
    # Accept only the exact current Task projected to review; this computes
    # prerequisites, never releases the hold or grants a reviewer claim.
    from hermes_cli import kanban_db as kb
    import time
    held_projection = bool(actual is not None and actual['status'] in {'triage', 'blocked'}
        and task.status == 'review'
        and task == dataclasses.replace(kb.Task.from_row(actual), status='review')
        and all(actual[key] is None for key in ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))
        and not conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task.id,)).fetchone()
        and not conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?', (task.id, int(time.time()))).fetchone())
    contract = json.loads(row['contract_json'])
    oc.validate_goal_contract(contract)
    required = contract.get('required_reviewer_profile')
    if (row['router_mode'] != 'reviewed' or not required or profile != required
            or any(row[key] != profile for key in ('required_reviewer_profile', 'reviewer_profile', 'runtime_profile'))
            or actual is None or (actual['status'] not in {'review', 'running'} and not held_projection)
            or any(actual[key] != profile for key in ('assignee', 'runtime_profile', 'reviewer_profile'))
            or task.assignee != profile
            or profile == contract['runtime_profile']
            or (profile == contract['owner_profile'] and not oc.owner_acceptance_allows(contract, profile))
            or row['review_state_version'] != row['current_state_version']
            or actual['current_state_version'] != row['current_state_version']):
        raise oc.OwnerCellGuardError('Review readiness requires the current assigned reviewer')
    ref = row['review_artifact_ref']
    digest = row['review_artifact_digest']
    if not isinstance(ref, str) or not Path(ref).is_absolute() or not digest:
        raise oc.OwnerCellGuardError('Review readiness requires a bound artifact')
    # A stale digest is an evidence-review problem, not missing capability.
    # The assigned reviewer must be able to inspect this already-scoped regular
    # file and use the existing process-bound evidence rebind. Completion still
    # checks the exact bound digest; admission never accepts the changed bytes.
    actual_digest = oc._regular_file_sha256(ref)
    if held_projection and actual_digest != oc.normalize_digest(digest):
        raise oc.OwnerCellGuardError('Held review readiness requires its unchanged bound artifact')
    from hermes_cli.worker_readiness import requirements
    return requirements('', '```worker-requirements\n' + json.dumps({
        'toolsets': ['file', 'kanban'], 'read_paths': [ref],
    }) + '\n```')


def admission_requirements(task, profile, needed, *, board=None):
    """A repair's missing inputs are repair targets, not launch prerequisites."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.worker_readiness import requirements
    conn = kb.connect(board=board)
    try:
        review_needed = _review_admission_requirements(conn, task, profile)
        if review_needed is not None:
            return review_needed, []
        current = state(conn, task.id)
    finally:
        conn.close()
    if current is None:
        return needed, []
    row, contract = current
    if row['phase'] in OWNER_PHASES and profile == contract['owner_profile']:
        needed = requirements('', '```worker-requirements\n{"toolsets":["file","kanban"]}\n```')
    # Only installation-authorized toolsets may be added, and only for the
    # existing contract owner/maker. Advisory profiles never inherit these.
    if profile not in {contract['owner_profile'], contract['runtime_profile']}:
        return needed, []
    approved = (settings().get('approved_toolsets') or {}).get(profile, [])
    if not isinstance(approved, list) or any(not isinstance(x, str) for x in approved):
        raise ValueError('approved_toolsets must map profiles to explicit toolset lists')
    return needed, sorted(set((needed or {}).get('toolsets', [])) & set(approved))


def _latest(conn, task_id, kind):
    row = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1', (task_id, kind)).fetchone()
    return json.loads(row[0]) if row else None


def required_advisors(contract):
    rule = (settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
    policy = rule.get('advisory_reviewers', []) if isinstance(rule, dict) else []
    return list(dict.fromkeys([*contract.get('advisory_reviewers', []), *(contract.get('owner_workflow') or {}).get('advisory_reviewers', []), *policy]))


def available_advisors(contract):
    rule = (settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
    optional = rule.get('optional_advisory_reviewers', []) if isinstance(rule, dict) else []
    return list(dict.fromkeys([*required_advisors(contract), *optional]))


def review_corrections(conn, task_id):
    """Keep the latest exact review available outside capped run/comment history."""
    verdict = oc._latest_verdict(conn, task_id)
    if verdict is None or verdict['verdict'] != 'REQUEST_CHANGES':
        return None
    return {key: verdict[key] for key in (
        'review_round', 'reviewer_profile', 'state_version', 'artifact_ref', 'artifact_digest',
    )} | {'defects': json.loads(verdict['defects_json'])}


def assert_rework_progress(conn, task_id, *, artifact_ref, artifact_digest):
    """Reject a no-op before spending a reviewer run; keep the maker's claim."""
    current = state(conn, task_id)
    if current is None:
        return
    row, _ = current
    corrections = review_corrections(conn, task_id)
    # A newer, explicit owner handoff can change the assignment. Old rejected
    # bytes are not an eternal ban under a different acceptance state.
    if not corrections or corrections['state_version'] != row['current_state_version']:
        return
    actual_digest = oc._regular_file_sha256(artifact_ref)
    if actual_digest != oc.normalize_digest(artifact_digest):
        raise oc.OwnerCellGuardError(
            'Rework artifact digest does not match current file bytes. Recompute the digest '
            'and correct the review binding in this same run; the task remains assigned to you.'
        )
    if actual_digest == corrections['artifact_digest']:
        raise oc.OwnerCellGuardError(
            'Rework artifact is unchanged from the latest rejected version. Continue in this same run: '
            'apply the current review corrections to the controlling source/generator, regenerate '
            'the affected outputs, and verify each correction before requesting review. Bind the '
            'changed deliverable or evidence package, not an unchanged companion file. '
            'This is correctable input, not a missing tool or dependency; do not block solely '
            'on this validation message. The task remains assigned to you.'
        )


def describe(conn, task_id):
    current = state(conn, task_id)
    if not current:
        receipt = _latest(conn, task_id, 'owner_advisory_external_receipt')
        if receipt:
            return {'external_advisory_receipt': {
                **{k: receipt[k] for k in ('receipt_digest', 'packet_digest', 'artifact_digest', 'result_digest', 'model', 'source')},
                'valid': _has_valid_external_advisory(conn, task_id),
                'instruction': 'Reuse the existing reviewed result; do not repeat the premium call. This receipt does not complete the card or authorize external actions.'}}
        legacy = _advisory_state(conn, task_id)
        if legacy and available_advisors(legacy[1]):
            row, contract = legacy
            if row.get('plain_advisory_only'):
                return {'phase': row['phase'], 'plain_advisory_only': True,
                        'owner': contract['owner_profile'], 'maker': contract['runtime_profile'],
                        'advisory_reviewers': required_advisors(contract),
                        'available_advisory_reviewers': available_advisors(contract),
                        'acceptance_criteria': row['acceptance_criteria'],
                        'artifact_ref': row['review_artifact_ref'], 'artifact_digest': row['review_artifact_digest']}
            return {'phase': row['phase'], 'legacy_advisory_only': True,
                    'owner': contract['owner_profile'], 'maker': contract['runtime_profile'],
                    'reviewer': contract['required_reviewer_profile'],
                    'advisory_reviewers': required_advisors(contract),
                    'available_advisory_reviewers': available_advisors(contract),
                    'fable_waiver': advisory_waiver_status(conn, task_id),
                    'recovery': _latest(conn, task_id, 'owner_recovery_requested')}
        return None
    row, contract = current
    completion = _latest(conn, task_id, 'owner_completion_started')
    if completion:
        completion = {k: v for k, v in completion.items() if k != 'previous_contract'}
    return {'phase': row['phase'], 'owner': contract['owner_profile'],
            'maker': contract['runtime_profile'], 'reviewer': contract['required_reviewer_profile'],
            'brief': _latest(conn, task_id, 'owner_handoff'),
            'review_corrections': review_corrections(conn, task_id),
            'owner_completion': completion,
            'delegation_budget': {'maker_corrections_used': correction_count(conn, task_id),
                                  'maker_corrections_limit': 1 if owner_completion_allowed(contract) else None},
            'recovery': _latest(conn, task_id, 'owner_recovery_requested'),
            'advisory_reviewers': required_advisors(contract),
            'available_advisory_reviewers': available_advisors(contract),
            'fable_waiver': advisory_waiver_status(conn, task_id),
            'engineering_repair': _latest(conn, task_id, 'owner_engineering_repair'),
            'engineering_return': _latest(conn, task_id, 'owner_engineering_repair_returned')}


def direct_producer_route(conn, task_id):
    """Describe an existing independent-review route; never migrate its authority."""
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    contract = json.loads(row['contract_json'])
    if (contract.get('owner_workflow') or row['phase'] not in {'execute', 'rework', 'senior_consolidation'}
            or contract.get('router_mode') != 'reviewed'
            or not contract.get('required_reviewer_profile')
            or row['runtime_profile'] != contract['runtime_profile']
            or contract['runtime_profile'] == contract['required_reviewer_profile']):
        return None
    return {'producer': contract['runtime_profile'],
            'reviewer': contract['required_reviewer_profile'], 'phase': row['phase'],
            'next_action': 'The task contract already assigns you as producer. Continue implementation '
                'and verification in this same run using existing authorized tools and workspace. '
                'Generic department delegation guidance does not add a maker to this contract. '
                'An unavailable extra handoff alone is not a capability blocker. Do not create a '
                'preparation or replacement task. When the requested work is ready, use '
                'kanban_request_review with the required reviewer and metadata.owner_cell_artifact '
                '{ref,digest} bound to the exact output. Preserve all approval, privacy, dependency '
                'and independent-review gates; this grants no tools, permissions or self-approval.'}


def continue_direct_producer(conn, task_id, *, expected_run_id, profile):
    """A wrong handoff leaves an already admitted producer working, not blocked."""
    from hermes_cli import kanban_db as kb
    route = direct_producer_route(conn, task_id)
    if route is None:
        return None
    task = kb.get_task(conn, task_id)
    if (not task or task.status != 'running' or not expected_run_id
            or task.current_run_id != expected_run_id
            or task.assignee != route['producer'] or profile != route['producer']):
        raise oc.OwnerCellGuardError('direct execution requires the currently claimed producer run')
    return {'task_id': task_id, 'run_id': expected_run_id, 'status': 'running',
            'continue_work': True, 'handoff_performed': False,
            'code': 'already_assigned_producer', **route}


def context(conn, task_id):
    view = describe(conn, task_id)
    if view is None:
        route = direct_producer_route(conn, task_id)
        if route:
            goal = conn.execute('SELECT owner_role_alias FROM owner_cell_goals WHERE task_id=?',
                                (task_id,)).fetchone()
            if goal['owner_role_alias'] == 'engineering_systems':
                route['engine_source_root'] = str(Path(__file__).resolve().parent.parent)
            return '## Contracted production and independent review\n' + json.dumps(route) + '\n'
        return ''
    if 'external_advisory_receipt' in view:
        return '## Existing actual advisory review\n' + json.dumps(view, ensure_ascii=False) + '\n'
    if view.get('plain_advisory_only'):
        return ('## Existing plain owner task with mandatory advisory gates\n' + json.dumps(view) + '\n'
            'Continue original production in this same task. Do not invent a maker or owner-cell PASS. '
            'For each required advisor, use kanban_handoff action=advisory with profile, compact redacted '
            'packet and brief_ref/brief_digest binding the exact finished candidate or deliverable manifest '
            'inside this workspace (not a planning brief). Fable requires existing explicit authorization. '
            'After each advisory, this same owner resumes ordinary execution. Inspect and disposition all '
            'findings, verify exact files and complete with metadata.owner_final_artifact={ref,digest} '
            'and owner_acceptance_checks for every criterion. This is owner acceptance, not independent '
            'owner review. Both actual advisors remain required; no waiver or external-action authority.\n')
    if view.get('legacy_advisory_only'):
        return ('## Existing owner review and scoped advisory\n' + json.dumps(view, ensure_ascii=False) + '\n'
            'Preserve this task\'s original producer and independent owner review. After inspecting '
            'the exact artifact, request each required advisory with kanban_handoff action=advisory, '
            'profile and a compact redacted packet; Fable still needs explicit user authorization. '
            'An advisory failure returns to this same review with fresh review authority. '
            'Resolve its concrete failure and retry within the existing attempt limit. '
            'Do not migrate the contract, create a replacement root, reset correction history, '
            'or use owner/maker handoff actions. Assess the advisory findings, verify the original '
            'deliverable and complete using the existing exact-artifact reviewer verdict. '
            'Advisories grant no production, access, sending or external-action authority.\n')
    if view['phase'] == 'owner_complete':
        return ('## Accountable owner completion\n' + json.dumps(view, ensure_ascii=False) + '\n'
            'Finish the requested outcome yourself in this run. Delegation is over for this task. '
            'Keep accepted worker research/facts; correct remaining defects directly. '
            'Additional presentation polish is not a completion gate unless requested or needed for usability. '
            'Use kanban_complete with metadata.owner_final_artifact={ref: exact finished local file, '
            'digest: actual SHA256} and metadata.owner_acceptance_checks for every criterion '
            'in owner_completion.acceptance_criteria. Record concrete evidence. '
            'This is accountable owner delivery, not independent peer review. Existing approval, '
            'privacy, source and external-action requirements remain in force.\n')
    return ('## Department-owned workflow\n' + json.dumps(view, ensure_ascii=False) + '\n'
        'The owner retains this outcome until the requested deliverable passes review. '
        'Delegation is optional and must save effort: for a small job or a repair cheaper to do '
        'yourself, use kanban_handoff action=owner with summary and concise acceptance_criteria '
        'to finish in this same run when ordinary owner completion is allowed. No separate brief '
        'is needed for that choice. Delegate only a bounded, worthwhile chunk with usable inputs. '
        'Acceptance must trace to the user request: do not invent PDF/DOCX, presentation, evidence '
        'packaging or cosmetic requirements that withhold an otherwise useful requested answer. '
        'Keep necessary factual/source/safety checks. Review all defects together once; ordinary '
        'tasks permit one consolidated maker correction, then the owner takes over. '
        'In owner_intake or owner_recovery, prepare/repair the brief in this workspace, '
        'check the worker requirements, and use kanban_handoff to resume the same task. '
        'For a repeated internal failure, compare the actual error with the controlling request '
        'and current brief. Correct obsolete/conflicting internal instructions, tool choices or '
        'implementation sources yourself within existing authority before handing it back. '
        'Do not just paraphrase the retry or ask again for routine work already authorized. '
        'Record the concrete change and verification in the repair handoff. Keep original '
        'user decisions, explicit stops, spending limits, privacy and required reviews intact; '
        'a worker error cannot override them. Use the existing recovery and usage limits. '
        'When prescribing a generated format, verify the actual downstream consumer/renderer '
        'supports that representation; schema validation alone is not compatibility evidence. '
        'If only the owner can run that consumer, the owner checks a minimal representative '
        'sample and stages the supported-format constraints for the maker before prescribing it. '
        'Do not complete or request final review of an outline or execution brief. '
        'In execute/rework, produce the actual artifact and request review by the named owner. '
        'For corrections, review_corrections above is the current defect list, not optional history. '
        'Apply it together with the latest owner handoff; it supersedes conflicting directions in '
        'older briefs unless a newer owner handoff explicitly resolves them. Fix the controlling '
        'source/template/generator as well as the generated deliverables so regeneration cannot '
        'restore the defect. Preserve already accepted facts and fixes. Before requesting review, '
        'check every requested correction against the regenerated output and include a concise '
        'defect-to-change-to-verification checklist in the handoff. Bind the changed deliverable '
        'or evidence package rather than an unchanged companion. Do not repeat unrelated research. '
        'The owner must inspect the exact artifact against every acceptance criterion, '
        'request concrete changes on this same task if it fails, and complete only after verification. Include metadata.owner_cell_verdict={state_version: CURRENT owner_cell.current_state_version, artifact_ref: CURRENT review_artifact_ref, artifact_digest: CURRENT review_artifact_digest, verdict: "PASS", defects: []}, plus metadata.owner_acceptance_checks with each exact criterion, passed=true, and concrete evidence. '
        'Before final completion, request each required advisory using kanban_handoff action=advisory, profile, packet, summary and confirm_manual only for already-authorized Fable. The packet must omit identifiers, secrets and local paths. Advisory reviewers never replace owner acceptance. '
        'A capability/transient blocker returns to the owner. Resolve routine existing-tool/input '
        'problems in scope. If the maker lacks network, account access, or outside-workspace paths '
        'but the owner already has authorized access, the OWNER performs that authorized retrieval '
        'during this intake/recovery run and stages non-secret, same-scope source files inside the '
        'assigned workspace. Brief the maker to process those staged files offline. The owner '
        'handles authorized final placement after inspecting the maker artifact. Do not reassign '
        'an unchanged retrieval/auth job to a confined maker, wait for the maker to fix its own '
        'sandbox, broaden permissions, copy credentials, or remove approval gates. '
        'For an infrastructure defect, record a reproducible diagnostic and a tested repair or '
        'a concrete engineering handoff; keep the original outcome open. Never repeat an unchanged repair.\n')


def _verify_brief(task, ref, digest):
    if not isinstance(ref, str) or not isinstance(digest, str):
        raise ValueError('brief_ref and brief_digest must be strings')
    path = Path(ref).resolve()
    workspace = Path(task.workspace_path or '').resolve()
    if not task.workspace_path or not path.is_relative_to(workspace):
        raise oc.OwnerCellGuardError('brief must be in the assigned task workspace')
    actual = oc._regular_file_sha256(ref)
    if actual != digest or not path.stat().st_size:
        raise oc.OwnerCellGuardError('brief bytes do not match the supplied digest or are empty')
    return str(path)


def handoff(conn, task_id, *, expected_run_id, brief_ref, brief_digest,
            requirements, acceptance_criteria, summary, repair=None, board=None):
    """Close the owner's claimed run only after a real maker readiness probe."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.worker_readiness import requirements as parse_requirements, _FENCE
    current = state(conn, task_id)
    task = kb.get_task(conn, task_id)
    if current is None or task is None:
        raise oc.OwnerCellGuardError('task does not use the department-owned workflow')
    row, contract = current
    if (row['phase'] not in OWNER_PHASES or task.assignee != contract['owner_profile']
            or task.status != 'running' or not expected_run_id or task.current_run_id != expected_run_id):
        raise oc.OwnerCellGuardError('handoff requires the currently claimed accountable-owner intake/recovery run')
    if (contract['owner_workflow'].get('owner_completion') is True
            or (owner_completion_allowed(contract) and correction_count(conn, task_id) >= 1)):
        return start_owner_completion(conn, task_id, expected_run_id=expected_run_id,
            summary='Delegation correction budget spent; owner completes the requested outcome. ' + summary,
            acceptance_criteria=acceptance_criteria)
    if not isinstance(acceptance_criteria, list) or not acceptance_criteria or any(
        not isinstance(x, str) or not x.strip() for x in acceptance_criteria
    ):
        raise ValueError('nonempty acceptance_criteria are required')
    if not isinstance(requirements, dict):
        raise ValueError('requirements must be an object')
    ref = _verify_brief(task, brief_ref, brief_digest)
    if row['phase'] == 'owner_recovery' and (not isinstance(repair, dict) or any(
        not isinstance(repair.get(k), str) or not repair[k].strip()
        for k in ('cause', 'change', 'verification', 'prevention')
    )):
        raise ValueError('recovery requires repair.cause, change, verification and prevention')
    fence = '```worker-requirements\n' + json.dumps(requirements, sort_keys=True) + '\n```'
    parse_requirements('', fence)
    # Keep the user's complete request and provenance; replace only the typed
    # requirements fence and append a reference to this current brief.
    body = _FENCE.sub('', task.body or '').rstrip() + '\n\n' + fence
    candidate = dataclasses.replace(task, body=body, assignee=contract['runtime_profile'])
    admission = kb._build_worker_admission(candidate, contract['runtime_profile'], board=board)
    with kb.write_txn(conn):
        fresh = kb.get_task(conn, task_id)
        latest = state(conn, task_id)
        if (fresh.current_run_id != expected_run_id or fresh.claim_lock != task.claim_lock
                or fresh.body != task.body or latest[0]['current_state_version'] != row['current_state_version']
                or latest[0]['contract_json'] != row['contract_json'] or latest[0]['phase'] != row['phase']):
            raise oc.OwnerCellGuardError('task changed during readiness verification')
        _verify_brief(fresh, brief_ref, brief_digest)
        version = oc.update_state(conn, task_id, expected_version=row['current_state_version'],
                                  facts=[summary], artifact_refs=[ref])
        maker = contract['runtime_profile']
        landing = kb._landing_status_after_parents(conn, task_id)
        conn.execute('UPDATE tasks SET body=?,assignee=?,runtime_profile=?,status=?,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,block_kind=NULL,current_state_version=? WHERE id=?',
                     (body, maker, maker, landing, version, task_id))
        conn.execute("UPDATE owner_cell_goals SET phase='execute',runtime_profile=?,review_artifact_ref=NULL,review_artifact_digest=NULL,review_state_version=NULL WHERE task_id=?", (maker, task_id))
        conn.execute('DELETE FROM target_leases WHERE task_id=?', (task_id,))
        run_id = kb._end_run(conn, task_id, outcome='owner_handoff', status=landing, summary=summary, metadata={'owner_repair': repair, 'failure_cause': (repair or {}).get('cause'), 'attempted_method': (repair or {}).get('change'), 'verification': (repair or {}).get('verification')})
        payload = {'brief_ref': ref, 'brief_digest': brief_digest, 'acceptance_criteria': acceptance_criteria,
                   'requirements': requirements, 'repair': repair, 'maker': maker,
                   'readiness': admission.contract['skill_dependencies'].get('readiness'),
                   'state_version': version}
        kb._append_event(conn, task_id, 'owner_handoff', payload, run_id=run_id)
        return {'task_id': task_id, 'status': landing, 'phase': 'execute', 'maker': maker, 'run_id': run_id}


def route_recovery(conn, task_id, *, reason, failure_kind, expected_status, expected_run_id=None):
    """Return a failed execution to its owner atomically, without another card."""
    from hermes_cli import kanban_db as kb
    current = state(conn, task_id)
    if not current:
        return False
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        row, contract = state(conn, task_id)
        if task.status != expected_status or (expected_run_id is not None and task.current_run_id != expected_run_id):
            return False
        if task.status == 'running' and (not expected_run_id or task.current_run_id != expected_run_id):
            return False
        if task.status != 'running' and (task.claim_lock or task.current_run_id or task.worker_pid):
            return False
        if task.block_kind == 'needs_input' or kb._has_sticky_block(conn, task_id):
            return False
        brief = _latest(conn, task_id, 'owner_handoff') or {}
        # Paraphrasing the same blocker must not reset its recovery budget.
        fingerprint = hashlib.sha256(json.dumps({'brief_digest': brief.get('brief_digest'),
                     'requirements': brief.get('requirements'), 'state_version': row['current_state_version']}, sort_keys=True).encode()).hexdigest()
        count = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='owner_recovery_requested' AND json_extract(payload,'$.fingerprint')=?", (task_id, fingerprint)).fetchone()[0]
        if count >= contract['owner_workflow'].get('max_identical_recoveries', 2):
            receipt = _queue_engineering_repair(conn, task_id, fingerprint=fingerprint, reason=reason, contract=contract)
            previous = _latest(conn, task_id, 'owner_recovery_escalated') or {}
            if previous.get('fingerprint') != fingerprint:
                kb._append_event(conn, task_id, 'owner_recovery_escalated', {'owner': contract['owner_profile'], 'fingerprint': fingerprint,
                    'engineering_repair': receipt,
                    'required': 'Repeated unchanged recovery requires a concrete engineering repair or a named new decision; the outcome remains open.'})
            return False
        owner = contract['owner_profile']
        landing = kb._landing_status_after_parents(conn, task_id)
        run_id = kb._end_run(conn, task_id, outcome='owner_recovery', status=landing, summary=reason)
        conn.execute('UPDATE tasks SET status=?,assignee=?,runtime_profile=?,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,block_kind=NULL WHERE id=?', (landing, owner, owner, task_id))
        conn.execute("UPDATE owner_cell_goals SET phase='owner_recovery',runtime_profile=? WHERE task_id=?", (owner, task_id))
        conn.execute('DELETE FROM target_leases WHERE task_id=?', (task_id,))
        kb._append_event(conn, task_id, 'owner_recovery_requested', {'owner': owner, 'failure_kind': failure_kind,
            'reason': reason, 'fingerprint': fingerprint, 'attempt': count + 1}, run_id=run_id)
        return True


def recover_failed_runs(conn, *, limit=4):
    """Dispatcher recovery after a crash/budget exit; explicit holds stay held."""
    rows = conn.execute("""SELECT t.id,t.status,t.last_failure_error FROM tasks t
        JOIN owner_cell_goals g ON g.task_id=t.id
        WHERE t.status='blocked' AND t.claim_lock IS NULL AND t.current_run_id IS NULL
          AND json_extract(g.contract_json,'$.owner_workflow.version')=1
          AND (SELECT kind FROM task_events WHERE task_id=t.id
               AND kind IN ('gave_up','blocked','unblocked','status') ORDER BY id DESC LIMIT 1)='gave_up'
        LIMIT ?""", (limit,)).fetchall()
    for row in rows:
        route_recovery(conn, row['id'], reason=row['last_failure_error'] or 'worker failed',
                       failure_kind='worker_failure', expected_status=row['status'])


_SYSTEM_FAILURES = frozenset({
    'kanban_request_token_budget', 'kanban_total_token_budget', 'kanban_invalid_token_budget',
    'profile_or_probe_unavailable', 'worker_startup_failed', 'protocol_violation',
    'review_handoff_input_invalid', 'missing_toolset', 'missing_command',
})


def _system_failure(reason):
    # Share a known infrastructure category only, never raw private task text,
    # filenames, mailbox data, stack traces or model-produced "redactions".
    for code in sorted(_SYSTEM_FAILURES):
        if code in str(reason):
            return code
    return None


def _queue_engineering_repair(conn, task_id, *, fingerprint, reason, contract):
    from hermes_cli import kanban_db as kb
    cfg = settings().get('engineering_recovery') or {}
    if (not isinstance(cfg, dict) or not cfg.get('board') or contract['owner_workflow'].get('engineering_case')
            or cfg.get('task_id') == task_id):
        return None
    code = _system_failure(reason)
    if not code:
        # No raw text crosses the realm. Engineering owns diagnosis of the
        # repeated recovery mechanism; the original owner retains source work.
        code = 'unclassified_worker_recovery'
    owner, maker = cfg.get('owner'), cfg.get('maker')
    rule = (settings().get('departments') or {}).get('engineering_systems') or {}
    native = rule.get('execution') == 'native_owner' and rule.get('owner') == owner and rule.get('maker') == maker
    producer, reviewer = (owner, 'grace') if native else (maker, owner)
    if not owner or not maker or owner == maker or not oc._installed_profile(producer) or not oc._installed_profile(reviewer):
        return None
    board = cfg['board']
    # Worker environments pin HERMES_KANBAN_DB to the original private board.
    # Resolve the explicitly configured engineering board without that pin.
    target = (kb.board_dir(board) / 'kanban.db').resolve()
    current = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
    incident = hashlib.sha256((str(current) + ':' + task_id + ':' + fingerprint).encode()).hexdigest()
    existing = _latest(conn, task_id, 'owner_engineering_repair')
    if existing and existing.get('incident') == incident and existing.get('board') == board:
        with (contextlib.nullcontext(conn) if target == current else kb.connect_closing(target, board=board)) as check:
            active = kb.get_task(check, existing['task_id'])
            if active and active.status != 'archived':
                return existing
    if conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='owner_engineering_repair'", (task_id,)).fetchone()[0] >= 2:
        return None
    body = (
        f'Infrastructure recovery incident {incident}. Known failure category: {code}. '
        'A department-owned job remains open while this repair is investigated. '
        'The original private task, its source documents, and other boards are outside this repair scope. '
        f'Inspect the installed infrastructure source at {Path(__file__).resolve().parents[1]}. '
        'Reproduce this failure category using synthetic fixtures, implement a narrow correction, '
        'run the canonical regression checks, and have the accountable engineering owner review '
        'the exact patch and evidence. Preserve originals and concurrent changes. '
        'Use existing approved access only. Do not alter credentials, broaden permissions or '
        'disable review gates. A live restart/deployment still follows its existing authorization. '
        'Return a precise repair receipt naming the tested change, verification artifact and '
        'whether it is active or still awaiting an explicit decision. A diagnosis or outline alone '
        'is not a completed repair. Do not claim that the original user outcome is finished.'
    )
    repair_contract = {
        'goal_id': 'repair-' + incident, 'objective': 'Repair infrastructure failure ' + code + ' incident ' + incident[:16],
        'owner_profile': owner, 'owner_role_alias': 'engineering_systems', 'runtime_profile': maker,
        'required_reviewer_profile': owner, 'owner_acceptance': {'mode': 'accountable_owner'},
        'router_mode': 'reviewed', 'risk_class': 'material', 'security_realm': 'company',
        'capability_bundle': 'engineering', 'worker_budget': {'producers': 1, 'reviewers': 1, 'extras': 0},
        'delegation_roles': {'maker': maker, 'reviewer': owner}, 'targets': [],
        'acceptance_test': {'required': True, 'expected': 'Reproduction, tested patch and exact owner acceptance'},
        'rollback': {'procedure_ref': 'preserve-originals'},
        'callback': {'source_surface': 'internal-recovery', 'return_surface': 'internal-recovery', 'report_policy': 'on_demand'},
        'approval_gates': ['deploy', 'restart', 'security_change', 'privacy_wall_change'],
        'owner_workflow': {'version': VERSION, 'max_identical_recoveries': 2, 'engineering_case': True, 'advisory_reviewers': []},
    }
    if native:
        repair_contract.pop('owner_workflow')
        repair_contract.pop('owner_acceptance')
        repair_contract.update(runtime_profile=producer, required_reviewer_profile=reviewer,
            delegation_roles={'maker': producer, 'reviewer': reviewer})
    with (contextlib.nullcontext(conn) if target == current else kb.connect_closing(target, board=board)) as repairs:
        canonical = kb.get_task(repairs, str(cfg.get('task_id') or ''))
        if canonical and canonical.status not in {'done', 'archived'}:
            goal_row = repairs.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?', (canonical.id,)).fetchone()
            bound = json.loads(goal_row['contract_json']) if goal_row else {}
            # A running legacy repair keeps its producer/reviewer authority
            # until the operator can migrate it at an idle boundary.
            compatible_legacy = (native and bound.get('runtime_profile') == maker
                and oc._installed_profile(maker)
                and bound.get('required_reviewer_profile') in {owner, 'grace'})
            compatible_native = (bound.get('runtime_profile') == producer
                and (not native or bound.get('required_reviewer_profile') == reviewer))
            if bound.get('owner_profile') != owner or not (compatible_legacy or compatible_native):
                raise oc.OwnerCellGuardError('configured engineering task does not match its accountable owner and maker')
            repair_id = canonical.id
            with kb.write_txn(repairs, allow_nested=True):
                kb._append_event(repairs, repair_id, 'owner_repair_incident', {'incident': incident, 'failure_code': code})
        else:
            repair_id = kb.create_task(repairs, title='Repair infrastructure failure: ' + code, body=body,
            assignee=producer, created_by='owner-workflow-recovery', board=board,
            owner_cell_contract=repair_contract, idempotency_key='owner-repair:' + incident,
            max_runtime_seconds=900, max_retries=2)
    receipt = {'incident': incident, 'board': board, 'task_id': repair_id, 'failure_code': code,
               'owner': contract['owner_profile'], 'state': 'engineering_repair_pending',
               'blocked_run_id': kb.get_task(conn, task_id).current_run_id or (existing or {}).get('blocked_run_id')}
    kb._append_event(conn, task_id, 'owner_engineering_repair', receipt)
    return receipt


def _repair_wait_is_current(conn, task_id, receipt):
    from hermes_cli import kanban_db as kb
    if not kb._has_sticky_block(conn, task_id):
        return True
    latest = conn.execute("SELECT kind,run_id,payload FROM task_events WHERE task_id=? AND kind IN ('blocked','unblocked','capability_preflight_blocked','worktree_preclaim_blocked') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return bool(latest and latest['kind'] == 'blocked' and receipt.get('blocked_run_id')
        and latest['run_id'] == receipt['blocked_run_id']
        and json.loads(latest['payload']).get('kind') in {'capability','transient'})


def reconcile_engineering_repairs(conn, *, limit=4):
    """Wake the retained owner after engineering review; never auto-approve work."""
    from hermes_cli import kanban_db as kb
    rows = conn.execute("""SELECT e.task_id,e.payload FROM task_events e JOIN tasks t ON t.id=e.task_id
        WHERE e.kind='owner_engineering_repair' AND t.status='blocked' AND t.claim_lock IS NULL
        AND t.current_run_id IS NULL AND e.id=(SELECT MAX(id) FROM task_events
            WHERE task_id=t.id AND kind='owner_engineering_repair') LIMIT ?""", (limit,)).fetchall()
    for event in rows:
        receipt = json.loads(event['payload'])
        current = state(conn, event['task_id'])
        if current is None or not _repair_wait_is_current(conn, event['task_id'], receipt):
            continue
        repair_path = kb.board_dir(receipt['board']) / 'kanban.db'
        if not repair_path.exists():
            with kb.write_txn(conn):
                escalation = _latest(conn, event['task_id'], 'owner_recovery_escalated') or {}
                _queue_engineering_repair(conn, event['task_id'], fingerprint=escalation.get('fingerprint', receipt['incident']),
                    reason=receipt['failure_code'], contract=current[1])
            continue
        same_database = repair_path.resolve() == Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
        with (contextlib.nullcontext(conn) if same_database else kb.connect_closing(repair_path, board=receipt['board'])) as repairs:
            repair = kb.get_task(repairs, receipt['task_id'])
            if not repair or repair.status == 'archived':
                with kb.write_txn(conn):
                    escalation = _latest(conn, event['task_id'], 'owner_recovery_escalated') or {}
                    _queue_engineering_repair(conn, event['task_id'], fingerprint=escalation.get('fingerprint', receipt['incident']),
                        reason=receipt['failure_code'], contract=current[1])
                continue
            if repair.status != 'done':
                continue
            try:
                oc.assert_completion_allowed(repairs, repair.id)
            except oc.OwnerCellGuardError:
                # An inconsistent completed repair cannot stall dispatch of
                # unrelated jobs or silently release its original task.
                continue
        with kb.write_txn(conn):
            task = kb.get_task(conn, event['task_id'])
            if (task.status != 'blocked' or task.current_run_id or task.block_kind == 'needs_input'
                    or not _repair_wait_is_current(conn, task.id, receipt)):
                continue
            owner = current[1]['owner_profile']
            landing = kb._landing_status_after_parents(conn, task.id)
            conn.execute('UPDATE tasks SET status=?,assignee=?,runtime_profile=?,block_kind=NULL WHERE id=?', (landing, owner, owner, task.id))
            conn.execute("UPDATE owner_cell_goals SET phase='owner_recovery',runtime_profile=? WHERE task_id=?", (owner, task.id))
            kb._append_event(conn, task.id, 'unblocked', {'reason': 'Exact associated engineering repair passed owner review', 'source': 'owner-workflow'})
            kb._append_event(conn, task.id, 'owner_engineering_repair_returned', {
                'board': receipt['board'], 'task_id': repair.id, 'owner': owner,
                'required': 'Inspect the engineering receipt, verify this original job is ready, and use the normal owner handoff. Repair completion is not user-outcome completion.'})


_ADVISORY_TOOLS = {'claude-review': 'claude_opus_review', 'fable-review': 'claude_fable_review'}


def _advisory_contract_digest(raw):
    contract = json.loads(raw)
    if contract.get('owner_workflow'):
        # Adding a second reviewer does not invalidate the first review of the
        # same packet. Current required_advisors still enforces every new gate.
        contract['owner_workflow'].pop('advisory_reviewers', None)
        raw = json.dumps(contract, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _assert_advisory_binding(conn, task_id, row, binding):
    """New receipts bind scope as well as bytes; retain old native receipts."""
    body = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()[0] or ''
    expected = {'parent_state_version': row['current_state_version'],
                'parent_contract_digest': _advisory_contract_digest(row['contract_json']),
                'parent_body_digest': hashlib.sha256(body.encode()).hexdigest()}
    has_scope = any(key in binding for key in expected)
    legacy = not json.loads(row['contract_json']).get('owner_workflow')
    if (binding.get('artifact_digest') != row['review_artifact_digest']
            or ((has_scope or legacy) and any(binding.get(key) != value for key, value in expected.items()))):
        raise oc.OwnerCellGuardError('advisory receipt belongs to a different artifact or task scope')


def advisory_waiver_status(conn, task_id):
    """Return a current explicit waiver, never fabricate an advisory PASS."""
    current = _advisory_state(conn, task_id)
    decision = _latest(conn, task_id, 'owner_advisory_waiver')
    if not current or not decision or decision.get('decision') != 'waive':
        return None
    row, contract = current
    if row.get('plain_advisory_only'):
        return None
    rule = (settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
    body = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()[0] or ''
    if (rule.get('allow_explicit_fable_waiver') is not True
            or decision.get('profile') != 'fable-review'
            or decision.get('artifact_digest') != row['review_artifact_digest']
            or decision.get('state_version') != row['current_state_version']
            or decision.get('contract_digest') != hashlib.sha256(row['contract_json'].encode()).hexdigest()
            or decision.get('body_digest') != hashlib.sha256(body.encode()).hexdigest()):
        return None
    return decision


def record_advisory_waiver(conn, task_id, *, expected_run_id, review_decision_capability,
                           profile, decision, confirm_manual, authorization_ref, authorization_text, summary):
    """Accountable owner attests an explicit task-specific user decision.

    This uses the same owner-attested manual authorization boundary as premium
    Fable invocation. It is not an independently authenticated user signature.
    """
    from hermes_cli import kanban_db as kb
    if profile != 'fable-review' or decision not in {'waive', 'revoke'}:
        raise oc.OwnerCellGuardError('only the Fable advisory can receive a user waiver')
    if (confirm_manual is not True or not isinstance(authorization_ref, str)
            or not 3 <= len(authorization_ref.strip()) <= 500
            or not isinstance(authorization_text, str) or not 10 <= len(authorization_text.strip()) <= 1000
            or not isinstance(summary, str) or not summary.strip()):
        raise oc.OwnerCellGuardError('an explicit user decision, its source reference and exact authorization text are required; low risk alone is not authorization')
    with kb.write_txn(conn):
        current = _advisory_state(conn, task_id); task = kb.get_task(conn, task_id)
        if not current or not task:
            raise oc.OwnerCellGuardError('owner workflow is required')
        row, contract = current
        rule = (settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
        if rule.get('allow_explicit_fable_waiver') is not True or profile not in required_advisors(contract):
            raise oc.OwnerCellGuardError('this department has no configured explicit-user Fable waiver path')
        if (task.status != 'running' or not expected_run_id or task.current_run_id != expected_run_id
                or task.assignee != contract['owner_profile'] or row['phase'] != 'review'):
            raise oc.OwnerCellGuardError('the claimed accountable owner must hold final artifact review to record a waiver')
        if not review_decision_capability:
            raise oc.OwnerCellGuardError('advisory waiver requires current native review authority')
        kb._validate_review_decision_capability(conn, task_id, review_decision_capability, expected_run_id=expected_run_id)
        if (not row['review_artifact_ref'] or not row['review_artifact_digest']
                or not oc.digest_matches(row['review_artifact_ref'], row['review_artifact_digest'])):
            raise oc.OwnerCellGuardError('bind the exact current artifact before recording a task-specific waiver')
        receipt = {'profile': profile, 'decision': decision, 'owner': contract['owner_profile'],
            'artifact_digest': row['review_artifact_digest'], 'state_version': row['current_state_version'],
            'contract_digest': hashlib.sha256(row['contract_json'].encode()).hexdigest(),
            'body_digest': hashlib.sha256((task.body or '').encode()).hexdigest(),
            'authorization_ref': authorization_ref.strip(), 'authorization_text': authorization_text.strip(),
            'authorization_basis': 'accountable_owner_attestation_of_explicit_user_instruction',
            'summary': summary.strip(), 'advisory_passed': False}
        kb._append_event(conn, task_id, 'owner_advisory_waiver', receipt, run_id=expected_run_id)
        return {'task_id': task_id, 'status': 'waived_by_user' if decision == 'waive' else 'waiver_revoked',
                'artifact_digest': row['review_artifact_digest'], 'advisory_passed': False,
                'owner_acceptance_and_action_approvals_still_required': True}


def request_advisory(conn, task_id, *, expected_run_id, review_decision_capability,
                     profile, packet, summary, confirm_manual=False, board=None,
                     artifact_ref=None, artifact_digest=None):
    """Park owner review for a scoped redacted advisory packet, not a local file."""
    from hermes_cli import kanban_db as kb
    import re
    if not isinstance(packet, str) or not 20 <= len(packet.strip()) <= 12000:
        raise ValueError('a compact redacted packet of 20 to 12000 characters is required')
    if re.search(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b|/Users/|BEGIN.*PRIVATE KEY|(?:access_token|refresh_token|password)\s*[=:]', packet, re.I):
        raise oc.OwnerCellGuardError('redact personal identifiers, local paths and secrets before advisory review')
    with kb.write_txn(conn):
        current = _advisory_state(conn, task_id)
        task = kb.get_task(conn, task_id)
        if not current or not task:
            raise oc.OwnerCellGuardError('owner workflow is required')
        row, contract = current
        plain = row.get('plain_advisory_only', False)
        if plain:
            from hermes_cli.plain_advisory import bind_artifact
            row, contract = bind_artifact(conn, task_id, expected_run_id=expected_run_id,
                artifact_ref=artifact_ref, artifact_digest=artifact_digest)
        if (profile not in available_advisors(contract)
                or profile not in _ADVISORY_TOOLS):
            raise oc.OwnerCellGuardError('this advisory profile is not required by the original contract')
        if profile == 'fable-review' and confirm_manual is not True:
            raise oc.OwnerCellGuardError('Fable requires its existing explicit Chris authorization')
        if (task.status != 'running' or task.current_run_id != expected_run_id
                or not expected_run_id or task.assignee != contract['owner_profile']
                or row['phase'] not in ({'execute'} if plain else {'review', 'owner_recovery'})
                or not row['review_artifact_digest']):
            raise oc.OwnerCellGuardError('advisory handoff requires the claimed owner and a bound final artifact')
        if row['phase'] == 'review':
            if not review_decision_capability:
                raise oc.OwnerCellGuardError('advisory handoff requires current native review authority')
            kb._validate_review_decision_capability(conn, task_id, review_decision_capability, expected_run_id=expected_run_id)
        if (not row['review_artifact_ref']
                or not oc.digest_matches(row['review_artifact_ref'], row['review_artifact_digest'])):
            raise oc.OwnerCellGuardError('advisory handoff requires unchanged bound artifact bytes')
        if profile == 'fable-review' and advisory_waiver_status(conn, task_id):
            kb._append_event(conn, task_id, 'owner_advisory_waiver', {
                'profile': profile, 'decision': 'revoke', 'reason': 'New explicitly authorized Fable review request',
                'artifact_digest': row['review_artifact_digest']}, run_id=expected_run_id)
        packet = packet.strip()
        packet_digest = hashlib.sha256(packet.encode()).hexdigest()
        artifact_digest = row['review_artifact_digest']
        prior = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_advisory_requested'", (task_id,)).fetchall()
        same = [json.loads(x[0]) for x in prior if json.loads(x[0]).get('profile') == profile and json.loads(x[0]).get('artifact_digest') == artifact_digest]
        # Recover the existing exact reviewed packet instead of buying another
        # review or stranding the owner in the recovery production phase.
        if same:
            existing = same[-1]
            advisory = kb.get_task(conn, existing['advisory_task'])
            succeeded = _latest(conn, existing['advisory_task'], 'owner_advisory_succeeded') or {}
            if (existing.get('packet_digest') == packet_digest and advisory
                    and advisory.status in {'ready', 'running', 'done'}
                    and (_has_valid_external_advisory(conn, advisory.id)
                         or (advisory.status == 'done' and succeeded.get('packet_digest') == packet_digest))):
                _assert_advisory_binding(conn, task_id, row, existing)
                conn.execute("INSERT OR IGNORE INTO task_links(parent_id,child_id,edge_role) VALUES (?,?,'dependency')", (advisory.id, task_id))
                conn.execute("UPDATE tasks SET status='todo',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?", (task_id,))
                conn.execute("UPDATE owner_cell_goals SET phase='review' WHERE task_id=?", (task_id,))
                kb._end_run(conn, task_id, outcome='advisory_wait', status='todo', summary=summary)
                conn.execute('DELETE FROM target_leases WHERE task_id=?', (task_id,))
                kb._append_event(conn, task_id, 'owner_advisory_reused', existing)
                kb._append_event(conn, task_id, 'dependency_wait', {'source_status': 'ready' if plain else 'review', 'reason': 'resume exact existing advisory review'})
                return {'task_id': task_id, 'status': 'todo', 'phase': 'advisory_wait', 'advisory_task': advisory.id, 'reused': True}
        if len(same) >= 2:
            raise oc.OwnerCellGuardError('repeated advisory failure requires owner diagnosis before another attempt')
        tool = _ADVISORY_TOOLS[profile]
        safe_body = (f'Perform the bounded advisory pass using {tool} on exactly the packet below. '
            'Return findings to the accountable owner through kanban_complete. This is advisory only; '
            'do not inspect other tasks, local files, or source documents and do not approve the final deliverable. '
            + ('Chris authorization has been confirmed by the accountable owner; pass confirm_manual=true. ' if profile == 'fable-review' else '')
            + '\n\nREDACTED PACKET:\n' + packet + '\n\n```worker-requirements\n'
            + json.dumps({'toolsets': ['kanban', tool]}) + '\n```')
        advisory_contract = {**contract, 'goal_id': 'advisory-' + hashlib.sha256((task_id+profile+artifact_digest+str(len(same))).encode()).hexdigest(),
            'objective': 'Return advisory findings on packet ' + packet_digest + ' attempt ' + str(len(same)), 'owner_profile': profile,
            'runtime_profile': profile, 'router_mode': 'single', 'required_reviewer_profile': None,
            'delegation_roles': {'maker': profile, 'reviewer': None}, 'targets': [],
            'worker_budget': {'producers': 1, 'reviewers': 0, 'extras': 0},
            'callback': {'source_surface': 'internal-advisory', 'return_surface': 'internal-advisory', 'report_policy': 'on_demand'}}
        advisory_contract.pop('owner_workflow', None)
        advisory_contract.pop('owner_acceptance', None)
        advisory_contract.pop('command_board_flow', None)
        advisory_contract.pop('advisory_reviewers', None)
        aid = kb.create_task(conn, title='Review redacted packet', body=safe_body, assignee=profile,
            created_by='owner-workflow-advisory', owner_cell_contract=advisory_contract, board=board,
            max_runtime_seconds=600, max_retries=2,
            idempotency_key=advisory_contract['goal_id'])
        binding = {'advisory_task': aid, 'profile': profile, 'packet_digest': packet_digest,
                   'artifact_digest': artifact_digest, 'tool': tool, 'confirm_manual': confirm_manual,
                   'parent_state_version': row['current_state_version'],
                   'parent_contract_digest': _advisory_contract_digest(row['contract_json']),
                   'parent_body_digest': hashlib.sha256((task.body or '').encode()).hexdigest()}
        kb._append_event(conn, aid, 'owner_advisory_packet', {'packet_digest': packet_digest, 'tool': tool, 'profile': profile})
        conn.execute("INSERT INTO task_links(parent_id,child_id,edge_role) VALUES (?,?,'dependency')", (aid, task_id))
        conn.execute("UPDATE tasks SET status='todo',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?", (task_id,))
        conn.execute("UPDATE owner_cell_goals SET phase='review' WHERE task_id=?", (task_id,))
        kb._end_run(conn, task_id, outcome='advisory_wait', status='todo', summary=summary)
        conn.execute('DELETE FROM target_leases WHERE task_id=?', (task_id,))
        kb._append_event(conn, task_id, 'owner_advisory_requested', binding)
        kb._append_event(conn, task_id, 'dependency_wait', {'source_status': 'ready' if plain else 'review', 'reason': 'independent advisory packet review'})
        return {'task_id': task_id, 'status': 'todo', 'phase': 'advisory_wait', 'advisory_task': aid}


def record_advisory_result(*, tool, packet, result):
    """Called by the constrained plugin after actual successful execution."""
    from hermes_cli import kanban_db as kb
    import os
    tid, run_id = os.environ.get('HERMES_KANBAN_TASK'), os.environ.get('HERMES_KANBAN_RUN_ID')
    if not tid or not run_id:
        return
    with kb.connect_closing() as conn:
        expected = _latest(conn, tid, 'owner_advisory_packet')
        if not expected:
            return
        with kb.write_txn(conn):
            task = kb.get_task(conn, tid)
            if (task.status != 'running' or task.current_run_id != int(run_id)
                    or task.worker_pid != os.getpid() or task.assignee != expected['profile']
                    or tool != expected['tool'] or hashlib.sha256(packet.strip().encode()).hexdigest() != expected['packet_digest']
                    or not isinstance(result, str) or not result.strip()):
                raise oc.OwnerCellGuardError('advisory result does not match the claimed packet and runtime')
            kb._append_event(conn, tid, 'owner_advisory_succeeded', {'tool': tool, 'packet_digest': expected['packet_digest'],
                'result_digest': hashlib.sha256(result.encode()).hexdigest()}, run_id=int(run_id))


def _latest_fable_binding(conn, parent_task_id):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_advisory_requested' ORDER BY id DESC", (parent_task_id,))
    return next((item for row in rows if (item := json.loads(row[0])).get('profile') == 'fable-review'), None)


def import_fable_wrapper_receipt(conn, task_id, *, parent_task_id, receipt_path,
                                expected_receipt_digest, operator, reason):
    """Operator-only reconciliation of an already executed, authorized review.

    Not a model tool and not a synthetic in-run tool-success event. The operator
    attests wrapper execution provenance; file hashes bind the reviewed evidence.
    """
    from hermes_cli import kanban_db as kb
    import os
    if os.environ.get('HERMES_KANBAN_TASK') or not operator or not reason:
        raise oc.OwnerCellGuardError('external review reconciliation requires an authorized non-worker operator')
    if not oc.digest_matches(receipt_path, expected_receipt_digest):
        raise oc.OwnerCellGuardError('external review receipt changed')
    path = Path(receipt_path)
    if path.stat().st_size > 128000:
        raise oc.OwnerCellGuardError('external review receipt is oversized')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_receipt_digest:
        raise oc.OwnerCellGuardError('external review receipt changed during import')
    receipt = json.loads(raw)
    if not isinstance(receipt, dict):
        raise oc.OwnerCellGuardError('external review receipt must be an object')
    output, stderr = receipt.get('stdout'), receipt.get('stderr')
    if (type(receipt.get('returncode')) is not int or receipt['returncode'] != 0
            or not isinstance(output, str) or not 1 <= len(output.strip()) <= 24000
            or not isinstance(stderr, str) or len(stderr) > 10000):
        raise oc.OwnerCellGuardError('external review must contain a successful bounded result')
    provenance = [json.loads(line) for line in stderr.splitlines() if line.strip()]
    if not any(isinstance(p, dict) and p.get('requested_model') == 'claude-fable-5-1'
               and p.get('launcher') == 'first_party_subscription_only' and p.get('tools') == 'none'
               and p.get('turn_cap') in {1, 2} for p in provenance):
        raise oc.OwnerCellGuardError('canonical Fable wrapper provenance is missing')
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id); parent = _advisory_state(conn, parent_task_id)
        packet = _latest(conn, task_id, 'owner_advisory_packet')
        binding = _latest_fable_binding(conn, parent_task_id)
        if (not task or task.status not in {'ready', 'blocked', 'todo'} or task.current_run_id
                or task.worker_pid or task.claim_lock or task.claim_expires or not parent or not packet or not binding
                or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()):
            raise oc.OwnerCellGuardError('external review reconciliation requires the idle original advisory and parent')
        if (task.assignee != 'fable-review' or packet.get('profile') != 'fable-review'
                or packet.get('tool') != 'claude_fable_review' or binding.get('confirm_manual') is not True
                or binding.get('advisory_task') != task_id or receipt.get('advisory_task') != task_id
                or receipt.get('packet_sha256') != packet['packet_digest']
                or binding.get('packet_digest') != packet['packet_digest']
                or receipt.get('manifest_sha256') != binding.get('artifact_digest')
                or binding.get('artifact_digest') != parent[0]['review_artifact_digest']
                or not oc.digest_matches(parent[0]['review_artifact_ref'], binding.get('artifact_digest'))):
            raise oc.OwnerCellGuardError('external review is not bound to the exact authorized Fable packet and artifact')
        _assert_advisory_binding(conn, parent_task_id, parent[0], binding)
        parent_body = conn.execute('SELECT body FROM tasks WHERE id=?', (parent_task_id,)).fetchone()[0] or ''
        record = {'receipt_ref': str(path.resolve()), 'receipt_digest': expected_receipt_digest,
            'packet_digest': packet['packet_digest'], 'artifact_digest': binding['artifact_digest'],
            'result_digest': hashlib.sha256(output.strip().encode()).hexdigest(), 'model': 'claude-fable-5-1',
            'parent_task': parent_task_id, 'parent_state_version': parent[0]['current_state_version'],
            'parent_body_digest': hashlib.sha256(parent_body.encode()).hexdigest(),
            'body_digest': hashlib.sha256((task.body or '').encode()).hexdigest(),
            'operator': operator, 'reason': reason, 'source': 'operator_verified_existing_wrapper_execution'}
        previous = _latest(conn, task_id, 'owner_advisory_external_receipt')
        if previous != record:
            kb._append_event(conn, task_id, 'owner_advisory_external_receipt', record)
        return {'task_id': task_id, 'receipt_digest': expected_receipt_digest,
                'review_execution': 'existing_external_wrapper', 'task_completed': False}


def _has_valid_external_advisory(conn, task_id):
    record = _latest(conn, task_id, 'owner_advisory_external_receipt')
    if not record:
        return False
    task = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
    parent = _advisory_state(conn, record['parent_task'])
    parent_task = conn.execute('SELECT body FROM tasks WHERE id=?', (record['parent_task'],)).fetchone()
    packet = _latest(conn, task_id, 'owner_advisory_packet') or {}
    binding = _latest_fable_binding(conn, record['parent_task']) or {}
    try:
        if parent:
            _assert_advisory_binding(conn, record['parent_task'], parent[0], binding)
        return bool(task and parent and parent_task
            and packet.get('profile') == 'fable-review' and packet.get('tool') == 'claude_fable_review'
            and packet.get('packet_digest') == record['packet_digest']
            and binding.get('advisory_task') == task_id and binding.get('confirm_manual') is True
            and binding.get('packet_digest') == record['packet_digest']
            and hashlib.sha256((task['body'] or '').encode()).hexdigest() == record['body_digest']
            and hashlib.sha256((parent_task['body'] or '').encode()).hexdigest() == record['parent_body_digest']
            and parent[0]['current_state_version'] == record['parent_state_version']
            and parent[0]['review_artifact_digest'] == record['artifact_digest']
            and oc.digest_matches(parent[0]['review_artifact_ref'], record['artifact_digest'])
            and oc.digest_matches(record['receipt_ref'], record['receipt_digest']))
    except (OSError, ValueError, oc.OwnerCellGuardError):
        return False


def assert_advisories_complete(conn, task_id, *, expected_run_id=None, metadata=None):
    """An owner assertion cannot replace an actual required advisory invocation."""
    from hermes_cli import kanban_db as kb
    packet = _latest(conn, task_id, 'owner_advisory_packet')
    if packet:
        succeeded = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_advisory_succeeded' ORDER BY id DESC LIMIT 1", (task_id, expected_run_id)).fetchone()
        if (not succeeded or json.loads(succeeded[0]).get('packet_digest') != packet['packet_digest']) and not _has_valid_external_advisory(conn, task_id):
            raise oc.OwnerCellGuardError('complete the required advisory tool call on the exact redacted packet before completing this advisory task')
    current = _advisory_state(conn, task_id)
    if not current:
        if _latest(conn, task_id, 'owner_advisory_requested'):
            raise oc.OwnerCellGuardError('original advisory owner review contract is no longer eligible')
        return
    row, contract = current
    if row.get('plain_advisory_only'):
        from hermes_cli.plain_advisory import assert_owner_acceptance
        assert_owner_acceptance(conn, task_id, row, contract, expected_run_id, metadata)
    if row['phase'] in OWNER_PHASES:
        raise oc.OwnerCellGuardError('owner intake/recovery cannot complete the outcome; a reviewed final artifact is required')
    if row['phase'] == 'owner_complete':
        task = kb.get_task(conn, task_id)
        if (not contract['owner_workflow'].get('owner_completion') or task.status != 'running'
                or task.assignee != contract['owner_profile'] or not expected_run_id
                or task.current_run_id != expected_run_id):
            raise oc.OwnerCellGuardError('only the currently claimed accountable owner may complete this task')
        artifact = (metadata or {}).get('owner_final_artifact')
        if (not isinstance(artifact, dict) or not artifact.get('ref') or not artifact.get('digest')
                or not oc.digest_matches(artifact['ref'], artifact['digest'])):
            raise oc.OwnerCellGuardError('owner_final_artifact must bind the exact finished file and actual SHA256')
        criteria = (_latest(conn, task_id, 'owner_completion_started') or {}).get('acceptance_criteria', [])
    else:
        if not row['review_artifact_ref'] or not oc.digest_matches(row['review_artifact_ref'], row['review_artifact_digest']):
            raise oc.OwnerCellGuardError('final artifact bytes changed or are missing; bind the current artifact for fresh review')
        criteria = (_latest(conn, task_id, 'owner_handoff') or {}).get('acceptance_criteria', [])
    checks = (metadata or {}).get('owner_acceptance_checks')
    if contract.get('owner_workflow') and (not isinstance(checks, list) or not criteria or any(
        not any(isinstance(check, dict) and check.get('criterion') == criterion
                and check.get('passed') is True and isinstance(check.get('evidence'), str)
                and check['evidence'].strip() for check in checks)
        for criterion in criteria
    )):
        raise oc.OwnerCellGuardError('record metadata.owner_acceptance_checks for every brief criterion: criterion, passed=true, and concrete evidence')
    bindings = [json.loads(x[0]) for x in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='owner_advisory_requested' ORDER BY id DESC", (task_id,))]
    requested = [b['profile'] for b in bindings if b.get('artifact_digest') == row['review_artifact_digest']]
    for profile in dict.fromkeys([*required_advisors(contract), *requested]):
        if profile == 'fable-review' and advisory_waiver_status(conn, task_id):
            continue
        matches = [b for b in bindings if b.get('profile') == profile and b.get('artifact_digest') == row['review_artifact_digest']]
        if not matches:
            raise oc.OwnerCellGuardError(f'required advisory review by {profile} is missing; use kanban_handoff action=advisory with a redacted packet')
        binding = matches[0]
        _assert_advisory_binding(conn, task_id, row, binding)
        advisory = kb.get_task(conn, binding['advisory_task'])
        if not advisory or advisory.status != 'done' or not (_latest(conn, advisory.id, 'owner_advisory_succeeded') or _has_valid_external_advisory(conn, advisory.id)):
            raise oc.OwnerCellGuardError('required advisory review is unfinished; preserve owner acceptance and resolve the advisory findings')


def recover_advisory_failures(conn, *, limit=4):
    """A failed advisory returns to its owner without waiving the final gate."""
    from hermes_cli import kanban_db as kb
    rows = conn.execute("""SELECT e.task_id,e.payload FROM task_events e JOIN tasks t ON t.id=e.task_id
        WHERE e.kind='owner_advisory_requested' AND t.status='todo' AND t.claim_lock IS NULL
        ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
    for event in rows:
        binding = json.loads(event['payload'])
        advisory = kb.get_task(conn, binding['advisory_task'])
        current = _advisory_state(conn, event['task_id'])
        if not advisory or advisory.status not in {'blocked','triage'} or not current:
            continue
        with kb.write_txn(conn):
            root = kb.get_task(conn, event['task_id'])
            if root.status != 'todo' or root.claim_lock:
                continue
            if not conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=? AND edge_role='dependency'",
                                (advisory.id, root.id)).fetchone():
                continue  # A historical failed attempt is not the current dependency.
            # The failed attempt ceases to be a launch prerequisite. Its actual
            # success is still independently required by final acceptance above.
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=? AND edge_role='dependency'", (advisory.id, root.id))
            owner = current[1]['owner_profile']
            landing = kb._landing_status_after_parents(conn, root.id)
            legacy = not current[1].get('owner_workflow') and not current[0].get('plain_advisory_only')
            if legacy and landing == 'ready':
                landing = 'review'
            if current[0].get('plain_advisory_only'):
                conn.execute('UPDATE tasks SET status=? WHERE id=?', (landing, root.id))
            else:
                conn.execute('UPDATE tasks SET status=?,assignee=?,runtime_profile=? WHERE id=?', (landing, owner, owner, root.id))
            conn.execute('UPDATE owner_cell_goals SET phase=?,runtime_profile=? WHERE task_id=?',
                         ('review' if legacy else 'owner_recovery', owner, root.id))
            kb._append_event(conn, root.id, 'owner_recovery_requested', {'owner': owner, 'failure_kind': 'advisory',
                'advisory_task': advisory.id, 'reason': 'Required advisory attempt failed. Inspect its allowed error, repair and retry the redacted advisory handoff. The final advisory gate remains required.'})

"""Operator-attached advisory gates for an existing plain legal-owner task.

No owner-cell, maker, waiver, review capability, status change, or access grant
is created. The owner still produces and accepts; constrained advisors supply
independent findings, never an owner-cell PASS. Attachment is operator-only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from hermes_cli import owner_cell as oc


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot_digest(conn, task_id):
    """CAS over original task, runs, events, comments and dependency edges."""
    result = {}
    for table in ('tasks', 'task_runs', 'task_events', 'task_comments'):
        key = 'id' if table == 'tasks' else 'task_id'
        result[table] = [dict(r) for r in conn.execute(
            f'SELECT * FROM {table} WHERE {key}=? ORDER BY id', (task_id,))]
    result['links'] = [dict(r) for r in conn.execute(
        'SELECT * FROM task_links WHERE parent_id=? OR child_id=? ORDER BY parent_id,child_id,edge_role',
        (task_id, task_id))]
    return _digest(result)


def _scope(task):
    return _digest({k: getattr(task, k) for k in (
        'title', 'body', 'assignee', 'tenant', 'workspace_kind', 'workspace_path',
        'model_override', 'provider_override')})


def attach_idle_task(conn, task_id, *, expected_snapshot_digest, contract,
                     acceptance_criteria, actor, reason):
    """Record an additive gate, never requeue. Not exposed to worker tools.

    The operator must supply the inspected existing scope/holds. The snapshot
    binds that inspection; only an idle null-contract legal owner qualifies.
    """
    from hermes_cli import kanban_db as kb, owner_workflow as ow
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise oc.OwnerCellGuardError('plain advisory attachment is operator-only')
    if not actor or not reason or not expected_snapshot_digest:
        raise ValueError('operator identity, reason and exact inspected snapshot required')
    if (not isinstance(acceptance_criteria, list) or not acceptance_criteria
            or any(not isinstance(x, str) or not x.strip() for x in acceptance_criteria)):
        raise ValueError('exact original acceptance criteria required')
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if (not task or task.status not in {'triage', 'blocked', 'todo', 'ready'}
                or any(getattr(task, k) is not None for k in
                       ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))):
            raise oc.OwnerCellGuardError('attachment requires an idle held or unclaimed task')
        if (conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
                or conn.execute('SELECT 1 FROM target_leases WHERE task_id=?', (task_id,)).fetchone()
                or conn.execute('SELECT 1 FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
                or any(conn.execute('SELECT goal_id,owner_profile,reviewer_profile FROM tasks WHERE id=?', (task_id,)).fetchone())
                or ow._latest(conn, task_id, 'plain_advisory_attached')
                or conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('review_requested','changes_requested','owner_advisory_requested')", (task_id,)).fetchone()):
            raise oc.OwnerCellGuardError('existing authority or review history requires its original recovery route')
        if snapshot_digest(conn, task_id) != expected_snapshot_digest:
            raise oc.OwnerCellGuardError('task changed since operator inspection')
        oc.validate_goal_contract(contract)
        rule = (ow.settings().get('departments') or {}).get(contract['owner_role_alias']) or {}
        owner = contract['owner_profile']
        if (contract['owner_role_alias'] != 'legal_compliance' or owner != rule.get('owner')
                or owner != task.assignee or contract['runtime_profile'] != owner
                or contract['router_mode'] != 'single' or contract.get('required_reviewer_profile')
                or contract.get('owner_workflow') or contract.get('owner_acceptance')
                or contract.get('targets') or contract.get('extra_workers')
                or contract['worker_budget'] != {'producers': 1, 'reviewers': 0, 'extras': 0}
                or set(contract.get('advisory_reviewers', [])) != {'fable-review', 'claude-review'}
                or not contract.get('approval_gates')
                or oc._installed_profile(owner) != owner
                or not {'fable-review', 'claude-review'} <= set(ow.available_advisors(
                    {k: v for k, v in contract.items() if k != 'advisory_reviewers'}))):
            raise oc.OwnerCellGuardError('retain the configured legal owner, both advisors and original protected-action holds')
        receipt = {'contract': contract, 'acceptance_criteria': acceptance_criteria,
                   'scope_digest': _scope(task), 'snapshot_digest': expected_snapshot_digest,
                   'actor': actor, 'reason': reason}
        kb._append_event(conn, task_id, 'plain_advisory_attached', receipt)
        return {'task_id': task_id, 'status': task.status, 'owner': owner,
                'advisory_reviewers': contract['advisory_reviewers'],
                'requeued': False, 'independent_owner_review': False}


def state(conn, task_id):
    from hermes_cli import kanban_db as kb, owner_workflow as ow
    receipt = ow._latest(conn, task_id, 'plain_advisory_attached')
    if not receipt:
        return None
    task = kb.get_task(conn, task_id)
    if (not task or _scope(task) != receipt['scope_digest']
            or any(conn.execute('SELECT goal_id,owner_profile,reviewer_profile FROM tasks WHERE id=?', (task_id,)).fetchone())
            or conn.execute('SELECT 1 FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()):
        raise oc.OwnerCellGuardError('plain advisory task scope or authority changed; operator inspection required')
    binding = ow._latest(conn, task_id, 'plain_advisory_artifact') or {}
    contract = receipt['contract']
    return {'plain_advisory_only': True, 'phase': 'completed' if task.status == 'done' else 'execute',
            'contract_json': json.dumps(contract, sort_keys=True),
            'current_state_version': binding.get('version', 0),
            'review_artifact_ref': binding.get('ref'), 'review_artifact_digest': binding.get('digest'),
            'acceptance_criteria': receipt['acceptance_criteria']}, contract


def bind_artifact(conn, task_id, *, expected_run_id, artifact_ref, artifact_digest):
    from hermes_cli import kanban_db as kb
    row, contract = state(conn, task_id)
    task = kb.get_task(conn, task_id)
    if (not expected_run_id or task.current_run_id != expected_run_id or task.status != 'running'
            or task.assignee != contract['owner_profile']):
        raise oc.OwnerCellGuardError('exact current owner production run required')
    if not artifact_ref or not artifact_digest or not task.workspace_path:
        raise oc.OwnerCellGuardError('bind exact finished candidate with brief_ref and brief_digest')
    path = Path(artifact_ref)
    if not path.is_absolute() or not path.resolve().is_relative_to(Path(task.workspace_path).resolve()):
        raise oc.OwnerCellGuardError('advisory candidate must remain in the assigned workspace')
    if not oc.digest_matches(artifact_ref, artifact_digest):
        raise oc.OwnerCellGuardError('advisory requires unchanged exact artifact bytes')
    if (row['review_artifact_ref'], row['review_artifact_digest']) != (artifact_ref, artifact_digest):
        kb._append_event(conn, task_id, 'plain_advisory_artifact', {
            'ref': artifact_ref, 'digest': artifact_digest, 'version': row['current_state_version'] + 1},
            run_id=expected_run_id)
    return state(conn, task_id)


def assert_owner_acceptance(conn, task_id, row, contract, expected_run_id, metadata):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    artifact = (metadata or {}).get('owner_final_artifact') or {}
    checks = (metadata or {}).get('owner_acceptance_checks')
    if (task.status != 'running' or not expected_run_id or task.current_run_id != expected_run_id
            or task.assignee != contract['owner_profile']
            or artifact.get('ref') != row['review_artifact_ref']
            or artifact.get('digest') != row['review_artifact_digest']
            or not row['review_artifact_digest']):
        raise oc.OwnerCellGuardError('claimed owner must accept the exact advisory-bound final artifact')
    if not isinstance(checks, list) or any(not any(
            isinstance(c, dict) and c.get('criterion') == criterion and c.get('passed') is True
            and isinstance(c.get('evidence'), str) and c['evidence'].strip()
            for c in checks) for criterion in row['acceptance_criteria']):
        raise oc.OwnerCellGuardError('exact owner acceptance checks and advisory disposition evidence required')


def recover_idle_task(conn, task_id, *, expected_snapshot_digest, evidence_ref,
                      evidence_digest, actor, reason):
    """Operator-attested, exact-evidence recovery for a plain blocked/triage card.

    This validates a recorded resolution, not user authorization itself. The
    operator must actually inspect the authorized execution and every remaining
    hold. Approval alone or a model's promise of capability is not resolution.
    Existing owner-cell review recoveries deliberately remain out of scope.
    """
    from hermes_cli import kanban_db as kb
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise oc.OwnerCellGuardError('resolved-block recovery is operator-only')
    if not actor or not reason or not expected_snapshot_digest:
        raise ValueError('operator, reason and inspected snapshot required')
    if not oc.digest_matches(evidence_ref, evidence_digest):
        raise oc.OwnerCellGuardError('resolution evidence digest changed')
    evidence_bytes = Path(evidence_ref).read_bytes()
    if hashlib.sha256(evidence_bytes).hexdigest() != evidence_digest:
        raise oc.OwnerCellGuardError('resolution evidence changed during read')
    proof = json.loads(evidence_bytes)
    if (not isinstance(proof, dict) or proof.get('task_id') != task_id
            or proof.get('authorization_verified') is not True
            or not isinstance(proof.get('authorization_ref'), str) or not proof['authorization_ref'].strip()
            or proof.get('execution_succeeded') is not True
            or not isinstance(proof.get('execution_ref'), str) or not proof['execution_ref'].strip()
            or proof.get('capability_verified') is not True or proof.get('unresolved_holds') != []):
        raise oc.OwnerCellGuardError('existing authorization AND successful execution/capability evidence with no unresolved holds required; do not repeat approval or blindly retry')
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if (not task or task.status not in {'blocked', 'triage'}
                or task.block_kind in {'needs_input', 'dependency'}
                or any(getattr(task, k) is not None for k in
                       ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))
                or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
                or conn.execute('SELECT 1 FROM target_leases WHERE task_id=?', (task_id,)).fetchone()
                or conn.execute('SELECT 1 FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
                or any(conn.execute('SELECT goal_id,owner_profile,reviewer_profile FROM tasks WHERE id=?', (task_id,)).fetchone())):
            raise oc.OwnerCellGuardError('only idle plain capability/transient work may use this recovery; preserve other holds and review authority')
        if snapshot_digest(conn, task_id) != expected_snapshot_digest:
            raise oc.OwnerCellGuardError('task changed since resolution inspection')
        block = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected','capability_preflight_blocked','worktree_preclaim_blocked') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        payload = json.loads(block['payload'] or '{}') if block else {}
        if (not block or block['id'] != proof.get('resolved_block_event_id')
                or payload.get('kind') not in {'capability', 'transient'}
                or kb._resume_status_from_events(conn, task_id) != 'ready'):
            raise oc.OwnerCellGuardError('resolution must cover the current capability/transient block, not an earlier approved action')
        # Validate any attached scope, but do not require a new advisory route
        # for an unrelated resolved validation failure.
        state(conn, task_id)
        landing = kb._landing_status_after_parents(conn, task_id)
        conn.execute('UPDATE tasks SET status=?,block_kind=NULL WHERE id=?', (landing, task_id))
        kb._append_event(conn, task_id, 'unblocked', {
            'source_status': task.status, 'resume_status': 'ready', 'status': landing,
            'resolved_block_event_id': block['id'], 'snapshot_digest': expected_snapshot_digest,
            'evidence_ref': evidence_ref, 'evidence_digest': evidence_digest,
            'actor': actor, 'reason': reason, 'basis': 'operator_verified_existing_authorization_and_execution',
            'counters_preserved': True, 'approval_granted': False})
        return {'task_id': task_id, 'status': landing, 'new_user_approval_required': False,
                'approval_granted': False, 'counters_preserved': True,
                'next_action': 'Verify actual owner claim/heartbeat and the previously failing operation; recovery is not completion.'}

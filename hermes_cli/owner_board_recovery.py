"""Exact-snapshot adapter for recovering an idle bound review.

Call only after trusted owner/board/private-task admission. This module repairs
the existing reviewer projection; it never supplies a verdict or spends a run.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re

from hermes_cli import kanban_db as kb
from hermes_cli import owner_cell as oc


def goal_snapshot_sha(row) -> str:
    return hashlib.sha256(json.dumps(dict(row), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _recover_ordinary_review(conn, task, goal, stop, *, actor, reason):
    """Reuse the existing exact-loop transition with fresh reviewer admission.

    No supplied profile, database, verdict or admission is accepted. The outer
    transaction binds the task/goal/comments; the lower transition also binds
    its original stopped run and immutable artifact before changing status.
    """
    from hermes_cli.board_owner_manage import require_owner, validate_bound_target
    from hermes_cli.owner_work_preparation import _connection_board

    run = conn.execute("SELECT * FROM task_runs WHERE id=? AND task_id=?",
                       (stop["run_id"], task["id"])).fetchone()
    if run is None and task["status"] == "blocked":
        # 2026-10-07 (t_017ce141): a stop recorded without a run (synthesized or
        # operator block) must not strand the review. The blocked path re-admits
        # the bound reviewer freshly and never uses the run snapshot.
        run = conn.execute("SELECT * FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                           (task["id"],)).fetchone() or {}
    if run is None:
        raise ValueError("Original stopped reviewer run is missing; inspect review history before recovery")
    member = require_owner()
    board = _connection_board(conn, member)
    projected = dataclasses.replace(kb.get_task(conn, task["id"]), status="review")
    try:
        admission = kb._build_worker_admission(projected, goal["required_reviewer_profile"], board=board)
    except kb.WorkerAdmissionError as exc:
        if isinstance(exc.__cause__, oc.OwnerCellGuardError):
            # Only this internal guard's bounded diagnostic is safe to expose;
            # provider/credential failures retain the existing generic message.
            detail = ' '.join(str(exc.__cause__).split())[:500]
            raise ValueError('Reviewer readiness: ' + detail) from exc
        raise
    # Discovery may take time: authority and the exact board cannot go stale
    # while probing the reviewer's current configured capabilities.
    if require_owner() != member:
        raise PermissionError("Owner authority changed during reviewer admission")
    validate_bound_target(conn, task["id"], "recover_review")
    if _connection_board(conn, member) != board:
        raise PermissionError("Review recovery board changed during admission")
    database = kb._database_identity_contract(kb.kanban_db_path(board=board))
    if admission.contract.get("board_database") != database:
        raise PermissionError("Reviewer admission belongs to a different board")
    if task["status"] == "blocked":
        if not kb.unblock_task(conn, task["id"], preserve_failure_history=True,
                               preserve_action_holds=True, review_admission=admission):
            raise ValueError("Bound blocked review changed before continuation")
    else:
        kb.resume_review_triage(
            conn, task["id"], expected_task_sha256=goal_snapshot_sha(task),
            expected_goal_sha256=goal_snapshot_sha(goal), expected_event_id=stop["id"],
            expected_event_sha256=goal_snapshot_sha(stop), expected_run_sha256=goal_snapshot_sha(run),
            admission=admission, author=actor.strip(), reason=reason.strip(),
        )
    return {"review_loop_count": goal["review_loop_count"], "state_version": goal["current_state_version"]}


def recover_review(conn, task_id: str, *, expected_task_sha256: str,
                   expected_goal_sha256: str, expected_stop_event_id: int,
                   expected_latest_comment_id: int, evidence_comment_ids: list[int],
                   actor: str, reason: str, dry_run: bool = True) -> dict:
    """Restore one held review after its owner resolves the blocking condition.

    ``expected_task_sha256`` is ``kb.triage_snapshot_sha(task_row)``;
    ``expected_goal_sha256`` is ``goal_snapshot_sha(goal_row)``. The caller's
    trusted owner boundary must enforce board identity and private task custody.
    """
    from agent.private_worker_mode import get_authenticated_worker_contract
    from gateway.session_context import get_bound_session_env

    kb._assert_not_delegated_child_mutation()
    if (get_authenticated_worker_contract()
            or os.environ.get("HERMES_KANBAN_TASK")
            or os.environ.get("HERMES_KANBAN_RUN_ID")
            or get_bound_session_env("HERMES_KANBAN_TASK")
            or get_bound_session_env("HERMES_KANBAN_RUN_ID")
            or (get_bound_session_env("HERMES_SESSION_SOURCE")
                or os.environ.get("HERMES_SESSION_SOURCE")) in {"kanban", "cron"}):
        raise PermissionError("held review recovery is unavailable to workers or cron")
    if (type(dry_run) is not bool
            or not all(isinstance(x, str) and re.fullmatch(r"[a-f0-9]{64}", x)
                       for x in (expected_task_sha256, expected_goal_sha256))
            or type(expected_stop_event_id) is not int or expected_stop_event_id < 1
            or type(expected_latest_comment_id) is not int or expected_latest_comment_id < 1
            or not isinstance(evidence_comment_ids, list) or not evidence_comment_ids
            or len(evidence_comment_ids) > 20
            or any(type(x) is not int or x < 1 for x in evidence_comment_ids)
            or not isinstance(actor, str) or not actor.strip() or len(actor) > 100
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
        raise ValueError("exact review snapshots, evidence comments and bounded reason required")

    with kb.write_txn(conn, allow_nested=True):
        from hermes_cli.board_owner_manage import validate_bound_target
        validate_bound_target(conn, task_id, "recover_review")
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        goal = conn.execute("SELECT * FROM owner_cell_goals WHERE task_id=?", (task_id,)).fetchone()
        if (task is None or goal is None or task["status"] not in {"triage", "blocked"}
                or kb.triage_snapshot_sha(task) != expected_task_sha256
                or goal_snapshot_sha(goal) != expected_goal_sha256):
            raise ValueError("held review snapshot changed; inspect the card again")
        stop = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN "
            "('block_loop_detected','blocked','triaged','escalated') ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        latest = conn.execute("SELECT id FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1",
                              (task_id,)).fetchone()
        if (stop is None or stop["id"] != expected_stop_event_id
                or latest is None or latest["id"] != expected_latest_comment_id):
            raise ValueError("review stop or latest comment changed; inspect the card again")
        for comment_id in evidence_comment_ids:
            evidence = conn.execute("SELECT task_id,author FROM task_comments WHERE id=?",
                                    (comment_id,)).fetchone()
            if (evidence is None or evidence["task_id"] != task_id
                    or evidence["author"] != goal["required_reviewer_profile"]):
                raise ValueError("review evidence must be same-card comments by the bound reviewer")
        if not kb._parents_satisfied(conn, task_id):
            raise ValueError("review prerequisites remain unsatisfied")

        contract = json.loads(goal["contract_json"])
        conn.execute("SAVEPOINT preview_owner_review_recovery")
        try:
            # Preserve the historical missing-senior repair exactly. Other
            # reviewed maker/reviewer contracts use the established full-run
            # snapshot and admission-bound transition, never a generic unblock.
            if contract.get("owner_acceptance") and goal["senior_owner_profile"] is None:
                receipt = oc.recover_owner_acceptance_senior_review(
                    conn, task_id,
                    expected_contract_sha256=hashlib.sha256(goal["contract_json"].encode()).hexdigest(),
                    expected_state_version=goal["current_state_version"],
                    expected_review_loop_count=goal["review_loop_count"],
                    expected_task_status=task["status"],
                    expected_owner_profile=goal["owner_profile"],
                    expected_maker_profile=contract["runtime_profile"],
                    expected_reviewer_profile=goal["required_reviewer_profile"],
                    expected_review_artifact_ref=goal["review_artifact_ref"],
                    expected_review_artifact_digest=goal["review_artifact_digest"],
                    evidence_comment_ids=evidence_comment_ids,
                    actor=actor.strip(), reason=reason.strip(),
                )
            else:
                receipt = _recover_ordinary_review(conn, task, goal, stop, actor=actor, reason=reason)

        finally:
            if dry_run:
                conn.execute("ROLLBACK TO SAVEPOINT preview_owner_review_recovery")
            conn.execute("RELEASE SAVEPOINT preview_owner_review_recovery")
        return {
            "task_id": task_id, "action": "recover_review", "dry_run": dry_run,
            "status": task["status"] if dry_run else "review",
            "proposed_status": "review", "reviewer": goal["required_reviewer_profile"],
            "review_loop_count": receipt["review_loop_count"],
            "review_state_version": receipt["state_version"],
            "verdict_recorded": False, "dispatch_performed": False,
            "next_action": "The bound reviewer must claim the existing review and issue a fresh authenticated decision; prior findings and repair limits remain in force.",
        }


def recover_native_review(conn, task_id: str, *, actor: str, reason: str, dry_run: bool) -> dict:
    """Continue an ordinary native review under the already-bound work request.

    Native review_requested provenance replaces an owner-cell contract here;
    the existing maker output and reviewer identity remain unchanged. This is
    not an approval, a review verdict, or an automatic retry.
    """
    from hermes_cli import board_owner_manage as control
    from hermes_cli import owner_work_preparation as prep
    from hermes_cli.kanban_completion import _independent_review_binding

    with kb.write_txn(conn, allow_nested=True):
        bound = control._expected.get()
        member = control.require_owner()
        if (not bound or bound.get('action') != 'work' or bound.get('task_id') != task_id
                or actor != (member['principal'] if member else 'default')
                or type(dry_run) is not bool):
            raise PermissionError('Native review continuation requires a bound owner work request')
        control.validate_bound_target(conn, task_id, 'work')
        if member is not None:
            control._validate_member_connection(conn, member)
        board = prep._connection_board(conn, member)
        task = prep._task(conn, task_id)
        prep._idle(conn, task)
        if (task['status'] not in {'blocked', 'triage'} or prep._goal(conn, task_id) is not None
                or not kb._parents_satisfied(conn, task_id)):
            raise ValueError('Native review requires an idle held reviewer and satisfied prerequisites')
        binding = _independent_review_binding(conn, task_id, allow_held=True)
        stop = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN "
            "('blocked','block_loop_detected','triaged','escalated') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if binding is None or stop is None:
            raise ValueError('Native review needs its ended maker, review handoff, bound reviewer and recorded stop')
        if stop['run_id'] is not None:
            run = conn.execute('SELECT * FROM task_runs WHERE id=? AND task_id=?', (stop['run_id'], task_id)).fetchone()
            if (run is None or run['profile'] != binding['reviewer'] or run['ended_at'] is None
                    or run['outcome'] not in {'blocked','failed','spawn_failed','timed_out','crashed'}):
                raise ValueError('Native review stop must retain its ended unsuccessful reviewer run')
        projected = dataclasses.replace(kb.get_task(conn, task_id), status='review')
        try:
            admission = kb._build_worker_admission(projected, binding['reviewer'], board=board)
        except kb.WorkerAdmissionError as exc:
            if isinstance(exc.__cause__, oc.OwnerCellGuardError):
                raise ValueError('Reviewer readiness: ' + ' '.join(str(exc.__cause__).split())[:500]) from exc
            raise
        if control.require_owner() != member or prep._connection_board(conn, member) != board:
            raise PermissionError('Owner or board changed during reviewer admission')
        control.validate_bound_target(conn, task_id, 'work')
        actual_db = next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main')
        if (admission.contract.get('version') != 'hermes.worker-admission/v1'
                or admission.contract.get('status') == 'rejected'
                or admission.contract.get('task') != task_id
                or admission.contract.get('profile') != binding['reviewer']
                or admission.contract.get('board_database') != kb._database_identity_contract(actual_db)
                or hashlib.sha256(admission.encoded_contract.encode()).hexdigest() != admission.digest
                or not kb._admission_still_current(projected, admission)
                or _independent_review_binding(conn, task_id, allow_held=True) != binding):
            raise ValueError('Fresh exact accepted native reviewer admission required')
        if not dry_run:
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
            kb._append_event(conn, task_id, 'review_work_resumed', {
                'actor': actor, 'reason': reason, 'source_status': task['status'],
                'review_binding': binding, 'preserved_stop_event_id': stop['id'],
                'failure_history_preserved': True, 'action_restrictions_preserved': True,
                'approval_granted': False})
        return {'task_id': task_id, 'status': task['status'] if dry_run else 'review',
                'reviewer': binding['reviewer'], 'review_binding': binding,
                'verdict_recorded': False, 'dispatch_performed': False}


def reroute_native_review(conn, task_id: str, *, reviewer: str, actor: str,
                          reason: str, dry_run: bool) -> dict:
    """Change one idle native reviewer, preserving its ended maker and evidence."""
    from hermes_cli import board_owner_manage as control
    from hermes_cli import owner_work_preparation as prep
    from hermes_cli.kanban_completion import _independent_review_binding

    if (not isinstance(reviewer, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', reviewer)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
            or type(dry_run) is not bool):
        raise ValueError('An installed reviewer and bounded current work instruction are required')
    # This is an authenticated owner assignment, not worker admission. The
    # replacement cannot prove native ownership until this assignment commits.
    # Normal dispatch must admit it freshly before claim or execution.
    if conn.in_transaction:
        raise ValueError('Reviewer reassignment requires its own short board transaction')
    bound = control._expected.get()
    member = control.require_owner()
    if (not bound or bound.get('action') != 'work' or bound.get('task_id') != task_id
            or actor != (member['principal'] if member else 'default')):
        raise PermissionError('Review reassignment requires a bound authenticated owner work request')
    control.validate_bound_target(conn, task_id, 'work')
    if member is not None:
        control._validate_member_connection(conn, member)
    board = prep._connection_board(conn, member)
    task = prep._task(conn, task_id)
    if task['status'] != 'review' or prep._goal(conn, task_id) is not None:
        raise ValueError('Reviewer reassignment requires an idle ordinary native review; preserve bound owner-cell review contracts')
    # Reuse the existing idle check without changing its accepted lifecycle
    # states, and the same installed-worker/private-route/roster policy.
    prep._idle(conn, {**task, 'status': 'ready'})
    prep._eligible(conn, task, reviewer, member)
    if not kb._parents_satisfied(conn, task_id):
        raise ValueError('Review prerequisites remain unsatisfied')
    binding = _independent_review_binding(conn, task_id)
    if binding is None or binding.get('source') != 'native_review_requested':
        raise ValueError('Review reassignment needs the existing ended maker, evidence and coherent review handoff')
    if reviewer == binding['implementer']:
        raise ValueError('The original maker cannot provide its own independent review')
    if reviewer == binding['reviewer']:
        with kb.write_txn(conn):
            control.validate_bound_target(conn, task_id, 'work')
            if control.require_owner() != member:
                raise PermissionError('Owner changed during reviewer check')
            prep._eligible(conn, prep._task(conn, task_id), reviewer, member)
        return {'changed': False, 'dry_run': dry_run, 'status': 'review',
                'reviewer': reviewer, 'worker_started': False,
                'failure_history_preserved': True,
                'next_action': 'The installed reviewer is already queued for the existing maker output.'}
    handoff = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    with kb.write_txn(conn):
        if control.require_owner() != member or prep._connection_board(conn, member) != board:
            raise PermissionError('Owner or board changed during reviewer reassignment')
        control.validate_bound_target(conn, task_id, 'work')
        # Roster enrollment can change independently of the task transaction.
        task = prep._task(conn, task_id)
        prep._idle(conn, {**task, 'status': 'ready'})
        prep._eligible(conn, task, reviewer, member)
        if _independent_review_binding(conn, task_id) != binding:
            raise ValueError('Review reassignment requires unchanged maker evidence')
        if not dry_run:
            conn.execute('UPDATE tasks SET assignee=? WHERE id=?', (reviewer, task_id))
            payload = json.loads(handoff['payload'])
            kb._append_event(conn, task_id, 'review_requested', {
                **payload, 'reviewer': reviewer, 'previous_reviewer': binding['reviewer'],
                'prior_review_requested_event_id': handoff['id'],
                'owner_actor': actor, 'owner_reason': reason,
                'failure_history_preserved': True, 'action_restrictions_preserved': True,
                'approval_granted': False}, run_id=binding['producer_run_id'])
        return {'changed': not dry_run, 'dry_run': dry_run, 'status': 'review',
                'reviewer': reviewer, 'previous_reviewer': binding['reviewer'],
                'producer_run_id': binding['producer_run_id'],
                'failure_history_preserved': True, 'action_restrictions_preserved': True,
                'worker_started': False, 'verdict_recorded': False, 'worker_admission': 'pending_dispatch',
                'next_action': 'The newly assigned reviewer must pass normal fresh dispatch admission before claim or execution, using the same maker evidence and existing task budget.'}

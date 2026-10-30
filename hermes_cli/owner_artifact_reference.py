"""Owner-only correction of a redacted review reference, without releasing work."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time

from hermes_cli import kanban_db as kb, owner_cell as oc


def _artifact_identity(workspace: str, artifact_ref: str):
    """Read the exact workspace file via no-follow directory descriptors."""
    root, path = Path(workspace), Path(artifact_ref)
    if (not root.is_absolute() or not path.is_absolute()
            or '..' in root.parts or '..' in path.parts):
        raise ValueError('An absolute bound workspace file is required')
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError('Review artifact must remain within its bound task workspace') from exc
    if not relative.parts:
        raise ValueError('Review artifact must be a regular workspace file')
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        raise ValueError('Safe no-follow artifact validation is unavailable')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', directory_flags)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Review artifact must be a regular file')
            digest = hashlib.sha256()
            while chunk := os.read(file_fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(file_fd)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after):
                raise ValueError('Review artifact changed while validating its bytes')
            return digest.hexdigest(), identity(after)
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise ValueError('Review artifact path is unavailable or contains a symlink') from exc
    finally:
        os.close(fd)


def repair_reference(conn, task_id, *, expected_goal_sha256,
                     expected_artifact_ref, expected_artifact_digest,
                     artifact_ref, actor, reason, dry_run=True):
    """Correct only an unusable redacted reference to unchanged reviewed bytes.

    The native owner boundary, board grant and complete task/goal snapshot are
    mandatory. This is operator maintenance, never a reviewer impersonation.
    Task status, review phase, findings, role bindings and budgets stay intact.
    """
    from hermes_cli import board_owner_manage as owner
    from hermes_cli.owner_board_recovery import goal_snapshot_sha

    owner.require_owner()
    bound = owner._expected.get()
    if bound is None or bound.get('action') != 'repair_review_reference':
        raise PermissionError('A bound native owner reference-repair request is required')
    if (type(dry_run) is not bool
            or not isinstance(expected_goal_sha256, str)
            or not re.fullmatch(r'[a-f0-9]{64}', expected_goal_sha256)
            or not isinstance(expected_artifact_digest, str)
            or not re.fullmatch(r'[a-f0-9]{64}', expected_artifact_digest)
            or not isinstance(expected_artifact_ref, str)
            or not re.fullmatch(r'redacted://artifact/[A-Za-z0-9._-]+', expected_artifact_ref)
            or not isinstance(artifact_ref, str) or not artifact_ref.strip()
            or len(artifact_ref) > 4096):
        raise ValueError('Exact goal, redacted reference and unchanged artifact digest are required')

    with kb.write_txn(conn):
        owner.validate_bound_target(conn, task_id, 'repair_review_reference')
        task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        goal = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        if (task is None or goal is None or task['status'] not in {'triage', 'blocked'}
                or goal_snapshot_sha(goal) != expected_goal_sha256
                or goal['router_mode'] != 'reviewed' or goal['phase'] != 'review'
                or goal['review_artifact_ref'] != expected_artifact_ref
                or goal['review_artifact_digest'] != expected_artifact_digest
                or not task['workspace_path']
                or goal['reviewer_profile'] != goal['required_reviewer_profile']
                or task['reviewer_profile'] != goal['reviewer_profile']
                or task['assignee'] != goal['reviewer_profile']
                or task['current_state_version'] != goal['current_state_version']):
            raise ValueError('Held review, exact goal and artifact binding must remain unchanged')
        if (any(task[key] is not None for key in ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))
                or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
                or conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?',
                                (task_id, int(time.time()))).fetchone()):
            raise ValueError('Reference repair requires a held idle task')
        checked = _artifact_identity(task['workspace_path'], artifact_ref)
        if checked[0] != expected_artifact_digest:
            raise ValueError('Reference repair cannot change the bound review artifact digest')
        if not dry_run:
            oc.rebind_review_artifact(
                conn, task_id, reviewer_profile=goal['reviewer_profile'],
                state_version=goal['current_state_version'],
                expected_artifact_ref=expected_artifact_ref,
                expected_artifact_digest=expected_artifact_digest,
                artifact_ref=artifact_ref, artifact_digest=expected_artifact_digest,
            )
            kb._append_event(conn, task_id, 'review_artifact_reference_repaired', {
                'actor': actor, 'reason': reason, 'status_preserved': task['status'],
                'reviewer': goal['reviewer_profile'], 'state_version': goal['current_state_version'],
                'review_loop_count': goal['review_loop_count'],
                'old_reference_sha256': hashlib.sha256(expected_artifact_ref.encode()).hexdigest(),
                'new_reference_sha256': hashlib.sha256(artifact_ref.encode()).hexdigest(),
                'artifact_digest': expected_artifact_digest,
                'verdict_recorded': False, 'dispatch_enabled': False,
            })
        if _artifact_identity(task['workspace_path'], artifact_ref) != checked:
            raise ValueError('Review artifact changed before reference repair completed')
        return {'metadata_only': True, 'status_preserved': task['status'],
                'verdict_recorded': False, 'dispatch_enabled': False,
                'next_action': ('Preview validated; no reference or lifecycle state changed.' if dry_run else 'Reference corrected only; the card remains held. Inspect again before any separate review recovery.')}

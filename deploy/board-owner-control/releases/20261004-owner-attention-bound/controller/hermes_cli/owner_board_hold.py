"""Authenticated owner board holds, separate from worker failure accounting."""
import json
from hermes_cli import kanban_db as kb
from hermes_cli import board_owner_manage as control

KINDS = frozenset({'needs_input', 'capability', 'transient'})


def _latest_stop(conn, task_id):
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind IN "
        "('blocked','block_loop_detected','triaged','escalated','dependency_wait','archived','restored','changes_requested') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row['payload'])
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError):
        return None


def _review_binding(conn, task_id):
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    # Holding a queue changes only these two fields. Everything that selected
    # the original reviewer and review evidence remains exactly bound.
    for key in ('status', 'block_kind'):
        task.pop(key, None)
    goal = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    handoff = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return control._hash({'task': task, 'goal': dict(goal) if goal else None,
                          'handoff': dict(handoff) if handoff else None})


def resume_queued_review(conn, task_id):
    """An unchanged owner-paused queue needs no fabricated reviewer findings.

    This is not recovery of a worker's failed review: only a hold placed while
    that same independent review was already queued can match this receipt.
    The caller retains normal work-order budgeting, dependencies and admission.
    """
    bound = control._expected.get()
    if not bound or bound.get('action') != 'work' or bound.get('task_id') != task_id:
        return False
    member = control.require_owner()
    if member is not None:
        control._validate_member_connection(conn, member)
    task = kb.get_task(conn, task_id)
    stop = _latest_stop(conn, task_id)
    if not task or task.status != 'blocked' or not stop or stop.get('owner_board_hold') is not True:
        return False
    if not stop.get('resume_queued_review') or stop.get('source_status') != 'review':
        return False
    if stop.get('review_binding') != _review_binding(conn, task_id):
        raise ValueError('The held review changed; inspect its current reviewer and evidence before resuming')
    control.validate_bound_target(conn, task_id, 'work')
    return True


def hold(conn, task_id, *, reason, kind, dry_run=False):
    member = control.require_owner()
    bound = control._expected.get()
    if not bound or bound.get('action') != 'block' or bound.get('task_id') != task_id:
        raise PermissionError('A bound authenticated owner hold request is required')
    if kind not in KINDS:
        raise ValueError('block requires kind=needs_input, capability or transient; use actual prerequisite links for dependency waits')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or type(dry_run) is not bool:
        raise ValueError('A concise concrete wait and what resolves it are required')
    if member is not None:
        control._validate_member_connection(conn, member)
    actor = member['principal'] if member else 'default'
    with kb.write_txn(conn):
        control.validate_bound_target(conn, task_id, 'block')
        if control.require_owner() != member:
            raise PermissionError('Owner identity changed')
        task = kb.get_task(conn, task_id)
        if task.status not in {'triage', 'todo', 'ready', 'review', 'blocked'}:
            raise ValueError('Only idle unfinished cards may be held; restore completed/archived work separately')
        goal = conn.execute('SELECT phase,owner_profile FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        source = 'review' if task.status == 'review' or (goal and goal['phase'] == 'review') else kb._resume_status_from_events(conn, task_id)
        prior = _latest_stop(conn, task_id)
        binding = _review_binding(conn, task_id) if source == 'review' else None
        queued_review = bool(task.status == 'review' or (task.status == 'blocked' and prior
            and prior.get('owner_board_hold') is True and prior.get('resume_queued_review') is True
            and prior.get('review_binding') == binding))
        payload = {'reason': reason.strip(), 'kind': kind, 'source_status': source,
                   'previous_status': task.status, 'actor': actor, 'owner_profile': (goal['owner_profile'] if goal else None) or actor,
                   'owner_board_hold': True, 'approval_granted': False,
                   'requester': member['principal'] if member else 'chris',
                   'owner_attention_requested': kind in {'needs_input', 'capability'},
                   'failure_history_preserved': True, 'action_restrictions_preserved': True,
                   'resume_queued_review': queued_review, 'review_binding': binding}
        if not dry_run:
            conn.execute("UPDATE tasks SET status='blocked',block_kind=? WHERE id=?", (kind, task_id))
            kb._append_event(conn, task_id, 'blocked', payload)
        return {'changed': not dry_run, 'dry_run': dry_run, 'status': task.status if dry_run else 'blocked',
                'proposed_status': 'blocked', 'block_kind': kind, 'reason': reason.strip(),
                'source_status': source, 'worker_started': False, 'failure_history_preserved': True,
                'action_restrictions_preserved': True, 'approval_granted': False,
                'next_action': 'Keep ownership of this card. Resolve or follow up on the recorded wait: '
                    + reason.strip() + '. When it is resolved or the owner provides a changed instruction, '
                    'use action=work on this same card; retain its assigned worker/reviewer, real dependencies and external-action restrictions.'}

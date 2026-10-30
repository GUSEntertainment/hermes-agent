"""Native owner board operations, using the installed guarded controller.

No authority is accepted from tool arguments. The gateway checks its actual
caller before crossing the process boundary; the controller checks again.
"""
import hashlib
import json
import os
import re
import sys
import time
from argparse import Namespace
from contextvars import ContextVar
from pathlib import Path

if __name__ == '__main__':
    sys.modules['hermes_cli.board_owner_manage'] = sys.modules[__name__]

_expected = ContextVar('owner_board_expected_state', default=None)
ACTIONS = ('work', 'block', 'prepare', 'archive', 'restore', 'start_triage', 'resume_triage', 'unblock', 'unlink_dependency', 'recover_review', 'repair_review_reference')


# Set only after the controller verifies a signed gateway request.
_member_context = ContextVar('verified_member_board_controller', default=None)
_administrator_context = ContextVar('verified_administrator_board_controller', default=None)
ROOT = Path('/Users/Chris/.hermes')


def refresh_member_grant(grant):
    """Re-resolve protected enrollment, never model-supplied authority."""
    if not isinstance(grant, dict) or set(grant) != {'principal', 'profile', 'board', 'fingerprint'}:
        raise PermissionError('Invalid member board grant')
    principal, profile = grant['principal'], grant['profile']
    if any(not isinstance(v, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,99}', v) for v in (principal, profile)):
        raise PermissionError('Canonical member identity required')
    from gateway.app_business_authority import canonical_enrollment, current
    if profile == principal:
        value = canonical_enrollment(principal, ROOT)
        if value.get('owner') != principal or value.get('profile') != principal:
            raise PermissionError('Member enrollment mismatch')
    else:
        from gateway.app_private_board_authority import private_profile
        if private_profile(profile):
            from gateway.app_private_board_authority import current
        value = current(profile, principal, root=ROOT)
    return dict(principal=principal, profile=profile, board=value['board'], fingerprint=value['fingerprint'])


def require_owner():
    """Main operator or currently authenticated member; workers never inherit it."""
    from agent.delegation_context import is_delegated_child_context
    from gateway.josh_board_authority import restricted, app_binding, _execution
    from gateway.member_worker_policy import authenticated_private_policy
    from hermes_constants import get_hermes_home
    from gateway.session_context import get_bound_session_env
    if (is_delegated_child_context() or os.environ.get('HERMES_KANBAN_TASK')
            or os.environ.get('HERMES_SESSION_SOURCE') in ('kanban', 'cron')
            or get_bound_session_env('HERMES_SESSION_SOURCE') in ('kanban', 'cron')
            or authenticated_private_policy() is not None):
        raise PermissionError('Board management requires the requesting member, not a worker or scheduled task')
    administrative = _administrator_context.get()
    if administrative is not None:
        grant, board = administrative
        administrator_board(grant, board)
        return None
    transported = _member_context.get()
    if transported is not None:
        if refresh_member_grant(transported) != transported:
            raise PermissionError('Member enrollment changed')
        return dict(transported)
    scope = _execution.get()
    if scope is not None:
        profile, principal = scope
        bound = app_binding()
        if bound is not None and bound.get('scope_contract') == 'margot-owner-private-v1':
            if bound['principal'] != principal:
                raise PermissionError('Authenticated private-app owner required')
            return refresh_member_grant(dict(principal=principal, profile=bound['profile'], board='', fingerprint=''))
        if principal == 'chris' and profile in (None, '', 'default', 'chris'):
            return None
        if not principal or not profile:
            raise PermissionError('Authenticated member identity required')
        # _execution is bound by the authenticated server, not environment.
        if profile != principal:
            bound = app_binding()
            if bound is None or bound['principal'] != principal or bound['profile'] != profile:
                raise PermissionError('Authenticated app binding required')
        return refresh_member_grant(dict(principal=principal, profile=profile, board='', fingerprint=''))
    home = Path(get_hermes_home())
    if (get_bound_session_env('HERMES_SESSION_PROFILE') not in ('', 'default')
            or os.environ.get('HERMES_PROFILE', 'default') not in ('', 'default')
            or home.parent.name == 'profiles' or restricted() or app_binding() is not None):
        raise PermissionError('Authenticated member request required; a profile name alone grants no board control')
    return None


def administrator_board(grant, requested):
    """Explicit maintenance selection, freshly checked against Chris's admin role."""
    if requested in (None, '', grant['board']):
        return None
    if (grant.get('principal') != 'chris' or grant.get('profile') not in
            {'chris', 'app-owner-chris-margot', 'app-gus-chris-margot'}):
        raise PermissionError('Board outside authenticated member ownership; enrolled board required')
    if refresh_member_grant(grant) != grant:
        raise PermissionError('Administrator enrollment changed')
    from gateway.app_business_authority import canonical_enrollment, protected_bytes, protected
    administrator = canonical_enrollment('chris', ROOT)
    if administrator['owner'] != 'chris' or administrator['board'] != grant['board']:
        raise PermissionError('Current system administrator required')
    if not isinstance(requested, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', requested):
        raise PermissionError('Enrolled maintenance board required')
    catalog = json.loads(protected_bytes(ROOT/'private/phone-assistant/caller-profiles.json', True, ROOT))
    owners = [row.get('profile_key') for row in catalog['profiles']
              if row.get('board') == requested and row.get('enabled') is not False]
    if len(owners) != 1 or not owners[0]:
        raise PermissionError('Unique current maintenance board owner required')
    target = canonical_enrollment(owners[0], ROOT)
    if target['owner'] != owners[0] or target['board'] != requested:
        raise PermissionError('Maintenance board enrollment changed')
    from hermes_cli import kanban_db as kb
    expected = ROOT/'kanban/boards'/requested/'kanban.db'
    if kb.kanban_db_path(board=requested) != expected:
        raise PermissionError('Administrator database override denied')
    protected(expected, ROOT)
    return requested


def requested_board(grant, requested=None):
    return administrator_board(grant, requested) or member_board(grant, requested)


def member_controller_scope():
    """For the guarded CLI only; ordinary named-profile CLI remains denied."""
    return require_owner() if _member_context.get() is not None else None


def member_board(grant, requested=None):
    if requested is not None and requested != grant['board']:
        raise PermissionError('Board outside authenticated member ownership')
    from hermes_cli import kanban_db as kb
    expected = ROOT / 'kanban' / 'boards' / grant['board'] / 'kanban.db'
    if kb.kanban_db_path(board=grant['board']) != expected:
        raise PermissionError('Member board database override denied')
    from gateway.app_business_authority import protected
    protected(expected, ROOT)
    return grant['board']


def _validate_member_connection(conn, grant):
    expected = ROOT / 'kanban' / 'boards' / grant['board'] / 'kanban.db'
    actual = [Path(r[2]) for r in conn.execute('PRAGMA database_list') if r[1] == 'main']
    if actual != [expected]:
        raise PermissionError('Member operation outside enrolled board')
    from gateway.app_business_authority import protected
    protected(expected, ROOT)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def describe(conn, task_id):
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        raise ValueError('Unknown task in the selected board')
    goal = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    stop = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected','triaged','escalated') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    event = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
    comment = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_comments WHERE task_id=?', (task_id,)).fetchone()[0]
    parents = [dict(x) for x in conn.execute('SELECT t.* FROM task_links l JOIN tasks t ON t.id=l.parent_id WHERE l.child_id=? ORDER BY t.id', (task_id,))]
    data = dict(task=dict(task), goal=dict(goal) if goal else None, event=event, comment=comment, parents=parents)
    fields = {k: task[k] for k in ('id', 'title', 'body', 'assignee', 'status', 'tenant')}
    return dict(expected_state=_hash(data), expected_task_sha256=_hash(fields),
                expected_goal_sha256=_hash(dict(goal)) if goal else None,
                expected_artifact_ref=goal['review_artifact_ref'] if goal else None,
                expected_artifact_digest=goal['review_artifact_digest'] if goal else None,
                expected_stop_event_id=stop[0] if stop else None, expected_latest_comment_id=comment,
                initial_triage=bool(task['status'] == 'triage' and stop is None and goal is None
                    and not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (task_id,)).fetchone()))


def validate_bound_target(conn, target, operation):
    """Called inside each existing mutation's write transaction."""
    bound = _expected.get()
    member = _member_context.get()
    if member is not None:
        current = require_owner()
        _validate_member_connection(conn, current)
        if bound is None:
            raise PermissionError('Scoped member operation requires a bound target')
    if bound is None:
        return
    task_id = target[1] if isinstance(target, tuple) else target
    if task_id != bound['task_id'] or describe(conn, task_id)['expected_state'] != bound['expected_state']:
        raise ValueError('Board state changed; inspect the card again before acting')
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if (any(task[k] is not None for k in ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))
            or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
            or conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?', (task_id, int(time.time()))).fetchone()):
        raise ValueError('Task has active work; preserve its current run and inspect it before cleanup')
    if operation == 'specify':
        if not describe(conn, task_id)['initial_triage'] or not task['assignee'] or not (task['body'] or '').strip():
            raise ValueError('start_triage requires a fully specified new card with no prior stop, run or review; use the appropriate recovery route for stopped work')
        evidence = bound.get('evidence') or {}
        comment_id = evidence.get('authorization_comment_id')
        if type(comment_id) is not int:
            raise ValueError('A same-card authorization comment is required to start new triage work')
        comment = conn.execute('SELECT body FROM task_comments WHERE id=? AND task_id=?', (comment_id, task_id)).fetchone()
        from gateway.gus_admin_cli import authorization_text_ok
        if comment is None or not authorization_text_ok(comment['body']):
            raise ValueError('The cited comment must record the existing authorization to proceed')


def execute(args):
    member = require_owner()
    if member is not None and _member_context.get() is None:
        raise PermissionError('Member mutations require the signed controller request')
    allowed = {'action', 'task_id', 'reason', 'expected_state', 'dry_run', 'board', 'parent_id', 'evidence', 'plan', 'instruction', 'kind'}
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError('Unsupported board operation fields')
    from gateway.gus_admin_cli import admission
    from gateway.josh_board_authority import admitted_board
    from hermes_cli import kanban_db as kb
    action = args.get('action')
    if action not in ACTIONS:
        raise ValueError('Unknown board management action')
    task_id = args.get('task_id', '')
    reason = args.get('reason', '')
    if not re.fullmatch(r't_[A-Za-z0-9_]+', task_id) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Exact task id and a concise disposition/resolution reason required')
    if action not in ('work', 'block') and not re.fullmatch(r'[a-f0-9]{64}', str(args.get('expected_state', ''))):
        raise ValueError('Use expected_state from the latest kanban_show board_administration.native fields')
    dry = args.get('dry_run', False)
    if type(dry) is not bool:
        raise ValueError('dry_run must be boolean')
    if dry and action not in ('work', 'block', 'prepare', 'start_triage', 'resume_triage', 'recover_review', 'repair_review_reference'):
        raise ValueError('Dry run is supported for triage and review operations; other operations are reversible')
    board = member_board(member, args.get('board')) if member is not None else admitted_board(args.get('board'))
    actor = member['principal'] if member is not None else 'default'
    if action == 'work':
        from hermes_cli.owner_work_start import work
        with kb.connect_closing(board=board) as conn:
            from hermes_cli.owner_work_preparation import _connection_board
            board = _connection_board(conn, member)
            fresh = describe(conn, task_id)
            if args.get('expected_state') is not None and args['expected_state'] != fresh['expected_state']:
                raise ValueError('Board state changed; repeat the current work request')
            bound = {**args, 'expected_state': fresh['expected_state']}
            token = _expected.set(bound)
            try:
                # Reuse the guarded owner continuation boundary. The work
                # helper binds its own exact operation and transaction state.
                proof = Namespace(kanban_action='resume-triage', owner_continuation=True, board=board)
                with admission(proof):
                    receipt = work(conn, task_id, args.get('instruction') or reason,
                                   actor=actor, member=member, board=board,
                                   plan=args.get('plan'), dry_run=dry)
                return dict(ok=True, action=action, task_id=task_id, board=board,
                            native=describe(conn, task_id), **receipt)
            finally:
                _expected.reset(token)
    if action == 'block':
        from hermes_cli.owner_board_hold import hold
        with kb.connect_closing(board=board) as conn:
            fresh = describe(conn, task_id)
            if args.get('expected_state') is not None and args['expected_state'] != fresh['expected_state']:
                raise ValueError('Board state changed; repeat the current hold request')
            token = _expected.set({**args, 'expected_state': fresh['expected_state']})
            try:
                with admission(Namespace(kanban_action='block', board=board)):
                    receipt = hold(conn, task_id, reason=reason, kind=args.get('kind'), dry_run=dry)
                return dict(ok=True, action=action, task_id=task_id, board=board,
                            native=describe(conn, task_id), **receipt)
            finally:
                _expected.reset(token)
    cli_action = {'start_triage': 'specify', 'resume_triage': 'resume-triage', 'unlink_dependency': 'unlink'}.get(action, action)
    proof = Namespace(kanban_action=cli_action, owner_continuation=action == 'resume_triage', board=board)
    token = _expected.set(args)
    try:
        with admission(proof), kb.connect_closing(board=board) as conn:
            validate_bound_target(conn, task_id, cli_action)
            if action == 'prepare':
                from hermes_cli.owner_work_preparation import prepare
                prepared = prepare(conn, task_id, args.get('plan'), actor=actor, member=member, reason=reason, dry_run=dry)
                return dict(ok=True, action=action, task_id=task_id, board=board, status=kb.get_task(conn, task_id).status, dry_run=dry, preparation=prepared, native=describe(conn, task_id), next_action='Inspect preparation readiness and existing holds; resume separately only after the blocking condition is resolved')
            elif action == 'archive':
                changed = kb.archive_task(conn, task_id, reason=reason)
            elif action == 'restore':
                changed = kb.restore_task(conn, task_id, reason=reason) is not None
            elif action == 'unblock':
                changed = kb.unblock_task(conn, task_id, preserve_failure_history=True)
                if not changed:
                    state = kb.get_task(conn, task_id).status
                    raise ValueError(f'No unblock transition occurred: task is {state}. Inspect kanban_show; use start_triage for a new specified card or resume_triage for a stopped card')
            elif action == 'start_triage':
                if dry:
                    changed = False
                else:
                    changed = kb.specify_triage_task(conn, task_id, author=actor)
                    if not changed:
                        raise ValueError('Task is no longer in initial triage')
                    kb.recompute_ready(conn)
            elif action == 'unlink_dependency':
                parent = args.get('parent_id', '')
                if not re.fullmatch(r't_[A-Za-z0-9_]+', parent):
                    raise ValueError('Exact obsolete prerequisite parent_id required')
                changed = kb.unlink_tasks(conn, parent, task_id)
            elif action == 'resume_triage':
                evidence = args.get('evidence') or {}
                kb.resume_triage_task(conn, task_id, author=actor, reason=reason, dry_run=dry,
                    expected_stop_event_id=evidence.get('expected_stop_event_id'),
                    authorization_comment_id=evidence.get('authorization_comment_id'),
                    expected_latest_comment_id=evidence.get('expected_latest_comment_id'),
                    expected_task_sha256=evidence.get('expected_task_sha256'))
                changed = not dry
            elif action == 'repair_review_reference':
                from hermes_cli.owner_artifact_reference import repair_reference
                metadata_receipt = repair_reference(conn, task_id, actor=actor, reason=reason, dry_run=dry,
                                                    **(args.get('evidence') or {}))
                changed = not dry
            else:
                from hermes_cli.owner_board_recovery import recover_review
                result = recover_review(conn, task_id, actor=actor, reason=reason, dry_run=dry,
                                        **(args.get('evidence') or {}))
                changed = not dry
            task = kb.get_task(conn, task_id)
            result = dict(ok=True, action=action, task_id=task_id, board=board,
                          changed=bool(changed), dry_run=dry, status=task.status,
                          failure_history_preserved=True)
            if action == 'recover_review':
                result['next_action'] = 'Fresh decision by the bound reviewer; recovery is not review approval'
            if action == 'repair_review_reference':
                result.update(metadata_receipt)
            return result
    finally:
        _expected.reset(token)


def main():
    try:
        payload = sys.stdin.read(32001)
        if len(payload) > 32000:
            raise ValueError('Board operation payload too large')
        request = json.loads(payload)
        if isinstance(request, dict) and 'signature' in request:
            from gateway.board_control_transport import verify
            args, grant = verify(request)
            board = administrator_board(grant, args.get('board'))
            context = _administrator_context if board else _member_context
            token = context.set((grant, board) if board else grant)
            try:
                result = execute(args)
                if board:
                    result['administrator'] = grant['principal']
                    result['original_owner_preserved'] = True
                    if result.get('changed') and not result.get('dry_run'):
                        from hermes_cli import kanban_db as kb
                        with kb.connect_closing(board=board) as conn, kb.write_txn(conn):
                            administrator_board(grant, board)
                            kb._append_event(conn, args['task_id'], 'administrator_board_action',
                                {'actor': grant['principal'], 'source_profile': grant['profile'],
                                 'action': args['action'], 'reason': args['reason'],
                                 'original_owner_preserved': True})
            finally:
                context.reset(token)
        else:
            result = execute(request)
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc))))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

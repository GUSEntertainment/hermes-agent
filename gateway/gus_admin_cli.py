"""Bounded native-worker CLI fallback for operations absent from tool schemas.

An installed worker's live assignment and existing terminal configuration are
required. This does not grant terminal access, enroll members or mint review
permission. Context is scoped to one CLI dispatch and checked again under the
mutation's write transaction.
"""
import os
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_active = ContextVar('native_admin_cli', default=None)
READS = frozenset({'list', 'ls', 'show', 'runs', 'stats', 'log', 'context'})
MUTATIONS = frozenset({'archive', 'promote', 'unblock', 'restore', 'specify'})

# Genuinely protected actions (t_9ebe1c65). Only these stay a human decision.
# Everything else that stopped (technical access, stale dependency, unfinished
# peer check, obsolete hold) is ordinary maintenance the user already
# authorised by asking for the work, and must not be re-asked.
import re as _re
PROTECTED = _re.compile(
    r"capital|payment|purchase|spend|invoice|wire|refund|\bsign(ing|ature|ed)?\b(?![- ]in)|"
    r"publish|external send|send to|outbound call|legal fil|immigration|"
    r"credential|password|oauth|account (change|setup)|privacy|connor|"
    r"delete|deletion|restart|production deploy", _re.I)


def _last_block_reason(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind IN ('blocked','triaged','escalated') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row or not row['payload']:
        return ''
    try:
        return str(json.loads(row['payload']).get('reason') or '')
    except Exception:
        return str(row['payload'])


def classify_hold(conn, task):
    """Separate the four kinds of stop. Returns (kind, plain_reason).

    granted     - ordinary work the user already authorised; peers may resume
    technical   - access/tooling failure; peers may resume or retire it
    review      - an independent check is in flight; never self-cleared
    protected   - a genuinely new consequential user decision
    """
    reason = _last_block_reason(conn, task.id)
    text = f"{task.title or ''} {reason}"
    if task.status == 'review':
        return 'review', 'An independent check is in progress.'
    if PROTECTED.search(text):
        m = PROTECTED.search(text).group(0)
        return 'protected', f'Needs Chris to decide: this involves {m.lower()}.'
    if task.block_kind in ('capability', 'transient'):
        return 'technical', 'Stopped on a technical access or tooling failure, not missing consent.'
    return 'granted', 'Ordinary work already authorised by the original request.'


def _proof(args):
    from agent.delegation_context import is_delegated_child_process_context
    from gateway.gus_worker_authority import current
    from gateway.josh_board_authority import admitted_board, restricted
    if is_delegated_child_process_context():
        # t_043e2722: the worker's own terminal inherits the write-fence marker but also an
        # admin locator. Accept it only outside an in-process delegate child; current() and
        # _assignment() still re-prove profile, board, live run, pid and terminal admission.
        from agent.delegation_context import is_delegated_child_context, ADMIN_LOCATOR
        task = os.environ.get(ADMIN_LOCATOR, '').strip()
        if not task or is_delegated_child_context() or os.environ.get('HERMES_KANBAN_TASK', task) != task:
            raise PermissionError('Delegated children must return results to their parent')
        os.environ['HERMES_KANBAN_TASK'] = task
        os.environ.pop('HERMES_DELEGATED_CHILD_CONTEXT', None)
    worker = current()
    if worker is None:
        # Member sessions cannot use this administrative escape hatch.
        if restricted() or os.environ.get('HERMES_SESSION_SOURCE') == 'kanban':
            raise PermissionError('Native GUS administrative assignment required')
        return None
    if args.kanban_action == 'specify' and (getattr(args, 'all_triage', False) or not getattr(args, 'task_id', None)):
        # t_1cc0200c: one named triage card at a time; never a blanket sweep.
        raise PermissionError('Administrative specify takes exactly one task id, never --all')
    board = admitted_board(getattr(args, 'board', None))
    if board != worker['board']:
        raise PermissionError('Administrative CLI board mismatch')
    import yaml
    from hermes_constants import get_hermes_home
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import resolve_toolset
    config = yaml.safe_load((Path(get_hermes_home()) / 'config.yaml').read_text())
    enabled = _get_platform_tools(config, 'cli', include_default_mcp_servers=False)
    disabled = config.get('agent', {}).get('disabled_toolsets', [])
    names = set().union(*(set(resolve_toolset(x)) for x in enabled if x not in disabled))
    if 'terminal' not in names:
        raise PermissionError('Existing native terminal capability required')
    action = args.kanban_action
    if action not in READS | MUTATIONS:
        raise PermissionError('Operation is outside the bounded administrative CLI; use native tools')
    if getattr(args, 'purge_ids', None) or getattr(args, 'force', False):
        raise PermissionError('Purge and forced transitions are unavailable to workers')
    if action in MUTATIONS and not str(getattr(args, 'reason', '') or '').strip():
        raise PermissionError('Administrative changes require --reason with recorded evidence')
    args.board = board
    if action == 'unblock':
        args.preserve_failure_history = True
    return worker


def _assignment(conn, worker):
    from hermes_cli import kanban_db as kb
    own = kb.get_task(conn, worker['task'])
    if (own is None or own.assignee != worker['profile'] or own.status != 'running'
            or not own.current_run_id or not own.worker_pid):
        raise PermissionError('Administrative native assignment is no longer running')
    run = conn.execute('SELECT profile,status,worker_admission_contract FROM task_runs WHERE id=? AND task_id=?', (own.current_run_id, own.id)).fetchone()
    contract = json.loads(run['worker_admission_contract'] or '{}') if run else {}
    available = contract.get('skill_dependencies', {}).get('tool_schema', {}).get('available_tools', [])
    if (run is None or run['profile'] != worker['profile'] or run['status'] != 'running'
            or contract.get('profile') != worker['profile'] or contract.get('task') != own.id
            or 'terminal' not in available):
        raise PermissionError('Current admitted worker must already expose terminal')
    # Phase-specific review never becomes administrative maker authority.
    row = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (own.id,)).fetchone()
    if row is not None and row['phase'] == 'review':
        raise PermissionError('Reviewer cannot mutate other tasks through administrative CLI')


@contextmanager
def admission(args):
    worker = _proof(args)
    if worker is not None:
        from hermes_cli import kanban_db as kb
        with kb.connect_closing() as conn:
            _assignment(conn, worker)
    token = _active.set(worker)
    try:
        yield
    finally:
        _active.reset(token)


def validate_target(conn, task_id, operation):
    """Must run within the supported mutator's write transaction."""
    worker = _active.get()
    if worker is None:
        return False
    from hermes_cli import kanban_db as kb
    _assignment(conn, worker)
    task = kb.get_task(conn, task_id)
    if task is None or task.id == worker['task']:
        raise PermissionError('Administrative CLI cannot change its own assignment or a missing task')
    # Self-clear prohibition: the profile doing the work never clears its own stop.
    if task.assignee and task.assignee == worker['profile'] and operation in {'unblock', 'promote'}:
        raise PermissionError('The assigned worker cannot clear its own stop; a different agent must')
    if operation == 'restore':
        if task.status != 'archived':
            raise PermissionError('Only archived tasks can be restored')
        return True
    if task.current_run_id or task.worker_pid or task.claim_lock:
        raise PermissionError('Active tasks cannot be changed by administrative cleanup')
    goal = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    # Only a review that is actually happening is protected. An idle triage
    # card that merely carries a review-phase goal is a stale projection.
    if task.status in {'review', 'running', 'scheduled'}:
        raise PermissionError('Preserve active review and scheduled holds')
    if goal and goal['phase'] == 'review' and operation != 'archive':
        raise PermissionError('Preserve active review and scheduled holds')
    kind, plain = classify_hold(conn, task)
    if task.status == 'blocked' and operation == 'unblock' and task.block_kind != 'dependency':
        # No approval laundering: protected decisions are never resumed by a peer.
        if kind == 'protected':
            raise PermissionError(plain)
    if operation == 'archive':
        # Archive is reversible removal from active view, never completion.
        # Protected obligations stay visible until Chris decides them.
        if kind == 'protected' and task.status == 'blocked':
            raise PermissionError(plain + ' It stays visible until then.')
        if task.status not in {'triage', 'todo', 'ready', 'done', 'blocked'}:
            raise PermissionError('Task is not eligible for reversible archival')
        for child_id in kb.child_ids(conn, task_id):
            child = kb.get_task(conn, child_id)
            if child and child.status not in {'done', 'archived'}:
                raise PermissionError('Archival would change a live dependency; preserve it')
    elif operation == 'specify':
        # Guarded triage -> todo. Status-only; dependency gating stays with
        # recompute_ready, so open parents keep the card in todo.
        if task.status != 'triage':
            raise PermissionError('Only triage cards can be specified')
        if task.assignee and task.assignee == worker['profile']:
            raise PermissionError('The assigned worker cannot clear its own stop; a different agent must')
        if kind == 'protected':
            raise PermissionError(plain)
    elif operation in {'promote', 'unblock'}:
        if task.status == 'triage' and operation == 'unblock':
            raise PermissionError('Use promote for triage cards')
        if not kb._parents_satisfied(conn, task_id):
            raise PermissionError('Dependencies must be satisfied without force')
        if operation == 'unblock' and task.status != 'blocked':
            raise PermissionError('Only resolved dependency blocks can be resumed')
    else:
        raise PermissionError('Unsupported administrative mutation')
    return True


def active():
    return _active.get() is not None

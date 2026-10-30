"""Audited next-step preparation; never grants authority or starts a worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

EVENT = 'owner_work_prepared'
_BIND_FIELDS = ('id', 'title', 'body', 'assignee', 'skills', 'tenant', 'workspace_kind',
                'workspace_path', 'branch_name', 'project_id', 'owner_profile', 'runtime_profile', 'reviewer_profile',
                'security_realm', 'scope_fingerprint', 'router_mode', 'capability_contract',
                'model_override', 'provider_override', 'reasoning_effort', 'approval_policy',
                'worker_budget_json')
_BUDGET_RANGES = {'max_runs_per_day': (1, 4), 'max_attempts_per_input': (1, 1),
                  'worker_max_turns': (12, 64), 'worker_run_seconds': (60, 1800),
                  'worker_total_tokens': (32000, 2_000_000), 'worker_request_tokens': (8000, 128000)}


def select_toolsets(available, requirements):
    """Select a lean subset of the caller's already-authorized toolsets only."""
    def names(value, field):
        if (not isinstance(value, (list, tuple, set, frozenset))
                or any(not isinstance(item, str) or not item.strip() for item in value)):
            raise ValueError(field + ' must contain nonempty toolset names')
        return set(value)

    authorized = names(available, 'Available toolsets')
    if not isinstance(requirements, dict):
        raise ValueError('Prepared requirements must be an object')
    requested = names(requirements.get('toolsets', []), 'Prepared toolsets')
    mcp = requirements.get('mcp', {})
    if not isinstance(mcp, dict):
        raise ValueError('Prepared MCP requirements must map server names to operations')
    requested.update(names(list(mcp), 'Prepared MCP servers'))
    commands = requirements.get('commands', [])
    if (not isinstance(commands, list)
            or any(not isinstance(command, str) or not command.strip() for command in commands)):
        raise ValueError('Prepared commands must be a list of nonempty strings')
    for flag in ('terminal_network', 'localhost_server', 'browser_session'):
        if type(requirements.get(flag, False)) is not bool:
            raise ValueError('Prepared ' + flag + ' must be true or false')
    if commands or requirements.get('terminal_network') or requirements.get('localhost_server'):
        requested.add('terminal')
    if requirements.get('browser_session'):
        requested.add('browser')
    missing = requested - authorized
    if missing:
        raise ValueError('Prepared work needs unavailable toolsets: ' + ', '.join(sorted(missing))
                         + '. Use currently configured authorized toolsets or repair the worker profile before starting.')
    return sorted(requested | ({'kanban', 'skills'} & authorized))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode()).hexdigest()


def _task(conn, task_id):
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown task in the selected board')
    return dict(row)


def _goal(conn, task_id):
    row = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def _binding(conn, task_id):
    task = _task(conn, task_id)
    goal = _goal(conn, task_id)
    if goal:
        goal.pop('updated_at', None)
    links = [dict(row) for row in conn.execute(
        'SELECT parent_id,edge_role FROM task_links WHERE child_id=? ORDER BY parent_id,edge_role',
        (task_id,))]
    fields = {key: task.get(key) for key in _BIND_FIELDS}
    if (task.get('workspace_kind') or 'scratch') == 'scratch':
        from hermes_cli import kanban_db as kb
        planned = kb._planned_workspace_contract(kb.get_task(conn, task_id), _connection_board(conn, None))
        fields['workspace_path'] = str(Path(planned['path']).resolve(strict=False))
    return _hash({'task': fields, 'goal': goal, 'parents': links})


def current(conn, task_id):
    """Return the latest plan only while its exact governing inputs still match."""
    row = conn.execute('SELECT id,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (task_id, EVENT)).fetchone()
    if row is None:
        return None
    try:
        record = json.loads(row['payload'])
        plan = record['plan']
        if (record.get('version') != 1 or not isinstance(plan, dict)
                or record.get('plan_digest') != _hash(plan)
                or record.get('binding_digest') != _binding(conn, task_id)):
            return None
        return {**plan, 'preparation_event_id': row['id'], 'plan_digest': record['plan_digest'],
                'readiness': record.get('readiness', {})}
    except (KeyError, TypeError, ValueError):
        return None



def capture_materialization(conn, task_id):
    """Capture a current plan inside native worktree persistence's existing CAS."""
    if not conn.in_transaction:
        raise ValueError('Native materialization capture requires the existing write transaction')
    plan = current(conn, task_id)
    if plan is None:
        return None
    row = conn.execute('SELECT id,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (task_id, EVENT)).fetchone()
    return {'task': _task(conn, task_id), 'goal': _goal(conn, task_id),
            'parents': [tuple(row) for row in conn.execute(
                'SELECT parent_id,edge_role FROM task_links WHERE child_id=? ORDER BY parent_id,edge_role', (task_id,))],
            'event_id': row['id'], 'record': json.loads(row['payload'])}


def reseal_materialization(conn, task_id, snapshot, *, resolved_path, resolved_branch):
    """Append the same brief binding only for the native CAS's exact materialization."""
    if snapshot is None:
        return False
    if not conn.in_transaction:
        raise ValueError('Native materialization reseal requires the existing write transaction')
    fresh = _task(conn, task_id)
    before = snapshot['task']
    if (before['id'] != task_id or before.get('workspace_kind') != 'worktree'
            or not isinstance(resolved_path, str) or not Path(resolved_path).is_absolute()
            or not isinstance(resolved_branch, str) or not resolved_branch):
        raise ValueError('Exact native worktree materialization is required')
    expected = {**before, 'workspace_path': resolved_path, 'branch_name': resolved_branch}
    row = conn.execute('SELECT id,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (task_id, EVENT)).fetchone()
    parents = [tuple(row) for row in conn.execute(
        'SELECT parent_id,edge_role FROM task_links WHERE child_id=? ORDER BY parent_id,edge_role', (task_id,))]
    if (fresh != expected or _goal(conn, task_id) != snapshot['goal'] or parents != snapshot['parents']
            or row is None or row['id'] != snapshot['event_id']
            or json.loads(row['payload']) != snapshot['record']):
        raise ValueError('Task or plan changed outside exact native worktree materialization')
    record = snapshot['record']
    if record['plan_digest'] != _hash(record['plan']):
        raise ValueError('Preparation digest changed during materialization')
    binding = _binding(conn, task_id)
    if binding == record['binding_digest']:
        return False
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, EVENT, {**record, 'binding_digest': binding,
        'materialization': {'source': 'native_worktree_preclaim', 'prior_event_id': row['id'],
                            'prior_binding_digest': record['binding_digest']}})
    return True


def _idle(conn, task):
    if task['status'] not in {'triage', 'blocked', 'todo', 'ready'}:
        raise ValueError('Preparation requires idle triage, blocked, todo or ready work')
    if (any(task.get(key) is not None for key in ('current_run_id', 'worker_pid', 'claim_lock', 'claim_expires'))
            or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task['id'],)).fetchone()
            or conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?',
                            (task['id'], int(time.time()))).fetchone()):
        raise ValueError('Task has active work; preparation must preserve its current run')


def _normalise(plan, task):
    from hermes_cli.worker_readiness import requirements
    if (not isinstance(plan, dict) or set(plan) - {'brief', 'acceptance', 'requirements', 'assignee', 'skills', 'budget'}
            or not {'brief', 'acceptance', 'requirements', 'budget'} <= set(plan)):
        raise ValueError('Provide brief, acceptance, requirements and budget only, plus optional assignee/skills')
    brief, acceptance = plan['brief'], plan['acceptance']
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 6000:
        raise ValueError('A nonempty next-step brief of at most 6000 characters is required')
    if (not isinstance(acceptance, list) or not acceptance or len(acceptance) > 32
            or any(not isinstance(item, str) or not item.strip() for item in acceptance)
            or sum(map(len, acceptance)) > 2000):
        raise ValueError('Acceptance must be concise nonempty strings totaling at most 2000 characters')
    needed = requirements('', '```worker-requirements\n' + json.dumps(plan['requirements']) + '\n```')
    needed.pop('source', None)
    budget = plan['budget']
    if not isinstance(budget, dict) or set(budget) != set(_BUDGET_RANGES):
        raise ValueError('Execution budget must contain all six supported completion fields')
    for key, (low, high) in _BUDGET_RANGES.items():
        if type(budget[key]) is not int or not low <= budget[key] <= high:
            raise ValueError('Execution budget field out of bounds: ' + key)
    if budget['worker_request_tokens'] > budget['worker_total_tokens']:
        raise ValueError('Request token budget must not exceed total token budget')
    from hermes_cli.profiles import normalize_profile_name
    worker = plan.get('assignee', task.get('assignee'))
    if not isinstance(worker, str) or not worker:
        raise ValueError('Choose an existing worker for this task')
    worker = normalize_profile_name(worker)
    skills = plan.get('skills')
    if skills is None:
        skills = json.loads(task.get('skills') or '[]')
    if (not isinstance(skills, list) or len(skills) > 32
            or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', item)
                   or '..' in item.split('/') for item in skills)):
        raise ValueError('Skills must be a bounded list of existing skill names')
    return {'brief': brief.strip(), 'acceptance': [s.strip() for s in acceptance],
            'requirements': needed, 'assignee': worker, 'skills': sorted(set(skills)),
            'budget': dict(budget)}


def _eligible(conn, task, worker, member):
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import profile_exists
    if not profile_exists(worker) or kb._worker_profile_uses_root_home(worker):
        raise ValueError('Choose an installed non-root worker profile')
    goal = _goal(conn, task['id'])
    if goal and goal.get('phase') == 'review':
        raise ValueError('This card holds a bound review; use recover_review with its reviewer findings')
    if worker == task.get('assignee'):
        return
    from gateway.private_action_worker_policy import classified_private
    if goal or classified_private(conn, task['id']) or task.get('runtime_profile') or task.get('reviewer_profile'):
        raise ValueError('Reassignment would change a bound maker/reviewer route; prepare the existing worker or repair that route explicitly')
    if member is not None:
        from gateway.owner_delegation import agents, agent
        matches = [entry for entry in agents(member['principal'])
                   if agent(entry['id'], member['principal'])['runtime'] == worker]
        if len(matches) != 1:
            raise PermissionError('Choose an active worker in this authenticated member\'s own roster')



def _connection_board(conn, member):
    """Select the open board, never an unrelated UI/default board."""
    from hermes_cli import kanban_db as kb, board_owner_manage as control
    rows = [row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main']
    if len(rows) != 1 or not rows[0]:
        raise PermissionError('Preparation requires a canonical file-backed board')
    actual = Path(rows[0]).resolve(strict=True)
    if member is not None:
        control._validate_member_connection(conn, member)
        return member['board']
    for root in (control.ROOT.resolve(), kb.kanban_home().resolve()):
        try:
            parts = actual.relative_to(root).parts
        except ValueError:
            continue
        if (len(parts) == 4 and parts[:2] == ('kanban', 'boards')
                and parts[3] == 'kanban.db' and kb._normalize_board_slug(parts[2]) == parts[2]):
            return parts[2]
    if actual == kb.kanban_db_path(board='default').resolve(strict=True):
        return 'default'
    raise PermissionError('Open database is not a canonical native board')


def _effective_budget(task, plan):
    from hermes_cli.kanban_completion import policy, task_limits
    sibling = json.loads(task.get('worker_budget_json') or '{}')
    if not isinstance(sibling, dict):
        raise ValueError('Existing worker budget must be an object')
    sibling['execution_budget'] = plan['budget']
    candidate = {**task, 'worker_budget_json': sibling}
    effective = task_limits(policy(), candidate)
    constraints = [{'field': key, 'requested': value, 'effective': effective[key],
                    'source': 'existing_operator_budget'}
                   for key, value in plan['budget'].items() if effective[key] < value]
    ceiling = task.get('max_runtime_seconds')
    if ceiling is not None:
        if type(ceiling) is not int or ceiling < 1:
            raise ValueError('Existing task runtime ceiling must be a positive integer')
        effective['max_runtime_seconds'] = ceiling
        if ceiling < effective['worker_run_seconds']:
            constraints.append({'field': 'worker_run_seconds',
                                'requested': plan['budget']['worker_run_seconds'],
                                'effective': ceiling, 'source': 'task.max_runtime_seconds'})
            effective['worker_run_seconds'] = ceiling
    return {'effective_budget': effective, 'budget_constraints': constraints,
            'budget_guidance': [
                'Total tokens accumulate input and output across calls; cached input tokens also count. '
                'Token usage is not a dollar-cost estimate.',
                'Turn, elapsed-time, per-request and total-token limits are independent; remaining turns '
                'do not guarantee remaining token allowance.',
                'Choose an explicit budget for the prepared work. Operator ceilings still apply; '
                'reaching a limit does not increase it or authorize another attempt.',
            ]}


def _readiness(conn, task, plan, member):
    """Offline profile/resource checks; never connect to MCP or a model provider."""
    import yaml
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_cli.worker_readiness import probe
    from gateway.member_worker_policy import select, effective
    board = _connection_board(conn, member)
    profile = get_profile_dir(plan['assignee'])
    config = yaml.safe_load((profile / 'config.yaml').read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError('Worker configuration must be a mapping')
    policy = select(task['id'], board, plan['assignee'])
    config = effective(config, policy)
    tools = sorted(_get_platform_tools(config, 'cli'))
    needed = plan['requirements']
    # The normal claim-time validator performs real connection/auth/schema and
    # provider checks. Inspection must not start MCP servers or bind sockets.
    local = {**needed, 'mcp': {}, 'localhost_server': False}
    workspace = task.get('workspace_path')
    if not workspace:
        workspace = kb._planned_workspace_contract(kb.get_task(conn, task['id']), board).get('path')
    if not workspace:
        raise ValueError('Task workspace must be resolved before preparation')
    result = probe(config, local, workspace, tools)
    result['requirements'] = needed
    deferred = []
    for server in needed['mcp']:
        entry = (config.get('mcp_servers') or {}).get(server)
        if not isinstance(entry, dict) or entry.get('enabled', True) is False or server not in tools:
            result['issues'].append({'code': 'missing_mcp_server', 'resource': server})
        else:
            deferred.append({'code': 'mcp_connection_auth_schema_check', 'resource': server})
    if needed['localhost_server']:
        if ((config.get('security') or {}).get('workspace_confinement') or {}).get('enabled') is True:
            result['issues'].append({'code': 'localhost_listen_denied', 'resource': 'terminal'})
        else:
            deferred.append({'code': 'localhost_bind_check', 'resource': 'terminal'})
    for skill in plan['skills']:
        path = profile / 'skills' / skill / 'SKILL.md'
        if not path.is_file():
            deferred.append({'code': 'skill_resolution_check', 'resource': skill})
    return {**result, 'offline_only': True, 'claim_time_admission_required': True,
            'deferred': deferred, 'requested_toolsets': needed['toolsets'],
            'local_checks_passed': not result['issues']}


def inspect(conn, task_id):
    from hermes_cli import kanban_db as kb
    task, goal = _task(conn, task_id), _goal(conn, task_id)
    plan = current(conn, task_id)
    exists = conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=? LIMIT 1', (task_id, EVENT)).fetchone()
    if goal and goal.get('phase') == 'review':
        action = 'recover_review'
    elif task['status'] == 'triage':
        from hermes_cli.board_owner_manage import describe
        action = 'start_triage' if describe(conn, task_id)['initial_triage'] else 'resume_triage'
    elif task['status'] == 'blocked':
        action = 'unblock'
    elif task['status'] in {'todo', 'ready'}:
        action = 'normal_dispatch'
    else:
        action = 'inspect_active_or_completed_work'
    dependencies_satisfied = kb._parents_satisfied(conn, task_id)
    if not dependencies_satisfied and task['status'] in {'triage', 'blocked', 'todo', 'ready'}:
        action = 'wait_for_dependencies'
    suggested = [action]
    if task['status'] in {'triage', 'blocked', 'todo', 'ready'} and not (goal and goal.get('phase') == 'review'):
        suggested.insert(0, 'prepare')
    return {'current': plan, 'stale': bool(exists and plan is None), 'next_action': action,
            'suggested_actions': suggested, 'dependencies_satisfied': dependencies_satisfied,
            'instruction': 'This is a next-step brief, not new authority. Preserve the original request, holds, review findings and normal worker admission.'}


def prepare(conn, task_id, plan, *, actor, member, reason, dry_run=False):
    from hermes_cli import kanban_db as kb, board_owner_manage as control
    if type(dry_run) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('A concise preparation reason and boolean dry_run are required')
    if control.require_owner() != member:
        raise PermissionError('Current owner identity changed')
    expected_actor = member['principal'] if member else 'default'
    if actor != expected_actor:
        raise PermissionError('Preparation actor must be the authenticated owner')
    task = _task(conn, task_id)
    _idle(conn, task)
    normalized = _normalise(plan, task)
    _eligible(conn, task, normalized['assignee'], member)
    before = control.describe(conn, task_id)['expected_state']
    readiness = _readiness(conn, task, normalized, member)
    budget_report = _effective_budget(task, normalized)
    readiness.update(budget_report)
    prior = current(conn, task_id)
    changed = prior is None or any(prior.get(key) != value for key, value in normalized.items())
    with kb.write_txn(conn):
        control.validate_bound_target(conn, task_id, 'prepare')
        if control.require_owner() != member or control.describe(conn, task_id)['expected_state'] != before:
            raise ValueError('Board state or owner identity changed; inspect before preparing again')
        fresh = _task(conn, task_id)
        _idle(conn, fresh)
        _eligible(conn, fresh, normalized['assignee'], member)
        if not dry_run and changed:
            budget = json.loads(fresh.get('worker_budget_json') or '{}')
            if not isinstance(budget, dict):
                raise ValueError('Existing worker budget must be an object')
            budget['execution_budget'] = normalized['budget']
            budget['preparation_digest'] = _hash(normalized)
            conn.execute('UPDATE tasks SET assignee=?,skills=?,worker_budget_json=? WHERE id=?',
                         (normalized['assignee'], json.dumps(normalized['skills']),
                          json.dumps(budget, sort_keys=True), task_id))
            kb._append_event(conn, task_id, EVENT, {'version': 1, 'actor': actor,
                'reason': reason.strip(), 'plan': normalized, 'plan_digest': _hash(normalized),
                'binding_digest': _binding(conn, task_id), 'readiness': readiness,
                'failure_history_preserved': True, 'status_preserved': fresh['status']})
    return {'task_id': task_id, 'changed': bool(changed and not dry_run), 'would_change': changed,
            'dry_run': dry_run, 'status': task['status'], 'plan': normalized, 'readiness': readiness,
            **budget_report,
            'failure_history_preserved': True, 'worker_started': False,
            'status_preserved': task['status'],
            'expected_state': control.describe(conn, task_id)['expected_state'],
            'next_action': inspect(conn, task_id)['next_action']}

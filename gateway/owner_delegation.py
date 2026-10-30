"""Enrolled-owner native delegation. Model intent never supplies routing authority.

Uses native Kanban creation/return subscriptions, owner-cell review and the
existing member worker sandbox. No shared-memory or company-board fallback.
"""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3

KIND = 'owner-delegation-return-v1'
ROOT = Path('/Users/Chris/.hermes')
_ACTIVE = ContextVar('owner_delegation', default=None)
TOOLS = {'own_work_create', 'own_agents_list', 'own_agent_propose', 'own_agent_confirm'}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def enrollment(owner):
    from gateway.app_business_authority import canonical_enrollment
    if type(owner) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', owner):
        raise PermissionError('Canonical private owner required')
    value = canonical_enrollment(owner, ROOT)
    if (value.get('owner') != owner or value.get('profile') != owner
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', str(value.get('board', '')))):
        raise PermissionError('Private owner enrollment changed')
    return value


def owner():
    return current()['principal']


def board():
    return current()['authority']['board']


def tenant(principal):
    return 'app:private:' + principal


def agent_profile_name(principal, identifier):
    if not re.fullmatch(r'oa_[a-f0-9]{32}', identifier):
        raise PermissionError('Invalid private agent identifier')
    # Preserve installed names and existing Josh task bindings without migration.
    if principal == 'joshgarcia':
        return 'josh-agent-' + identifier[3:]
    return 'owner-agent-' + digest([principal, identifier])[:32]


def _connect(path, *, write=False):
    from gateway.app_business_authority import protected
    path = Path(path)
    before = protected(path, ROOT)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if side.exists() or side.is_symlink():
            protected(side, ROOT)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    after = protected(path, ROOT)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        conn.close()
        raise PermissionError('Private database replaced')
    return conn


def database(board_name=None):
    board_name = board() if board_name is None else board_name
    if type(board_name) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', board_name):
        raise PermissionError('Canonical owner board required')
    return ROOT / 'kanban/boards' / board_name / 'kanban.db'


def agents(principal=None):
    """Same owner_agents table used by the member sidebar; absent means deny.

    Legacy contacts are seeded only by the app's authorized backend resolver.
    This tool never falls back to a global roster or seeds company agents.
    """
    principal = owner() if principal is None else principal
    enrollment(principal)
    with closing(_connect(ROOT / 'agent-os/owner-agents.sqlite')) as conn:
        rows = conn.execute('SELECT id,owner,name,purpose,archivedAt FROM owner_agents WHERE owner=? AND archivedAt IS NULL ORDER BY createdAt,id', (principal,)).fetchall()
    return [dict(r) for r in rows]


def agent(identifier, principal=None):
    principal = owner() if principal is None else principal
    authority = enrollment(principal)
    rows = [r for r in agents(principal) if r['id'] == identifier]
    if len(rows) != 1 or identifier in {'margot', 'default', principal, 'fable-review'}:
        raise PermissionError('Choose an active agent in your sidebar')
    row = rows[0]
    if re.fullmatch('oa_[a-f0-9]{32}', identifier):
        runtime = agent_profile_name(principal, identifier)
    elif re.fullmatch('[a-z][a-z0-9_-]{0,59}', identifier):
        runtime = identifier
    else:
        raise PermissionError('Invalid sidebar agent')
    from gateway.app_business_authority import protected_bytes
    raw = protected_bytes(ROOT / 'profiles' / runtime / 'config.yaml', anchor=ROOT)
    if identifier.startswith('oa_'):
        import yaml
        config = yaml.safe_load(raw) or {}
        if config.get('owner_agent') != {'owner': principal, 'id': identifier, 'board': authority['board']}:
            raise PermissionError('Private agent ownership changed')
    return dict(row, runtime=runtime)


@contextmanager
def bind(profile, principal, session, run, message, *, claim=None, revalidate=None):
    """Called only inside authenticated API dispatch, not exposed as a tool."""
    token = _ACTIVE.set(None)
    try:
        if profile in (None, '', 'default') or profile != principal:
            yield
            return
        from gateway.josh_board_authority import _execution, admitted_board
        authority = enrollment(principal)
        if _execution.get() != (profile, principal) or admitted_board(None) not in (None, authority['board']):
            raise PermissionError('Authenticated private owner required')
        if any(type(v) is not str or not v or len(v) > 200 for v in (session, run)):
            raise PermissionError('Native session and run required')
        if type(message) is not str or not message.strip() or len(message) > 32000:
            raise PermissionError('Bounded original request required')
        identity = None
        if claim is not None:
            identity = claim['identity']
            if claim['run'] != run or identity['session'] != session or claim['digest'] != hashlib.sha256(message.encode()).hexdigest():
                raise PermissionError('Original transport receipt mismatch')
            if identity['platform'] != 'photon' or identity['chat_type'] != 'dm' or identity['conversation'] != 'any;-;' + identity['sender']:
                raise PermissionError('Private original message required')
        _ACTIVE.set(dict(profile=profile, principal=principal, session=session, run=run,
                         message=message, identity=identity, authority=authority,
                         revalidate=revalidate))
        yield
    finally:
        _ACTIVE.reset(token)


def active():
    return _ACTIVE.get() is not None


def available():
    """Per-request catalog check; child or revoked contexts never gain tools."""
    try:
        current()
        return True
    except PermissionError:
        return False


def current():
    from gateway.josh_board_authority import _execution, admitted_board
    from agent.delegation_context import is_delegated_child_context
    value = _ACTIVE.get()
    if (value is None or _execution.get() != (value['principal'], value['principal'])
            or admitted_board(None) not in (None, value['authority']['board']) or is_delegated_child_context()):
        raise PermissionError('Authenticated private work context required')
    if value['authority'] != enrollment(value['principal']):
        raise PermissionError('Private enrollment changed')
    if value['revalidate'] is not None:
        value['revalidate']()
    return value


def return_binding(conn, *, session_id, request):
    if not active():
        return None
    ctx = current()
    principal, board_name = ctx['principal'], ctx['authority']['board']
    path = Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'))
    if path != database() or session_id != ctx['session'] or request.get('tenant') != tenant(principal):
        raise PermissionError('Private task return outside original scope')
    from gateway.session_context import get_bound_session_env as get
    identity = ctx['identity']
    if identity:
        target = dict(platform='photon', chat_id=identity['conversation'], thread_id=None,
                      user_id=identity['sender'], user_id_alt=None, chat_type='dm',
                      notifier_profile='default', delivery_mode='notify', delivery_metadata={})
        source_id = identity['message']
    else:
        if get('HERMES_SESSION_PLATFORM') != 'api_server' or get('HERMES_SESSION_ID') != ctx['session']:
            raise PermissionError('Original API session required')
        key = get('HERMES_SESSION_KEY')
        chat = get('HERMES_SESSION_CHAT_ID')
        if not key or not chat:
            raise PermissionError('Original API return target required')
        target = dict(platform='api_server', chat_id=chat, thread_id=None, user_id=principal,
                      user_id_alt=None, chat_type='dm', notifier_profile=principal,
                      delivery_mode='notify', delivery_metadata={'session_key': key})
        source_id = ctx['run']
    return dict(kind=KIND, profile=principal, principal=principal, board=board_name,
                session_id=ctx['session'], run_id=ctx['run'], source_id=source_id,
                source_sha256=hashlib.sha256(ctx['message'].encode()).hexdigest(),
                authority=ctx['authority'], target=target, request_sha256=digest(request))


def _contract(title, maker, reviewer, key, principal):
    return dict(goal_id='own-work-' + key[:24], objective=title,
                owner_profile=maker, owner_role_alias='private-owner-agent', runtime_profile=maker,
                required_reviewer_profile=reviewer, security_realm='owner:' + principal,
                router_mode='reviewed', risk_class='material', capability_bundle='owner-private-workspace',
                worker_budget={'producers': 1, 'reviewers': 1, 'extras': 0, 'max_depth': 1},
                targets=[], acceptance_test={'source': KIND, 'required': True},
                rollback={'procedure_ref': 'same-card-correction'},
                callback={'source_surface': KIND, 'return_surface': 'original-thread', 'report_policy': 'owner-only'},
                approval_gates=['external_send', 'spend', 'destructive', 'credentials', 'production'],
                delegation_roles={'maker': maker, 'reviewer': reviewer}, command_board_flow=True)


def create(args, **kwargs):
    ctx = current()
    principal, board_name = ctx['principal'], ctx['authority']['board']
    if type(args) is not dict or set(args) != {'title', 'request', 'agent'}:
        raise ValueError('Only title, request and sidebar agent are accepted')
    for key, limit in [('title', 140), ('request', 16000), ('agent', 64)]:
        if type(args[key]) is not str or not args[key].strip() or len(args[key]) > limit:
            raise ValueError('Bounded work intent required')
    selected = agent(args['agent'])
    reviewer = agent('grace' if selected['runtime'] != 'grace' else 'scarlett')
    source = ctx['identity']['message'] if ctx['identity'] else ctx['run']
    key = digest([principal, 'photon' if ctx['identity'] else 'app', source])
    # One substantive source request -> one native card, even after a tool retry.
    body = (f'[{tenant(principal)}]\nOwner-private delegated work. Never copy this task, its files or customer context into shared memory, boards or another member\'s data.\n\n'
            + 'Agent: ' + selected['name'] + '\nPurpose: ' + selected['purpose']
            + '\n\nOriginal request:\n' + ctx['message'] + '\n\nRequested result:\n' + args['request']
            + '\n\nUse only the admitted private workspace and explicitly authorized owner sources. Personal and company work belongs here; it is not refused for being outside GUS. Missing connected sources must be reported precisely, never replaced with host/Chris access.\n'
            + 'Produce and verify files in this workspace, attach them, then request the required native review. Return metadata.owner_cell_artifact with the real ref and digest. Native infrastructure returns the result in the original thread and exposes approved files only to the requester on their Desk; do not send separately. External sends, spend, credential changes and destructive actions still require their normal explicit approval.')
    from hermes_cli import kanban_db as kb
    with closing(_connect(database(), write=True)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS owner_delegated_work(task_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL)')
        conn.commit()
        with kb.write_txn(conn):
            old = conn.execute('SELECT * FROM owner_delegated_work WHERE source_key=?', (key,)).fetchone()
            if old:
                record = json.loads(old['record_json'])
                if record['intent'] != args or record['source_sha256'] != hashlib.sha256(ctx['message'].encode()).hexdigest():
                    raise PermissionError('Work request replay conflict')
                verify_task(conn, old['task_id'])
                return json.dumps({'accepted': True, 'task_id': old['task_id'], 'board': board_name, 'duplicate': True})
            workspace = ROOT / 'kanban/boards' / board_name / 'workspaces' / ('own-' + key)
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            if workspace.resolve() != workspace:
                raise PermissionError('Private workspace symlink')
            contract = _contract(args['title'], selected['runtime'], reviewer['runtime'], key, principal)
            current()
            task_id = kb.create_task(conn, title=args['title'], body=body, assignee=selected['runtime'],
                        created_by=principal, tenant=tenant(principal), board=board_name, workspace_kind='dir', workspace_path=str(workspace),
                        initial_status='running', session_id=ctx['session'], idempotency_key='owner-work-' + key,
                        owner_cell_contract=contract)
            record = dict(intent=args, selected=selected, reviewer=reviewer, authority=ctx['authority'],
                          source_sha256=hashlib.sha256(ctx['message'].encode()).hexdigest(), body_sha256=digest(body),
                          workspace=str(workspace), contract=contract)
            conn.execute('INSERT INTO owner_delegated_work VALUES(?,?,?)', (task_id, key, encode(record)))
            current()
            verify_task(conn, task_id)
        from gateway.native_task_receipts import record_created
        recorded = record_created(conn, kb.get_task(conn, task_id))
    return json.dumps({'accepted': True, 'task_id': task_id, 'board': board_name, 'duplicate': False, 'receipt_recorded': recorded})


def verify_task(conn, task_id):
    row = conn.execute('SELECT record_json FROM owner_delegated_work WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise PermissionError('Private delegation source missing')
    value = json.loads(row[0])
    principal = value['authority']['owner']
    authority = enrollment(principal)
    board_name = authority['board']
    actual = Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'))
    if actual != database(board_name):
        raise PermissionError('Private delegation board changed')
    if value['authority'] != authority or agent(value['selected']['id'], principal) != value['selected'] or agent(value['reviewer']['id'], principal) != value['reviewer']:
        raise PermissionError('Private delegation authority revoked')
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    goal = conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    binding_row = conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (task_id,)).fetchone()
    if not task or not goal or not binding_row:
        raise PermissionError('Private native source incomplete')
    binding = json.loads(binding_row[0])
    if (task['tenant'] != tenant(principal) or task['security_realm'] != 'owner:' + principal
            or task['workspace_kind'] != 'dir' or task['workspace_path'] != value['workspace']
            or digest(task['body']) != value['body_sha256'] or json.loads(goal['contract_json']) != value['contract']
            or binding.get('kind') != KIND or binding.get('authority') != value['authority']
            or binding.get('source_sha256') != value['source_sha256'] or binding.get('session_id') != task['session_id']):
        raise PermissionError('Private task binding changed')
    custody_authority(binding)
    return value, dict(task), dict(goal), binding


def worker_policy(task, board, worker):
    path = database(board)
    if not path.is_file():
        return None
    with closing(_connect(path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_delegated_work'").fetchone():
            return None
        if not conn.execute('SELECT 1 FROM owner_delegated_work WHERE task_id=?', (task,)).fetchone():
            return None
        value, native, goal, binding = verify_task(conn, task)
    principal, board_name = value['authority']['owner'], value['authority']['board']
    maker, reviewer = value['selected']['runtime'], value['reviewer']['runtime']
    if worker != native['assignee'] or worker not in {maker, reviewer}:
        raise PermissionError('Private worker assignment changed')
    if worker == reviewer and (goal['phase'] != 'review' or goal['reviewer_profile'] != reviewer):
        raise PermissionError('Private reviewer claim required')
    from gateway.member_worker_policy import PRIVATE_VERSION
    return dict(version=PRIVATE_VERSION, principal=principal, board=board_name, task=task, worker=worker,
                action='owner_work:' + task, authority_fingerprint=digest([value, binding]))


def custody_authority(binding):
    if binding.get('kind') != KIND:
        return None
    principal = binding.get('principal')
    auth = enrollment(principal)
    board_name = auth['board']
    if (binding.get('profile') != principal or binding.get('principal') != principal
            or binding.get('board') != board_name or binding.get('authority') != auth):
        raise PermissionError('Owner result custody revoked')
    target = binding.get('target', {})
    if target.get('platform') == 'api_server':
        if (target.get('notifier_profile') != principal or target.get('user_id') != principal
                or target.get('chat_id') != binding.get('session_id') or not target.get('delivery_metadata', {}).get('session_key')):
            raise PermissionError('Owner API return mismatch')
    elif target.get('platform') == 'photon':
        from plugins.platforms.photon.secure_intake.canonical_enrollment import canonical_transport_enrollment
        identity = dict(platform='photon', chat_type='dm', sender=target.get('user_id'), conversation=target.get('chat_id'), message=binding.get('source_id'))
        fresh = canonical_transport_enrollment(identity)
        if fresh.get('owner') != principal or target.get('notifier_profile') != 'default' or target.get('chat_type') != 'dm' or target.get('thread_id') is not None:
            raise PermissionError('Owner message return mismatch')
    else:
        raise PermissionError('Unsupported private return')
    from gateway.gateway_document_custody import KIND as CUSTODY
    return dict(kind=CUSTODY, owner=principal, domain='recipient_documents', realm='recipient:' + principal,
                board=board_name, source_digest=digest(binding), policy_generation=1,
                policy_fingerprint=digest(auth), transport_fingerprint=digest(target), enabled_from=1)


def list_agents(args, **kwargs):
    principal = current()['principal']
    if args != {}:
        raise ValueError('No arguments accepted')
    rows = []
    for row in agents():
        if row['id'] in {'margot', 'default', principal, 'fable-review'}:
            continue
        rows.append({k: row[k] for k in ('id', 'name', 'purpose')})
    from gateway.owner_agent_creation import pending_summary
    asked = pending_summary(current())
    current()
    if asked is not None:
        # Server memory of the agent question already asked in this exact
        # conversation; a plain yes now confirms it via own_agent_confirm.
        return json.dumps({'agents': rows, 'pending_proposal': asked})
    return json.dumps({'agents': rows})


def verify_return(conn, sub):
    """Revalidate the persisted private source before any notifier side effect."""
    row = conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (sub['task_id'],)).fetchone()
    if row is None:
        return
    binding = json.loads(row[0])
    if binding.get('kind') != KIND:
        return
    actual = Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'))
    if actual != database(enrollment(binding.get('principal'))['board']):
        raise PermissionError('Private return board mismatch')
    verify_task(conn, sub['task_id'])
    target = binding['target']
    for key in ('platform', 'chat_id', 'notifier_profile', 'user_id', 'delivery_mode'):
        if sub.get(key) != target.get(key):
            raise PermissionError('Private return target changed')
    if (sub.get('thread_id') or None) != target.get('thread_id'):
        raise PermissionError('Private return thread changed')
    metadata = sub.get('delivery_metadata') or '{}'
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if any(metadata.get(k) != v for k, v in target['delivery_metadata'].items()):
        raise PermissionError('Private return session changed')


def register():
    from gateway.owner_agent_creation import register as register_creation
    register_creation()
    from tools.registry import registry, no_cache_check_fn
    check = no_cache_check_fn(available)
    registry.register(name='own_agents_list', toolset='owner_delegation', handler=list_agents, check_fn=check,
        schema={'name': 'own_agents_list', 'description': 'List your active sidebar agents and their specialties. Use these exact IDs for delegation.', 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}})
    registry.register(name='own_work_create', toolset='owner_delegation', handler=create, check_fn=check,
        schema={'name': 'own_work_create', 'description': 'Delegate substantive work to your own agent automatically. One card per request, private to the authenticated owner, with an original-thread return and reviewed files on your Desk. No permission question needed for delegation; external actions retain their approvals.',
                'parameters': {'type': 'object', 'properties': {'title': {'type': 'string', 'maxLength': 140}, 'request': {'type': 'string', 'maxLength': 16000}, 'agent': {'type': 'string', 'maxLength': 64}}, 'required': ['title', 'request', 'agent'], 'additionalProperties': False}})

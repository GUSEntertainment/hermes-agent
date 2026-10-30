"""Confirmed owner agent creation using the app's existing owner_agents store.

Metadata IDs are shared with the sidebar. A new dedicated execution profile has
no imported credentials, MCPs, memories, home paths or company permissions.
"""
from contextlib import closing
import json
import logging
import os
import re
import time
from gateway import owner_delegation as work


_log = logging.getLogger('gateway.owner_agent_creation')


class ConfirmationDenied(PermissionError):
    """Denial with a stable, redacted reason code for telemetry and UX."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _deny(code, message, ctx=None, proposal=None):
    # Redacted technical telemetry: reason code, owner and short hashes only.
    # Never the message text, names, purposes or full ids.
    _log.warning('owner_agent_confirm denied code=%s owner=%s session=%s proposal=%s',
                 code, ctx['principal'] if ctx else '<unbound>',
                 work.digest(['s', ctx['session']])[:12] if ctx else '-',
                 (proposal or '-')[:12])
    raise ConfirmationDenied(code, message)


def conversation(ctx):
    """Stable per-conversation key bound by the authenticated dispatcher.

    The api_server binds HERMES_SESSION_KEY to the per-RUN approval key, so it
    changes on every app turn and a proposal could never be confirmed from the
    next message. ctx['session'] is the native session id that bind() verified
    for this authenticated owner and is constant for one app conversation and
    distinct across conversations.
    """
    if ctx['identity']:
        return 'photon:' + ctx['identity']['conversation']
    from gateway.session_context import get_bound_session_env
    session = ctx.get('session')
    if type(session) is not str or not session or get_bound_session_env('HERMES_SESSION_ID') != session:
        raise PermissionError('Original conversation required')
    return 'api:session:' + session


_YES = ('yes', 'yep', 'yeah', 'yup', 'sure', 'ok', 'okay', 'confirm', 'confirmed',
        'approve', 'approved', 'do it', 'go ahead', 'create it', 'make it', 'sounds good', 'please do')
_NEG = re.compile(r"\b(no|nope|not|don'?t|do not|never|cancel|stop|wait|hold|later|instead|rather|but|unless|change|rename|call (it|him|her|them))\b|\?|n't\b")
_ACTION = re.compile(r'^(and |please |)?(create|make|add|set up|build|go ahead( and)?|do it|proceed)?( (it|him|her|them|the agent|that agent|this agent|that one|this one))?( please| now| thanks?| thank you)?$')


def creation_directive(message, name):
    """An explicit named creation request is authority, not a new proposal."""
    text=' '.join(message.replace('\u2019', "'").casefold().split())
    target=' '.join(name.casefold().split())
    for clause in re.split(r'[.!;\n]',text):
        match=re.search(r'\b(?:create|make|add|set up|build)\s+(?:an? agent (?:called|named)\s+)?'+re.escape(target)+r'\b',clause)
        if not match: continue
        before=clause[:match.start()]
        if re.search(r"\b(?:no|not|don't|do not|never|cancel|stop|wait|maybe|should|whether|recommend)\b",before): continue
        if not before.strip() or re.search(r'\b(?:yes|please|go ahead|can you|could you|want you to|want to|need you to)\b',before): return True
    return False


def affirmative(message, name):
    """Bounded explicit consent: a leading yes, optionally followed only by a
    create verb and/or the exact proposed name. Anything else (negation,
    question, different name, extra instructions) is not consent."""
    if creation_directive(message,name):
        return True
    text = ' '.join(message.replace('\u2019', "'").casefold().split()).strip(' .!')
    if not text or len(text) > 120 or _NEG.search(text):
        return False
    lead = next((y for y in sorted(_YES, key=len, reverse=True) if text == y or text.startswith(y + ' ') or text.startswith(y + ',')), None)
    if lead is None:
        return False
    rest = text[len(lead):].strip(' ,.!')
    target = ' '.join(name.casefold().split())
    if target and rest.endswith(target):
        rest = rest[:-len(target)].strip(' ,.!')
    rest = re.sub(r'^(please|thanks?|thank you)\s*', '', rest).strip(' ,.!')
    rest = re.sub(r'\s*(please|thanks?|thank you)$', '', rest).strip(' ,.!')
    return rest == '' or bool(_ACTION.fullmatch(rest))


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS owner_agent_proposals(id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation TEXT NOT NULL,source TEXT NOT NULL,record_json TEXT NOT NULL,created_at REAL NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0)')
    # Durable outcome of a confirmed proposal: the agent and the routed task.
    # A later repeated yes returns this outcome instead of asking again, and a
    # failed routing step is retried without a new consent prompt.
    conn.execute('CREATE TABLE IF NOT EXISTS owner_agent_confirmations(proposal_id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation TEXT NOT NULL,agent_id TEXT NOT NULL,profile TEXT NOT NULL,task_id TEXT,board TEXT,error TEXT,updated_at REAL NOT NULL)')


def _same(a, b):
    return ' '.join(str(a).casefold().split()) == ' '.join(str(b).casefold().split())


def pending(conn, ctx):
    """Latest unconfirmed, unexpired proposal asked in an EARLIER turn of this
    exact owner conversation. Channels like iMessage start each turn with no
    model history, so the server, not the model, must remember the question."""
    row = conn.execute('SELECT * FROM owner_agent_proposals WHERE owner=? AND conversation=? ORDER BY created_at DESC LIMIT 1',
                       (work.owner(), conversation(ctx))).fetchone()
    if not row or row['confirmed'] or time.time() - row['created_at'] > 86400:
        return None
    if ctx['run'] == row['source'] or (ctx['identity'] and ctx['identity']['message'] == row['source']):
        return None
    return row


def pending_summary(ctx):
    """Redacted pending question for own_agents_list (id, name, purpose only)."""
    try:
        with closing(work._connect(work.ROOT / 'agent-os/owner-agents.sqlite', write=True)) as conn, conn:
            schema(conn)
            row = pending(conn, ctx)
            if row is None:
                return None
            record = json.loads(row['record_json'])
            existing=conn.execute('SELECT name FROM owner_agents WHERE owner=? AND archivedAt IS NULL',(ctx['principal'],)).fetchall()
            if any(_same(r['name'],record['name']) for r in existing):
                return None
            return {'proposal_id': row['id'], 'name': record['name'], 'purpose': record['purpose'],
                    'reply_is_yes': affirmative(ctx['message'], record['name'])}
    except (PermissionError, OSError, ValueError, KeyError):
        return None


def propose(args, **kwargs):
    ctx = work.current()
    if type(args) is not dict or set(args) != {'name', 'purpose', 'title', 'request'}:
        raise ValueError('Name, purpose and original work required')
    for key, limit in [('name', 60), ('purpose', 240), ('title', 140), ('request', 16000)]:
        if type(args[key]) is not str or not args[key].strip() or len(args[key]) > limit or (key in {'name', 'purpose'} and any(ord(c) < 32 for c in args[key])):
            raise ValueError('Bounded agent proposal required')
    existing=[r for r in work.agents() if _same(r['name'],args['name'])]
    if len(existing)>1:
        raise PermissionError('Ambiguous existing agent name')
    if existing:
        row=existing[0]
        runtime=work.agent(row['id'])['runtime']
        return json.dumps({'agent_id':row['id'],'profile':runtime,'agent_name':row['name'],'created':False,'already_created':True,
                           'message':'This agent already exists. Use own_work_create to delegate the current work; do not ask for creation consent again.'})
    # Source, original request and conversation are server-bound, not model claims.
    if len(ctx['message'] + '\n\n' + args['request']) > 16000:
        raise ValueError('Saved work exceeds the bounded task request; shorten the request before proposing')
    source = ctx['identity']['message'] if ctx['identity'] else ctx['run']
    record = dict(args, original=ctx['message'], authority=ctx['authority'])
    key = work.digest([work.owner(), conversation(ctx), source])
    with closing(work._connect(work.ROOT / 'agent-os/owner-agents.sqlite', write=True)) as conn, conn:
        schema(conn)
        # The person is answering the question already asked in this
        # conversation. Re-proposing the same agent would only ask again.
        prior_ask = pending(conn, ctx)
        if prior_ask is not None:
            asked = json.loads(prior_ask['record_json'])
            if _same(asked['name'], args['name']) and affirmative(ctx['message'], asked['name']):
                prior_id = prior_ask['id']
            else:
                prior_id = None
        else:
            prior_id = None
    if prior_id is not None:
        _log.info('owner_agent_propose resolved_to_pending_confirmation owner=%s proposal=%s', ctx['principal'], prior_id[:12])
        return confirm({'proposal_id': prior_id})
    with closing(work._connect(work.ROOT / 'agent-os/owner-agents.sqlite', write=True)) as conn, conn:
        schema(conn)
        prior = conn.execute('SELECT record_json FROM owner_agent_proposals WHERE id=?', (key,)).fetchone()
        if prior and prior[0] != work.encode(record):
            raise PermissionError('Agent proposal replay conflict')
        conn.execute('INSERT OR IGNORE INTO owner_agent_proposals VALUES(?,?,?,?,?,?,0)', (key, work.owner(), conversation(ctx), source, work.encode(record), time.time()))
        work.current()
    if creation_directive(ctx['message'],args['name']):
        return confirm({'proposal_id':key})
    return json.dumps({'proposal_id': key, 'suggestion': f"Want a {args['name']} agent for {args['purpose']}?", 'created': False})


OWNER_SKILL_BUNDLE = 'private/owner-agent-skills/v1'


def _install_owner_skills(folder):
    """Copy the sanitized, neutral-brand owner skill bundle (no GUS data)."""
    import shutil
    source = work.ROOT / OWNER_SKILL_BUNDLE
    if not source.is_dir() or source.is_symlink():
        return
    target = folder / 'skills' / 'owner-toolkit'
    if target.exists():
        return
    target.parent.mkdir(mode=0o700, exist_ok=True)
    shutil.copytree(source, target, symlinks=False, ignore=shutil.ignore_patterns('*.json'))


def _profile(identifier, name, purpose):
    return _profile_for(identifier,name,purpose,principal=work.owner(),board_name=work.board())


def _profile_for(identifier,name,purpose,*,principal,board_name):
    runtime = work.agent_profile_name(principal, identifier)
    folder = work.ROOT / 'profiles' / runtime
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.resolve() != folder:
        raise PermissionError('Private profile path changed')
    # No mutable global default/template import, company configs, copied keys or
    # auth files. Model selection follows the dispatcher's normal provider config.
    config = {'model': {'default': 'gpt-6-sol', 'provider': 'openai-codex'},
              'toolsets': ['terminal', 'file', 'kanban', 'worker_skills', 'todo', 'web', 'browser', 'memory', 'session_search', 'vision', 'image_gen', 'code_execution', 'delegation'],
              'mcp_servers': {}, 'memory': {'memory_enabled': True, 'user_profile_enabled': True},
              # Chris 2026-09-28: same permissions as Josh's Margot, same Mac terminal.
              'terminal': {'backend': 'ssh', 'cwd': '/Users/jj', 'ssh_host': '100.71.37.12', 'ssh_user': 'jj', 'ssh_port': 22,
                           'ssh_key': '/Users/Chris/.ssh/josh-margot-temp', 'timeout': 600, 'persistent_shell': False, 'ssh_sync_files': False},
              'owner_device_enabled': True,
              'owner_agent': {'owner': principal, 'id': identifier, 'board': board_name},
              'security': {'workspace_confinement': {'enabled': True, 'root_from_task_cwd': True,
                  'deny_terminal_network': False, 'deny_home_reads_outside_workspace': True, 'read_only_roots': []}}}
    import yaml
    files = {'config.yaml': yaml.safe_dump(config, sort_keys=True),
             'SOUL.md': f'You are {name}, Josh\'s private agent. Purpose: {purpose}\nExecute only a native admitted Josh task in its assigned workspace. No Chris/GUS memory, boards, Vault, credentials or other members\' data. Your terminal runs on Josh\'s own Mac, the same access his Margot has. Do the work there. Never open Chris\'s personal files or settings. Report unavailable connected sources honestly. Preserve external-action approval gates.\n'}
    if principal != 'joshgarcia':
        # A newly enrolled owner does not inherit Josh's device, credentials or
        # model-selected roots. Native task admission confines this local worker
        # to its exact workspace before execution.
        config['terminal'] = {'backend': 'local', 'timeout': 600}
        config['owner_device_enabled'] = False
        config['security']['workspace_confinement']['deny_terminal_network'] = True
        files['config.yaml'] = yaml.safe_dump(config, sort_keys=True)
        files['SOUL.md'] = (f'You are {name}, a private agent for {principal}. Purpose: {purpose}\n'
                            'Execute only a native admitted task in its assigned owner workspace. '
                            'Do not access shared host files, another owner, credentials, memory or boards. '
                            'Report unavailable connected sources honestly. Preserve external-action approval gates.\n')
    if principal == 'joshgarcia':
        _install_owner_skills(folder)
    for filename, text in files.items():
        target = folder / filename
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            from gateway.app_business_authority import protected_bytes
            if protected_bytes(target, True, work.ROOT) != text.encode():
                raise PermissionError('Existing profile differs from confirmed private agent')
        else:
            with os.fdopen(fd, 'w') as f:
                f.write(text)
    return runtime


def confirm(args, **kwargs):
    ctx = work.current()
    if type(args) is not dict or set(args) != {'proposal_id'} or type(args['proposal_id']) is not str or not re.fullmatch('[a-f0-9]{64}', args['proposal_id']):
        raise ValueError('Exact proposal required')
    pid = args['proposal_id']
    with closing(work._connect(work.ROOT / 'agent-os/owner-agents.sqlite', write=True)) as conn, conn:
        schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        proposal = conn.execute('SELECT * FROM owner_agent_proposals WHERE id=? AND owner=? AND conversation=?', (pid, work.owner(), conversation(ctx))).fetchone()
        if not proposal:
            _deny('proposal_not_in_conversation', 'No agent proposal from this conversation matches; propose it here first, then ask for yes.', ctx, pid)
        if time.time() - proposal['created_at'] > 86400:
            _deny('proposal_expired', 'That agent proposal expired; propose it again and ask for yes.', ctx, pid)
        if proposal['confirmed']:
            done = conn.execute('SELECT * FROM owner_agent_confirmations WHERE proposal_id=? AND owner=? AND conversation=?',
                                (pid, work.owner(), conversation(ctx))).fetchone()
            if done is None:
                _deny('proposal_already_used', 'That proposal was already confirmed; it cannot be replayed.', ctx, pid)
            conn.rollback()
            return _route(dict(done), json.loads(proposal['record_json']), repeat=True)
        latest = conn.execute('SELECT id FROM owner_agent_proposals WHERE owner=? AND conversation=? ORDER BY created_at DESC LIMIT 1', (work.owner(), conversation(ctx))).fetchone()
        if latest[0] != pid:
            _deny('proposal_superseded', 'A newer proposal exists in this conversation; confirm the latest one only.', ctx, pid)
        record = json.loads(proposal['record_json'])
        # The affirmative must be the actual authenticated current human message
        # (server-bound ctx), sent AFTER the proposal turn, never a tool boolean,
        # model-supplied quote or historical context.
        if (ctx['run'] == proposal['source'] or (ctx['identity'] and ctx['identity']['message'] == proposal['source'])) and not creation_directive(ctx['message'],record['name']):
            _deny('confirmation_same_turn', f"Ask \"Create {record['name']}?\" and wait for the person's reply before confirming.", ctx, pid)
        if not affirmative(ctx['message'], record['name']):
            _deny('confirmation_not_explicit', f"Not created. The reply was not a plain yes to \"{record['name']}\". If they want a different name or purpose, propose that instead and ask again.", ctx, pid)
        if len(record['original'] + '\n\n' + record['request']) > 16000:
            raise ValueError('Saved work exceeds the bounded task request')
        if record['authority'] != ctx['authority']:
            raise PermissionError('Proposal authority changed')
        identifier = 'oa_' + work.digest([work.owner(), args['proposal_id']])[:32]
        prior = conn.execute('SELECT * FROM owner_agents WHERE owner=? AND id=?', (work.owner(), identifier)).fetchone()
        if prior and (prior['name'] != record['name'] or prior['purpose'] != record['purpose'] or prior['archivedAt'] is not None):
            raise PermissionError('Confirmed agent changed')
        if not prior and conn.execute('SELECT count(*) FROM owner_agents WHERE owner=?', (work.owner(),)).fetchone()[0] >= 100:
            raise PermissionError('Owner agent limit reached')
        work.current()
        runtime = _profile(identifier, record['name'], record['purpose'])
        conn.execute('INSERT OR IGNORE INTO owner_agents(id,owner,name,purpose,archivedAt,createdAt,requestId) VALUES(?,?,?,?,NULL,?,?)', (identifier, work.owner(), record['name'], record['purpose'], int(time.time()*1000), 'confirmed:' + args['proposal_id']))
        conn.execute('UPDATE owner_agent_proposals SET confirmed=1 WHERE id=?', (args['proposal_id'],))
        conn.execute('INSERT OR IGNORE INTO owner_agent_confirmations VALUES(?,?,?,?,?,NULL,NULL,NULL,?)',
                     (pid, work.owner(), conversation(ctx), identifier, runtime, time.time()))
        work.current()
    done = dict(proposal_id=pid, agent_id=identifier, profile=runtime, task_id=None)
    return _route(done, record, repeat=False)


def _route(done, record, *, repeat):
    """Route the saved work once. The agent and the consent are already durable;
    a routing failure is a system problem to retry, never a new consent ask."""
    ctx = work.current()
    base = dict(agent_id=done['agent_id'], profile=done['profile'], agent_name=record['name'],
                created=not repeat, already_created=repeat)
    if done.get('task_id'):
        return json.dumps(dict(base, accepted=True, task_id=done['task_id'], board=done.get('board'), duplicate=True))
    try:
        result = json.loads(work.create({'title': record['title'], 'request': record['original'] + '\n\n' + record['request'], 'agent': done['agent_id']}))
        task_id, board, error = result.get('task_id'), result.get('board'), None
    except Exception as exc:  # recorded for the operator; consent is not re-asked
        result, task_id, board, error = {'accepted': False}, None, None, type(exc).__name__
        _log.warning('owner_agent_confirm routing_failed owner=%s proposal=%s error=%s', ctx['principal'], done['proposal_id'][:12], error)
    with closing(work._connect(work.ROOT / 'agent-os/owner-agents.sqlite', write=True)) as conn, conn:
        conn.execute('UPDATE owner_agent_confirmations SET task_id=COALESCE(task_id,?),board=COALESCE(board,?),error=?,updated_at=? WHERE proposal_id=?',
                     (task_id, board, error, time.time(), done['proposal_id']))
    if error:
        return json.dumps(dict(base, accepted=False, routing_error=error,
            message=f"{record['name']} is created and saved. Starting the work hit a system error that has been logged for the team; do not ask for approval again."))
    return json.dumps(dict(result, **base))


def register():
    from tools.registry import registry, no_cache_check_fn
    check = no_cache_check_fn(work.available)
    properties = {key: {'type': 'string', 'maxLength': limit} for key, limit in [('name', 60), ('purpose', 240), ('title', 140), ('request', 16000)]}
    registry.register(name='own_agent_propose', toolset='owner_delegation', handler=propose, check_fn=check,
        schema={'name': 'own_agent_propose', 'description': 'When no current sidebar agent fits, save a one-line name/purpose suggestion and the work. Does NOT create an agent or task. A clear named creation directive creates immediately. Otherwise ask the returned question once and wait for their reply. If they pick a different name or purpose, propose again.', 'parameters': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}})
    registry.register(name='own_agent_confirm', toolset='owner_delegation', handler=confirm, check_fn=check,
        schema={'name': 'own_agent_confirm', 'description': 'After the person says yes to the latest proposal in this conversation (own_agents_list shows it as pending_proposal even when this turn has no earlier chat history), create their private agent in their sidebar and automatically route the saved work. Server verifies the actual yes, scope and proposal. If it returns an error, tell the person plainly that the agent was NOT created and why; never claim success.', 'parameters': {'type': 'object', 'properties': {'proposal_id': {'type': 'string'}}, 'required': ['proposal_id'], 'additionalProperties': False}})

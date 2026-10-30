"""Effective native task policy derived from protected member-board ownership.

No model-supplied flag or tenant can disable it. Global department configs and
Chris's own board are unchanged. Caller supplies a native task ID, never an
owner grant. The existing authenticated FD and OS/file sandbox remain authority.
"""
import copy,json,re,sqlite3
from pathlib import Path
ROOT=Path('/Users/Chris/.hermes')
# t_82ea5042: exact legacy native boards whose owner predates the protected
# member catalog. Exact slug -> owner only; no prefix/alias, unknown boards stay
# denied, and a member/catalog claim on the slug still takes precedence.
LEGACY_OWNER_BOARDS={'chris':'chris'}
TOOLSETS=('terminal','file','kanban','worker_skills','todo','delegation')
VERSION='gus-member-workspace/v1'
PRIVATE_VERSION='owner-private-action-workspace/v1'
# Owner-org capability projection (Chris 2026-10-05): an owner's own private
# agents get the same working tools Margot's agents use, intersected with what
# their profile declares. Workspace-confined; no MCP, memory, shared history,
# terminal network, host browser profile, private URLs or other owners' data.
OWNER_AGENT_PROJECTION_VERSION='owner-agent-tools/v1'
# t_10f94d4e: every registered member gets the same owner-agent extras.
class _Extras(dict):
    def get(self,k,d=()):
        from gateway.member_registry import members
        return ('vision','image_gen','browser') if k in members() else d
OWNER_AGENT_EXTRA_TOOLSETS=_Extras()

def owner_agent_extras(config,policy):
    if not isinstance(policy,dict) or policy.get('version')!=PRIVATE_VERSION:return ()
    if not str(policy.get('action') or '').startswith('owner_work:'):return ()
    extras=OWNER_AGENT_EXTRA_TOOLSETS.get(policy.get('principal'),())
    binding=config.get('owner_agent') if isinstance(config,dict) else None
    if not extras or not isinstance(binding,dict) or binding.get('owner')!=policy.get('principal'):return ()
    if binding.get('board')!=policy.get('board'):return ()
    declared=(config.get('platform_toolsets') or {}).get('cli', config.get('toolsets') or [])
    disabled=(config.get('agent') or {}).get('disabled_toolsets', [])
    return tuple(t for t in extras if t in declared and t not in disabled)

def select(task_id, board, worker, *, root=ROOT):
    import yaml
    from gateway.app_business_authority import protected_bytes,current
    from gateway.gus_worker_authority import enrolled_profiles,REGISTRY,protected
    root=Path(root)
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}',board or ''):raise PermissionError('Canonical native board required')
    from gateway.gus_worker_authority import owner_agent_binding
    member=owner_agent_binding(worker,root) if root==ROOT else None
    if member is not None and member[1]!=board:raise PermissionError('Member worker outside its owner board')
    from gateway.owner_delegation import worker_policy
    delegated_policy = worker_policy(task_id, board, worker)
    if delegated_policy is not None:return delegated_policy
    from gateway.private_action_worker_policy import current as private_current
    private_policy=private_current(task_id,board,worker)
    if private_policy is not None:return private_policy
    matches=[]
    for path in sorted((root/'profiles').glob('app-gus-*/config.yaml')):
        raw=protected_bytes(path,anchor=root)
        config=yaml.safe_load(raw) or {};binding=config.get('app_business_authority')
        if isinstance(binding,dict) and binding.get('board')==board:
            matches.append((path.parent.name,binding.get('principal')))
    # Disabled entries remain classification evidence; only current() may grant.
    # This prevents deletion/revocation of an app binding widening a member task.
    catalog=set()
    for relative,key,ownerkey in (
        ('private/phone-assistant/caller-profiles.json','profiles','profile_key'),
        ('private/photon-intake/custody-enrollments.json','enrollments','owner')):
        path=root/relative
        if not path.is_file():
            if root==ROOT:raise PermissionError('Protected board catalog unavailable')
            continue
        data=json.loads(protected_bytes(path,anchor=root))
        rows=data.get(key)
        if not isinstance(rows,list):raise PermissionError('Invalid protected board catalog')
        for entry in rows:
            if entry.get('board')==board:
                owner=entry.get(ownerkey)
                if not isinstance(owner,str) or not owner:raise PermissionError('Invalid board owner')
                catalog.add(owner)
    catalog.update(p for _,p in matches)
    if len(catalog)>1:raise PermissionError('Ambiguous member board owner')
    if catalog=={'chris'}:return None
    if not catalog and root==ROOT and LEGACY_OWNER_BOARDS.get(board)=='chris':
        from gateway.member_registry import boards as _member_boards
        if board not in set(_member_boards().values()):return None
        raise PermissionError('Ambiguous member board owner')
    if not catalog:
        # Existing non-command boards do not gain a member grant here. A deleted
        # canonical member enrollment remains fail-closed for persisted boards.
        if root==ROOT and board!='default' and (root/'kanban/boards'/board).exists():
            raise PermissionError('Native board owner unavailable')
        return None
    if not matches:raise PermissionError('Member board enrollment unavailable')
    db=root/'kanban/kanban.db' if board=='default' else root/'kanban/boards'/board/'kanban.db'
    if not db.is_file():raise PermissionError('Native board unavailable')
    protected(db,root)
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
        row=conn.execute('SELECT assignee FROM tasks WHERE id=?',(task_id,)).fetchone()
    # Reviewer assignment is independently checked by native admission; do not
    # require maker == reviewer here.
    if row is None:raise PermissionError('Native task unavailable')
    principals={p for _,p in matches}
    if len(principals)!=1 or len(matches)!=1:raise PermissionError('Ambiguous member board owner')
    app,principal=matches[0]
    authority=current(app,principal,root=root)
    if authority['board']!=board:raise PermissionError('Member board binding changed')
    if principal=='chris':return None
    if worker not in enrolled_profiles(REGISTRY,root.parent):raise PermissionError('Member task requires installed GUS worker')
    return {'version':VERSION,'principal':principal,'board':board,'task':task_id,'worker':worker,
            'app_profile':app,'authority_fingerprint':authority['fingerprint']}

def verify(policy):
    if not isinstance(policy,dict) or policy.get('version') not in {VERSION,PRIVATE_VERSION}:raise PermissionError('Invalid member worker policy')
    if select(policy.get('task'),policy.get('board'),policy.get('worker'))!=policy:
        raise PermissionError('Member worker enrollment changed')

def effective_toolsets(config,policy=None):
    # Public source lookup uses existing read-only web tools, never terminal
    # network access, browser sessions, MCP accounts or another owner's files.
    declared = (config.get('platform_toolsets') or {}).get('cli', config.get('toolsets') or [])
    disabled = (config.get('agent') or {}).get('disabled_toolsets', [])
    return (*TOOLSETS, *(['web'] if 'web' in declared and 'web' not in disabled else []), *owner_agent_extras(config,policy))


def member_owner_full(config,policy):
    """t_65dff1fd: a member owner's own enrolled profile/agent on its own board
    keeps its full declared toolsets and ONLY its own configured MCP servers."""
    if not isinstance(policy,dict):return False
    from gateway.gus_worker_authority import owner_agent_binding,MEMBER_OWNER_BOARDS
    worker=policy.get('worker');principal=policy.get('principal');board=policy.get('board')
    if principal not in MEMBER_OWNER_BOARDS or MEMBER_OWNER_BOARDS[principal]!=board:return False
    return owner_agent_binding(worker)==(principal,board)

def own_mcp_servers(config):
    servers=config.get('mcp_servers') or {}
    return {n:c for n,c in servers.items() if isinstance(c,dict) and str(c.get('enabled',True)).lower() not in {'false','0','no','off','none'}}

def full_toolsets(config):
    declared=(config.get('platform_toolsets') or {}).get('cli') or config.get('toolsets') or []
    disabled=(config.get('agent') or {}).get('disabled_toolsets', [])
    out=[t for t in declared if t!='no_mcp' and t not in disabled]
    for name in own_mcp_servers(config):
        if name not in out:out.append(name)
    for t in TOOLSETS:
        if t not in out:out.append(t)
    return out

def effective(config,policy):
    if policy is None:return config
    if policy.get('version') not in {VERSION,PRIVATE_VERSION}:raise PermissionError('Unknown member worker policy')
    if member_owner_full(config,policy):
        result=copy.deepcopy(config)
        toolsets=full_toolsets(config)
        result['toolsets']=list(toolsets)
        result['platform_toolsets']={'cli':list(toolsets),'kanban':list(toolsets)}
        result['mcp_servers']=own_mcp_servers(config)
        result['privacy']={'redact_pii':True}
        security=result.setdefault('security',{})
        conf=security.setdefault('workspace_confinement',{})
        conf.update({'enabled':True,'root_from_task_cwd':True})
        return result
    result=copy.deepcopy(config)
    toolsets = effective_toolsets(config,policy)
    result['toolsets']=[*toolsets,'no_mcp']
    result['platform_toolsets']={'cli':[*toolsets,'no_mcp'],'kanban':[*toolsets,'no_mcp']}
    result['mcp_servers']={};result['memory']={'memory_enabled':False,'user_profile_enabled':False};result['privacy']={'redact_pii':True}
    security=result.setdefault('security',{})
    security['workspace_confinement']={'enabled':True,'root_from_task_cwd':True,'deny_terminal_network':True,'deny_home_reads_outside_workspace':True,'read_only_roots':[]}
    terminal=result.setdefault('terminal',{});terminal['backend']='local'
    delegation=result.setdefault('delegation',{})
    delegation['inherit_mcp_toolsets']=False
    delegation['default_toolsets']=['terminal','file','worker_skills']
    if 'image_gen' in toolsets:
        # Same subscription-backed image provider as the agent's own model
        # login; no paid key, FAL or managed gateway is opted in.
        result['image_gen']={'provider':'openai-codex','model':'gpt-image-2-medium'}
    if 'browser' in toolsets:
        # Isolated headless built-in browser; never the host's real profile,
        # CDP override or Browser Use harness.
        browser=result.setdefault('browser',{})
        browser.update({'backend':'off','use_real_profile':False,'cdp_url':'','allow_private_urls':False,'cloud_provider':'local'})
    # Model, concurrency, depth, timeout and spending settings are untouched.
    return result

def runtime_config(config):
    from agent.private_worker_mode import get_authenticated_worker_contract
    contract=get_authenticated_worker_contract()
    return effective(config,contract.get('member_company_policy')) if contract else config

def allowed_tools():
    from agent.private_worker_mode import get_authenticated_worker_contract
    contract=get_authenticated_worker_contract()
    if not contract or not contract.get('member_company_policy'):return None
    verify(contract['member_company_policy'])
    from toolsets import resolve_multiple_toolsets
    from agent.delegation_context import is_delegated_child_context
    allowed=set(resolve_multiple_toolsets(contract['toolsets']))
    if member_owner_full(None,contract.get('member_company_policy')) and not is_delegated_child_context():
        # Own MCP servers register at runtime; admit their tools and the
        # deferred-tool bridge that reaches them.
        from tools.registry import registry as _reg
        for ts in contract['toolsets']:
            try:allowed.update(_reg.get_tool_names_for_toolset(ts))
            except Exception:pass
        allowed.update({'tool_search','tool_describe','tool_call'})
    if isinstance(contract.get('member_company_policy'),dict) and contract['member_company_policy'].get('version')==PRIVATE_VERSION:
        # Raw CDP/JS-eval could reach file:// or host endpoints; keep page tools.
        allowed-={'browser_cdp','browser_exec'}
    if is_delegated_child_context():
        # Children inherit the same authenticated sandbox but never control
        # the parent's board lifecycle or recursively widen tools.
        allowed &= set(resolve_multiple_toolsets(['terminal','file','worker_skills']))
    return allowed

def private_worker_active():
    """True inside any authenticated private-owner worker (fail closed on error)."""
    try:
        return authenticated_private_policy() is not None
    except Exception:
        return True if __import__('os').environ.get('HERMES_WORKER_ADMISSION_FD','').strip() else False


def allowed_tool(name):
    allowed=allowed_tools()
    return allowed is None or name in allowed


def authenticated_private_policy():
    """Runtime authority only: env/task IDs cannot substitute for admitted FD."""
    from agent.private_worker_mode import get_authenticated_worker_contract
    contract=get_authenticated_worker_contract()
    if not contract:return None
    policy=contract.get('member_company_policy')
    if not isinstance(policy,dict) or policy.get('version')!=PRIVATE_VERSION:return None
    if contract.get('workspace_confinement') is not True:raise PermissionError('Private workspace confinement required')
    verify(policy)
    return policy

def admission_toolsets(config, policy):
    if policy is None:
        from hermes_cli.tools_config import _get_platform_tools
        return _get_platform_tools(config, 'cli')
    # effective() has selected the admitted workspace tools. Global CLI
    # conveniences must not augment this private task's startup catalog.
    if policy.get('version') not in {VERSION, PRIVATE_VERSION}:
        raise PermissionError('Unknown member worker policy')
    disabled = (config.get('agent') or {}).get('disabled_toolsets', [])
    return set(config['platform_toolsets']['cli']) - {'no_mcp', *disabled}


def admission_schema_definitions(toolsets,policy):
    """Read-only preflight catalog. Never installs an execution/FD context."""
    from model_tools import get_tool_definitions,_compute_tool_definitions
    if not isinstance(policy,dict) or policy.get('version')!=PRIVATE_VERSION:
        return get_tool_definitions(enabled_toolsets=toolsets,quiet_mode=True,skip_tool_search_assembly=True)
    verify(policy)
    if not set(toolsets).issubset(set(TOOLSETS) | {'web'} | set(OWNER_AGENT_EXTRA_TOOLSETS.get(policy.get('principal'),()))):
        if not member_owner_full(None,policy):raise PermissionError('Unexpected private admission toolsets')
        return _compute_tool_definitions(toolsets,quiet_mode=True,skip_tool_search_assembly=True)
    from toolsets import resolve_multiple_toolsets
    allowed=set(resolve_multiple_toolsets(toolsets))
    schemas=_compute_tool_definitions(toolsets,quiet_mode=True,skip_tool_search_assembly=True)
    if any(item.get('function',{}).get('name') not in allowed for item in schemas):raise PermissionError('Private schema outside task toolsets')
    return schemas


def authorize_private_kanban(name,args):
    """Enforce task arguments at the native handler boundary, after rewrites.

    No environment-derived exemption: authenticated_private_policy validates
    the current FD and durable source. Company/non-worker behavior is unchanged.
    """
    policy=authenticated_private_policy()
    if policy is None:return None
    from tools.kanban_tools import _is_delegated_child_context
    if _is_delegated_child_context():raise PermissionError('Private child cannot access native task tools')
    if not isinstance(args,dict):raise PermissionError('Invalid private task arguments')
    if args.get('board') not in (None,'',policy['board']):raise PermissionError('Private task board mismatch')
    reads={'kanban_show','kanban_attachments'}
    writes={'kanban_complete','kanban_block','kanban_request_review','kanban_request_changes',
            'kanban_heartbeat','kanban_comment','kanban_attach','kanban_attach_url','kanban_rebind_review_artifact'}
    if name not in reads|writes:raise PermissionError('Private task orchestration is not admitted')
    target=args.get('task_id') or policy['task']
    allowed={policy['task']}
    if name in reads and policy.get('preparation_for'):allowed.add(policy['preparation_for'])
    if target not in allowed:raise PermissionError('Private native task target outside admitted scope')
    return policy

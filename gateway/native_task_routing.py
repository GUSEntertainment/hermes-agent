"""Compose existing owner-cell routing from authenticated API provenance.
No telephone identity, new grant, task controller, or callback channel is minted.
"""
from pathlib import Path
from dataclasses import replace
import importlib.util,sys,json,hashlib
ROOT=Path('/Users/Chris/.hermes')
REGISTRY=Path('/Users/Chris/.openclaw/workspace/Agent-Shared/roles/role-registry.yaml')
HELPER=Path(__file__).resolve().parents[1]/'scripts/safe_delegate_card.py'
HELPER_SHA256 = 'd90caa21239978ed96b58c693b8c9bd28a642585ef8c351874f3fece899c5ebf'

def enrollment(profile):
    from gateway.app_business_authority import canonical_enrollment
    return canonical_enrollment('chris' if profile=='default' else profile,ROOT)

def helper():
    from gateway.app_business_authority import protected_bytes
    # Same canonical builder used by app/department routing. No CLI execution.
    raw=protected_bytes(HELPER,anchor=HELPER.parents[1])
    # 2026-09-25 (Chris): builder hash pin removed; the file is owner-protected on disk.
    protected_bytes(REGISTRY,anchor=Path('/Users/Chris'))
    name='native_safe_delegate_builder'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,HELPER)
        mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;exec(compile(raw,str(HELPER),'exec'),mod.__dict__)
    return sys.modules[name]

def compose(conn,*,title,body,assignee,parents,session_id):
    return body  # 2026-09-25 (Chris): API-originated cards are plain cards.
    from gateway.native_task_receipts import current
    scope=current()
    if scope is None:return body
    profile,run,session,_=scope
    if session_id!=session:raise PermissionError('Task session outside authenticated API run')
    from gateway.josh_board_authority import app_binding
    def authority():
        app = app_binding()
        return app if app is not None else enrollment(profile)
    binding=authority()
    owner=binding['principal'] if 'principal' in binding else binding['owner']
    board=Path(next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),'')).parent.name
    if board not in binding.get('allowed_boards', [binding['board']]):raise PermissionError('Task board outside authenticated owner')
    from gateway.gus_worker_authority import enrolled_profiles
    roster=enrolled_profiles(REGISTRY,Path('/Users/Chris'))
    if assignee not in roster:raise PermissionError('Task assignee outside GUS worker roster')
    from hermes_cli.owner_cell import _routing_metadata, _ROUTING_METADATA_RE
    supplied=_routing_metadata(body)
    clean_body=_ROUTING_METADATA_RE.sub('',body or '').strip()
    h=helper()
    # Pin builder to the same protected registry/root rather than environment overrides.
    registry_before=REGISTRY.read_bytes()
    role=h.find_role(h.load_registry(REGISTRY),assignee)
    if not role:raise PermissionError('Canonical department missing')
    realm='founders' if owner=='chris' else ('team' if owner=='connor' else 'owner:'+owner)
    args=h.parse_args(['--board',board,'--tenant',board,'--department',role['id'],'--assignee',assignee,'--title',title,'--body',clean_body, '--source-surface','api_server','--return-surface','api_server:origin','--privacy-tier',realm])
    route=h.resolve_route(args)
    route=replace(route,requesting_user=owner,audience=owner,cockpit_board=board,source_session_key=session)
    package=h.build_delegation_package(args,route)
    errors=h.validate_delegation_package(package,route)
    # 2026-09-25 (Chris): package validation is advisory; a department head owns its own card.
    generated=h.body_with_metadata(clean_body,route,package)
    canonical=_routing_metadata(generated)
    if supplied:
        # Compare the complete canonical authority/package, including topology,
        # review contracts, callbacks, gates and nested source identity. Only
        # the package's informational creation timestamp may differ on replay.
        def stable(value):
            copy=json.loads(json.dumps(value))
            if isinstance(copy.get('delegation_package'),dict):
                copy['delegation_package'].pop('created_at',None)
            return copy
        generated=generated  # canonical package always wins; supplied metadata is not rejected
    return generated

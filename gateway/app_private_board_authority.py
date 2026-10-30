"""Private-app board controls bound to existing private work enrollment.

This adds no work creation, memory, filesystem, delivery or company grant.
Only a directly authenticated private listener can use the four own-board
controls. The controller independently re-resolves the same protected files.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import time

import yaml

ROOT = Path('/Users/Chris/.hermes')
CONTRACT = 'margot-owner-private-v1'
TOOLSET = 'owner_board_control'


def private_profile(profile):
    return isinstance(profile, str) and re.fullmatch(r'app-owner-[a-z][a-z0-9_-]{0,63}-margot', profile) is not None


def capabilities(profile, principal):
    """Advertise only freshly admitted private tools, never configuration alone."""
    from gateway.josh_board_authority import execution_scope, app_binding
    try:
        with execution_scope(profile, principal):
            bound = app_binding()
            if bound is None or bound.get('scope_contract') != CONTRACT:
                return None
            from gateway.member_board_tools import TOOLS, enabled
            from model_tools import get_tool_definitions
            schemas = get_tool_definitions([TOOLSET], quiet_mode=True, skip_tool_search_assembly=True)
            if {s['function']['name'] for s in schemas} != TOOLS or not enabled():
                return None
            return {'tools': sorted(TOOLS)}
    except (PermissionError, OSError, ValueError):
        return None


def current(profile, principal, *, root=None, enrollment=None):
    from gateway.app_business_authority import protected_bytes, canonical_enrollment
    root = ROOT if root is None else Path(root)
    if (not isinstance(principal, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', principal)
            or profile != f'app-owner-{principal}-margot'):
        raise PermissionError('Authenticated private-app owner required')
    reads = []

    def read(path, *, private=True, yml=False):
        raw = protected_bytes(path, private=private, anchor=root)
        reads.append((path, private, raw))
        return yaml.safe_load(raw) if yml else json.loads(raw)

    home = root / 'profiles' / profile
    config = read(home / 'config.yaml', private=False, yml=True)
    marker = config.get('app_private_board_control', {})
    if (type(marker) is not dict or set(marker) != {'enabled', 'principal', 'board', 'scope_contract'}
            or marker.get('enabled') is not True or marker.get('principal') != principal
            or marker.get('scope_contract') != CONTRACT):
        raise PermissionError('Private board controls are not enabled for this profile')
    board = marker['board']
    if not isinstance(board, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', board):
        raise PermissionError('Private enrolled board required')
    expected_tools = {'owner_context', TOOLSET}
    if (set(config.get('toolsets', [])) != expected_tools
            or set(config.get('platform_toolsets', {}).get('api_server', [])) != expected_tools):
        raise PermissionError('Private board catalog must preserve the context reader only')
    api = config.get('platforms', {}).get('api_server', {})
    extra = api.get('extra', {})
    if (api.get('enabled') is not True or extra.get('cockpit_principal') != principal
            or extra.get('host') != '127.0.0.1' or type(extra.get('port')) is not int
            or not 1024 <= extra['port'] <= 65535 or config.get('gateway', {}).get('multiplex_profiles') is not False):
        raise PermissionError('Dedicated authenticated private listener required')
    base = root / 'private' / 'phone-assistant'
    backends = read(base / 'app-private-backends.json')
    rows = [b for b in backends.get('bindings', []) if b.get('owner') == principal]
    if backends.get('version') != 1 or len(rows) != 1:
        raise PermissionError('Unique private backend binding required')
    binding = rows[0]
    if (binding.get('enabled') is not True or binding.get('domain') != 'owner_private'
            or binding.get('agent') != 'margot' or binding.get('profile') != profile
            or binding.get('url') != f"http://127.0.0.1:{extra['port']}"
            or type(binding.get('grantVersion')) is not int or binding['grantVersion'] < 1):
        raise PermissionError('Current private backend binding required')
    context_path = base / f'owner-context-{principal}.json'
    context = read(context_path)
    consumer = dict(owner=principal, domain='owner_private', agent='margot',
                    profile=profile, audience='margot-app', grantVersion=binding['grantVersion'])
    if (context.get('enabled') is not True or context.get('consumer') != consumer
            or context.get('grant_ref') != binding.get('grantRef')):
        raise PermissionError('Matching private memory consumer required')
    servers = config.get('mcp_servers', {})
    if set(servers) != {'owner_context'}:
        raise PermissionError('Private memory reader must remain isolated')
    reader = servers['owner_context']
    if (not isinstance(reader, dict) or reader.get('enabled', True) is not True
            or str(context_path) not in reader.get('args', [])):
        raise PermissionError('Existing private memory reader required')
    works = read(base / 'app-private-work-bindings.json')
    rows = [w for w in works.get('bindings', []) if w.get('owner') == principal]
    if works.get('version') != 1 or len(rows) != 1:
        raise PermissionError('Unique private work binding required')
    work = rows[0]
    if (work.get('enabled') is not True or work.get('domain') != 'owner_private'
            or work.get('agent') != 'margot' or work.get('profile') != profile
            or work.get('privateGrantVersion') != binding['grantVersion']):
        raise PermissionError('Current private work binding required')
    grants = read(base / 'app-work-grants.json')
    grant = grants.get(work.get('workGrantRef'), {})
    expiry = grant.get('expires_at')
    if (grant.get('enabled') is not True or grant.get('revoked_at')
            or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()
            or grant.get('owner') != principal or grant.get('profile_key') != principal
            or grant.get('profile') != profile or grant.get('domain') != 'owner_private'
            or grant.get('agent') != 'margot' or grant.get('audience') != 'margot-app'
            or grant.get('version') != work.get('workGrantVersion')
            or grant.get('private_grant_ref') != binding.get('grantRef')
            or grant.get('private_grant_version') != binding['grantVersion']
            or grant.get('realm') != 'owner:' + principal or grant.get('board') != board):
        raise PermissionError('Current private work authority required')
    profiles = read(base / 'caller-profiles.json')
    rows = [p for p in profiles.get('profiles', []) if p.get('profile_key') == principal]
    if len(rows) != 1:
        raise PermissionError('Current private caller enrollment required')
    caller = rows[0]
    if (caller.get('enabled', True) is not True or caller.get('revoked_at') or caller.get('revoked')
            or caller.get('subject_key') != grant.get('subject_key') or caller.get('board') != board
            or grant['realm'] not in caller.get('allowed_realms', [])
            or (grant.get('account') is not None and grant['account'] not in caller.get('allowed_accounts', []))):
        raise PermissionError('Private caller authority changed')
    owner = enrollment(principal) if enrollment is not None else canonical_enrollment(principal, root)
    if owner.get('owner') != principal or owner.get('profile') != principal or owner.get('board') != board:
        raise PermissionError('Current enrolled owner board required')
    if expiry <= time.time() or any(protected_bytes(path, private=private, anchor=root) != raw
                                   for path, private, raw in reads):
        raise PermissionError('Private authority changed during lookup')
    fingerprint = hashlib.sha256(b''.join(raw for _, _, raw in reads)
                                 + json.dumps(owner, sort_keys=True).encode()).hexdigest()
    return dict(toolsets=sorted(expected_tools), profile=profile, principal=principal, board=board,
                scope_contract=CONTRACT, allowed_boards=[board], fingerprint=fingerprint)

"""Single source of member principals (t_10f94d4e).

Every member guard reads this instead of hardcoding 'joshgarcia'. Written only
by scripts/member_setup/provision_member.py. Members are first-class: inside
their own board they are allowed by default. Raw data stays walled per board.
"""
import json
from pathlib import Path
REGISTRY = Path('/Users/Chris/.hermes/private/members/registry.json')
# Legacy fallback keeps Josh working if the file is unreadable (fail-safe, not fail-open).
_FALLBACK = {'joshgarcia': {'principal': 'joshgarcia', 'short': 'josh', 'board': 'josh-home', 'agent_prefix': 'josh-agent-'}}

def members():
    try:
        from gateway.gus_worker_authority import protected
        protected(REGISTRY, REGISTRY.parent.parent.parent)
        rows = json.loads(REGISTRY.read_text()).get('members') or []
        out = {r['principal']: r for r in rows if isinstance(r, dict) and isinstance(r.get('principal'), str) and isinstance(r.get('board'), str)}
        return out or dict(_FALLBACK)
    except Exception:
        return dict(_FALLBACK)

def boards():
    return {p: r['board'] for p, r in members().items()}

def agent_prefixes():
    return tuple(sorted({r.get('agent_prefix') or 'owner-agent-' for r in members().values()} | {'owner-agent-'}))

def owner_of_profile(name):
    """Member principal that owns an installed profile name, by name only."""
    m = members()
    if name in m:
        return name
    for p, r in m.items():
        if name == r.get('app_profile') or (r.get('agent_prefix') not in (None, 'owner-agent-') and name.startswith(r['agent_prefix'])):
            return p
    return None

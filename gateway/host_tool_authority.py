"""Multiplex API host-tool boundary. Runtime identity only; no model/env grants.

Candidate scope: authenticated API execution contexts. Other ingress requires
separate verification before this change can be called global containment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast


def scope() -> tuple[str | None, str | None] | None:
    from gateway.josh_board_authority import _execution
    return cast(tuple[str | None, str | None] | None, _execution.get())


def remote_required() -> bool:
    current = scope()
    return current is not None and (current[1] != 'chris' or current[0] in {'connor', 'josh', 'joshgarcia'})


def terminal_environment() -> dict[str, str] | None:
    current = scope()
    if current is None or not remote_required():
        return None
    profile, principal = current
    if profile not in {'connor', 'josh', 'joshgarcia'} or principal not in {profile, 'chris'}:
        raise PermissionError('Caller-scoped terminal unavailable')
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    if home.parent.name != 'profiles' or home.name != profile:
        raise PermissionError('Caller-scoped terminal unavailable')
    # Read this exact context-local profile; no merged/default host settings,
    # environment expansion, migration, cached-good fallback, or env mutation.
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP, _terminal_env_value
    from yaml import safe_load
    try:
        raw = safe_load((home / 'config.yaml').read_text(encoding='utf-8'))
        terminal = raw['terminal']
        if not isinstance(terminal, dict) or terminal.get('backend') != 'ssh':
            raise ValueError('remote backend required')
        host = terminal.get('ssh_host')
        if not isinstance(host, str) or not host.strip() or host.strip().lower() in {'localhost', '127.0.0.1', '::1'} or '${' in host:
            raise ValueError('remote host required')
        result = {env: _terminal_env_value(terminal[key])
                  for key, env in TERMINAL_CONFIG_ENV_MAP.items() if key in terminal}
        # Host source synchronization is not an implicit grant to remote callers.
        result['TERMINAL_SSH_SYNC_FILES'] = 'false'
        return result
    except Exception as exc:
        raise PermissionError('Caller-scoped terminal unavailable') from exc


def terminal_cache_key() -> str | None:
    current = scope()
    if current is None:
        return None
    remote = terminal_environment()
    if remote is None:
        return 'caller:chris:host'
    # Identity, profile home and backend config all bind the cache. Profile edits
    # cannot reuse a prior local or different remote environment.
    from hermes_constants import get_hermes_home
    digest = hashlib.sha256(json.dumps([current, str(get_hermes_home()), remote], sort_keys=True).encode()).hexdigest()
    return 'caller:' + digest


# Backend-bound native tools stay useful; host-side helper paths are separately
# withheld until they carry an audited caller-bound implementation. No blanket
# removal of a non-owner's terminal/file/web/task capabilities.
HOST_SIDE_TOOLS = frozenset({
    'process', 'delegate_task', 'cronjob',
    'vision_analyze', 'image_generate', 'video_analyze', 'video_generate',
    'text_to_speech', 'skills_list', 'skill_view', 'skill_manage',
    'session_search', 'memory', 'send_message',
})


def tool_allowed(name: str) -> bool:
    if not remote_required():
        return True
    if name in {'own_work_show', 'own_work_manage', 'own_work_comment', 'own_work_list'}:
        # These handlers resolve a fresh generic enrolled member grant. A
        # legacy member with the normal catalog need not enter the pilot.
        from gateway.member_board_tools import enabled
        return enabled()
    if name in {'own_work_create', 'own_agents_list', 'own_agent_propose', 'own_agent_confirm'}:
        from gateway.owner_delegation import available
        return available()
    # Owner-native handlers already enforce their own enrolled board/device
    # boundary. Admit only the exact current pilot grant, not generic host tools.
    if name in {'josh_task_status', 'owner_computer',
                'owner_secure_intake', 'owner_password_reset'}:
        current = scope()
        if current is None or current[0] != current[1]:
            return False
        try:
            from gateway.josh_pilot_tools import pilot_required, allowed_names
            return pilot_required() and name in allowed_names()
        except (PermissionError, OSError, ValueError):
            return False
    if name in HOST_SIDE_TOOLS or name.startswith('browser_'):
        return False
    if name.startswith('mcp__'):
        # MCP backends are ambient host processes. Only independently scoped
        # GBrain is admitted here; other connectors need explicit scoped review.
        return name.startswith(('mcp__gbrain__', 'mcp__gbrain_business__', 'mcp__gbrain_private__', 'mcp__gbrain_family__'))
    if name in {'terminal', 'read_file', 'write_file', 'patch', 'search_files', 'execute_code'}:
        try:
            return terminal_environment() is not None
        except PermissionError:
            return False
    return name in {'web_search', 'web_extract', 'todo', 'tool_search', 'tool_describe', 'tool_call'}

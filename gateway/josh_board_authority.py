"""Staged generic named-profile board admission; no account grants inferred."""
import os,json,re,time
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
request_principal=ContextVar('verified_api_request_principal',default=None)
_execution=ContextVar('verified_board_execution',default=None)
_binding=ContextVar('verified_board_binding',default=None)
_app_binding=ContextVar('verified_app_business_binding',default=None)
LEGACY=frozenset(('default','chris','connor'))
def _current(profile):
    from gateway.owner_work_enrollment import current
    return current(profile)
def restricted():
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home())
    scope=_execution.get()
    if scope is not None:
        profile,principal=scope
        return bool((profile and profile not in LEGACY) or (principal and principal not in LEGACY))
    if home.parent.name=='profiles' and home.name not in LEGACY:
        from gateway.gus_worker_authority import current
        return current() is None
    return False
def app_binding():
    bound=_app_binding.get()
    if bound is None:return None
    if bound.get('scope_contract') == 'margot-owner-private-v1':
        from gateway.app_private_board_authority import current
    else:
        from gateway.app_business_authority import current
    fresh=current(bound['profile'],bound['principal'])
    if fresh!=bound:raise PermissionError('App business enrollment changed')
    return fresh

def app_tool_allowed(name):
    bound=app_binding()
    if bound is None:return True
    from model_tools import resolve_toolset
    allowed=set().union(*(set(resolve_toolset(t)) for t in bound['toolsets']))
    # Deferred tools are still resolved against the same current catalog.
    return name in allowed or name in {'tool_search','tool_describe','tool_call'}

@contextmanager
def execution_scope(profile,principal):
    binding=None;app=None
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home())
    installed_profile=profile or (home.name if home.parent.name=='profiles' else None)
    if installed_profile and installed_profile not in LEGACY and installed_profile!=principal:
        from gateway.app_private_board_authority import private_profile
        if private_profile(installed_profile):
            from gateway.app_private_board_authority import current
        else:
            from gateway.app_business_authority import current
        app=current(installed_profile,principal)
    elif profile and profile not in LEGACY:
        if principal!=profile:raise PermissionError('Authenticated named-profile authority required')
        binding=_current(profile)
    token=_execution.set((profile,principal));bt=_binding.set(binding);at=_app_binding.set(app)
    try:yield
    finally:_app_binding.reset(at);_binding.reset(bt);_execution.reset(token)
def admitted_board(board):
    from gateway.member_worker_policy import authenticated_private_policy
    private=authenticated_private_policy()
    if private is not None:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():raise PermissionError('Private children cannot control the native board')
        if board is not None and board!=private['board']:raise PermissionError('Board outside admitted private task')
        return private['board']
    app=app_binding()
    if app is not None:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():raise PermissionError("Company child must use its native assigned task")
        requested=board or app["board"]
        if requested not in app["allowed_boards"]:raise PermissionError("Board outside current organization grant")
        return requested
    if _execution.get() is None:
        from gateway.gus_worker_authority import current
        worker=current()
        if worker is not None:
            from agent.delegation_context import is_delegated_child_context
            if is_delegated_child_context():raise PermissionError('Leaf workers cannot control the native board')
            if board is not None and board!=worker['board']:raise PermissionError('Board outside assigned GUS task')
            return worker['board']
    if not restricted():return board
    from agent.delegation_context import is_delegated_child_context
    if is_delegated_child_context():raise PermissionError('Delegated owner-work authority not granted')
    scope=_execution.get();binding=_binding.get()
    if scope is None and binding is None:
        from gateway.native_owner_board import current as native_current
        current=native_current()
        if board is not None and board!=current['board']:raise PermissionError('Board outside authorized lane')
        return current['board']
    if scope is None or binding is None or scope[0]!=scope[1]:raise PermissionError('Owner-work authority missing')
    current=_current(scope[0])
    if current!=binding:raise PermissionError('Owner-work enrollment changed')
    expected=current['board']
    if board is not None and board!=expected:raise PermissionError('Board outside authorized lane')
    return expected

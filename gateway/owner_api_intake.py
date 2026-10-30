"""Server-only native run intake registration. Disabled without operator composition."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from types import SimpleNamespace
_ACTIVE=ContextVar('native_owner_intake_tool',default=None)
TOOL='owner_secure_intake'

def active():return _ACTIVE.get() is not None

def invoke(args,**kwargs):
    service=_ACTIVE.get()
    if service is None:raise PermissionError('Current native intake required')
    return json.dumps(service.invoke(args))

def register():
    if not active():return
    from plugins.platforms.photon.secure_intake.trusted_intake import SCHEMA
    from tools.registry import registry
    registry.register(name=TOOL,toolset='josh_status_pilot',schema=dict(SCHEMA,name=TOOL),handler=invoke)

@contextmanager
def native_scope(adapter,request,profile,session,run,message):
    token=_ACTIVE.set(None)
    try:
        locator=request.headers.get('X-Hermes-Intake-Receipt')
        if locator is None:
            from gateway.owner_delegation import bind
            with bind(profile, request.get('hermes.authenticated_principal'), session, run, message):
                yield
            return
        # The native handler has authenticated this request and allocated run.
        # An opaque locator alone cannot activate any tool/provider execution.
        if request.get('hermes.authenticated_principal')!=profile:
            raise PermissionError('Owner intake is not configured')
        from gateway.owner_api_intake_factory import build
        binding=build(profile,adapter._port)
        if not session or not run or run not in adapter._run_statuses:
            raise PermissionError('Native run required')
        service,verifier,endpoint=binding
        if endpoint!=f'http://127.0.0.1:{adapter._port}/p/{profile}/v1/runs':
            raise PermissionError('Native endpoint mismatch')
        with verifier.bind(request,native_profile=profile,native_endpoint=endpoint,native_session=session,native_run=run):
            with service.executor(request,message):
                _ACTIVE.set(service)
                claim = service.current_claim()
                from gateway.owner_delegation import bind
                with bind(profile, request.get('hermes.authenticated_principal'), session, run, message,
                          claim=claim, revalidate=service.current_claim):
                    yield
    finally:_ACTIVE.reset(token)

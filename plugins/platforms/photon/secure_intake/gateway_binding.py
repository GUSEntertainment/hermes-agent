"""Staged Photon ingress binding. Not registered or enabled in the gateway.

Registry resolves an authorized intake request by exact server session/message;
its implementation is still required. No request parsing or model identity input.
"""
from .errors import Denied


def current_identity():
    from gateway.session_context import get_bound_session_env
    names = {'platform':'PLATFORM','chat_type':'CHAT_TYPE','conversation':'CHAT_ID',
             'sender':'USER_ID','message':'MESSAGE_ID','session':'ID'}
    return {k:get_bound_session_env('HERMES_SESSION_'+v) for k,v in names.items()}


class GatewayBinding:
    def __init__(self, *, registry, enrollment, delegated):
        # Trusted server composition callbacks; none are model tool arguments.
        self.registry,self.enrollment,self.delegated=registry,enrollment,delegated

    def __call__(self, context_handle):
        if context_handle is not None or self.delegated():
            raise Denied('Current private request required')
        i=current_identity()
        if i['platform']!='photon' or i['chat_type']!='dm' or any(not isinstance(v,str) or not v or len(v)>256 for v in i.values()):
            raise Denied('Current private request required')
        p=self.enrollment(i.copy())
        if not isinstance(p,dict) or p.get('enabled') is not True or p.get('revoked') is not False:
            raise Denied('Current enrollment required')
        if any(type(p.get(k)) is not str or not p[k] or len(p[k])>200 for k in ('owner','tenant')):
            raise Denied('Verified owner required')
        if p.get('sender')!=i['sender'] or p.get('conversation')!=i['conversation']:
            raise Denied('Enrollment mismatch')
        r=self.registry(i.copy())
        if not isinstance(r,dict) or r.get('identity')!=i:
            raise Denied('Registered request required')
        b=r.get('binding')
        if not isinstance(b,dict) or any(b.get(k)!=p.get(k) for k in ('owner','tenant')):
            raise Denied('Registered owner mismatch')
        if b.get('sender')!=i['sender'] or b.get('conversation')!=i['conversation'] or b.get('channel')!='photon':
            raise Denied('Registered route mismatch')
        return b.copy()

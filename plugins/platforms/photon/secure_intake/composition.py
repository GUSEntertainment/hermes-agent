"""Protected operator startup only. No model-supplied configuration."""
import json
import os
import pathlib
import threading
from .paths import private_file,private_directory,code_file
from .request_registry import RequestRegistry
from .trusted_intake import Publisher,capture_event,bind_event
from .node_service import NodeService
from .canonical_enrollment import canonical_enrollment
from .enrollment_guard import EnrollmentGuard
from agent.delegation_context import is_delegated_child_context

_lock=threading.RLock()
_state=None
REQUIRED={'version','enabled','registry_file','form_directory','node','origin','python','secure_core'}

def load_config(file):
    private_file(file);raw=pathlib.Path(file).read_bytes()
    if len(raw)>8192:raise ValueError('Invalid config')
    c=json.loads(raw)
    if type(c) is not dict or set(c)!=REQUIRED or c['version']!=1 or c['enabled'] is not True:raise ValueError('Inactive config')
    private_file(c['registry_file']);private_directory(c['form_directory'])
    code_file(c['node']);code_file(c['python'],True);code_file(c['secure_core'])
    return c

def start():
    global _state
    with _lock:
        try:
            file=os.environ.get('HERMES_PHOTON_INTAKE_CONFIG')
            if not file:raise ValueError('Unconfigured')
            c=load_config(file)
            if _state and _state[0]==c:return _state
            _state=None
            r=RequestRegistry(c['registry_file'])
            p=Publisher(r,enrollment=canonical_enrollment,delegated=is_delegated_child_context)
            EnrollmentGuard(c['registry_file'],enrollment=canonical_enrollment).sweep()
            service=NodeService(registry_file=c['registry_file'],form_directory=c['form_directory'],node=c['node'],origin=c['origin'],secure_core=c['secure_core'])
            _state=(c,p,service);return _state
        except Exception:_state=None;return None

def capture(event,content):capture_event(event,content)
def bind(event):
    state=start()
    try:return bind_event(event,state[1] if state else None)
    except Exception:return False

def invoke(args,**kwargs):
    state=start()
    if state is None:return {'state':'unavailable','text':'The secure form connection is not ready yet.'}
    return state[1].run(args,state[2])

def available():return start() is not None

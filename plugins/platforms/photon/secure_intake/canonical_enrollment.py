"""Read current enrollment in an isolated, operator-pinned runtime.

The gateway may already have older phone modules in sys.modules. Never reuse
those imports, mutate the gateway import path, or accept runtime paths from a
model. Only the nonsecret enrollment projection crosses this process boundary.
"""
import json
import re
import os
from pathlib import Path
import subprocess
import sys

PINNED=Path('/Users/Chris/.hermes/phone-runtimes/live-20260923-v29-phone-pin/processors')
RUNTIME=Path(__file__).resolve().parents[4]
PYTHON=Path('/Users/Chris/.hermes/releases/hermes-agent-20260904-v0210-eea312f8537b/.venv/bin/python')
TIMEOUT_SECONDS=2

# Load only this fixed local package. Importing plugins.platforms.photon would
# execute the gateway adapter's __init__ in installed v7, unlike a bare overlay.
# A private namespace also avoids regular packages installed by venv .pth files.
if __name__ == '__main__':
    import importlib.util
    import importlib
    local=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location('_secure_enrollment_local',local/'__init__.py',submodule_search_locations=[str(local)])
    package=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=package
    spec.loader.exec_module(package)
    Denied=importlib.import_module('_secure_enrollment_local.errors').Denied
    guard=importlib.import_module('_secure_enrollment_local.enrollment_guard')
    fingerprint,protected_file,EnrollmentRevoked=guard.fingerprint,guard.protected_file,guard.EnrollmentRevoked
else:
    from .errors import Denied
    from .enrollment_guard import fingerprint,protected_file,EnrollmentRevoked

def _verify_runtime_modules():
    # Include transitive phone_state/phone_work imports, not only the entrypoint.
    for name,module in tuple(sys.modules.items()):
        expected=PINNED/(name+'.py')
        if '.' not in name and expected.is_file():
            actual=getattr(module,'__file__',None)
            if not actual or Path(actual).resolve()!=expected:
                raise Denied('Unverified enrollment implementation')
    for name in ('photon_text_continuation','phone_conversation_auth','caller_acl'):
        module=sys.modules.get(name)
        if not module or Path(module.__file__).resolve()!=PINNED/(name+'.py'):
            raise Denied('Unverified enrollment implementation')

def transport_identity_valid(identity):
    fields={'platform','chat_type','conversation','sender','message'}
    if (type(identity) is not dict or set(identity)!=fields
        or any(type(v) is not str or not v or len(v)>200 for v in identity.values())
        or identity['platform']!='photon' or identity['chat_type']!='dm'):
        raise Denied('Verified transport required')

def canonical_transport_enrollment(identity):
    transport_identity_valid(identity)
    return _canonical_enrollment(identity,transport=True)

def canonical_enrollment(identity):
    return _canonical_enrollment(identity,transport=False)

def _canonical_enrollment(identity,*,transport):
    if os.getenv('PHONE_CALLER_PROFILES_PATH') or os.getenv('PHONE_CALLER_REGISTRY_PATH'):
        raise Denied('Unexpected enrollment override')
    try:
        payload=json.dumps(identity,ensure_ascii=True)
        if len(payload)>4096:raise ValueError()
        # Keep the venv executable path: resolving its symlink loses the venv.
        result=subprocess.run([str(PYTHON),'-I',str(Path(__file__).resolve())]+(['--transport'] if transport else []),
            input=payload,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,check=False,
            env={'PATH':'/usr/bin:/bin','PYTHONIOENCODING':'utf-8'})
        if result.returncode or len(result.stdout)>4096:raise ValueError()
        reply=json.loads(result.stdout)
        if reply=={'status':'revoked'}:raise EnrollmentRevoked('Enrollment changed')
        if type(reply) is not dict or set(reply)!={'status','enrollment'} or reply['status']!='ok':raise ValueError()
        value=reply['enrollment']
        if (type(value) is not dict or set(value)!={'generation','owner','tenant','sender','conversation','enabled','revoked'}
            or value['enabled'] is not True or value['revoked'] is not False
            or (type(value['owner'])!=str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value['owner'])) or value['tenant']!='gus'
            or value['sender']!=identity['sender'] or value['conversation']!=identity['conversation']
            or not isinstance(value['generation'],str) or len(value['generation'])!=64
            or any(c not in '0123456789abcdef' for c in value['generation'])):raise ValueError()
        return value
    except EnrollmentRevoked:raise
    except Exception:raise Denied('Enrollment verification unavailable') from None

def _read_canonical(identity):
    import photon_text_continuation as continuation
    if Path(continuation.__file__).resolve()!=PINNED/'photon_text_continuation.py':raise Denied('Unverified enrollment implementation')
    import phone_conversation_auth,caller_acl
    _verify_runtime_modules()
    # Production sources must remain private; no draft/template fallback.
    profile_path=Path(phone_conversation_auth.DEFAULT_PROFILES)
    registry_path=Path(caller_acl.PRIVATE_REGISTRY)
    import os
    if os.getenv('PHONE_CALLER_PROFILES_PATH') or os.getenv('PHONE_CALLER_REGISTRY_PATH'):raise Denied('Unexpected enrollment override')
    before=[protected_file(p) for p in (profile_path,registry_path)]
    custody_binding=None
    revalidate_custody=None
    try:p=continuation._profile({'platform':identity['platform'],'chat_type':identity['chat_type'],'space':identity['conversation'],'sender':identity['sender'],'message_id':identity['message']})
    except continuation.AdmissionDenied:
        profiles=phone_conversation_auth.load_profiles()
        # Legacy denials retain their old revocation semantics; this extension
        # does not repair/reassign either existing owner's permissions.
        if any(p.get('phone_e164')==identity['sender'] and p.get('profile_key') in {'chris','connor'} for p in profiles):
            raise EnrollmentRevoked('Enrollment changed')
        # New owner admission is explicit and isolated; legacy accepted paths
        # keep the exact original projection/generation, including current ACL.
        if __name__=='__main__':
            from importlib import import_module
            resolve_explicit=import_module('_secure_enrollment_local.explicit_enrollment').resolve_explicit
        else:
            from .explicit_enrollment import resolve_explicit
        p,_,custody_binding,revalidate_custody=resolve_explicit(identity,profiles,caller_acl.load_registry(registry_path))
    # Actual _profile validates current caller ACL subject, board and private
    # destination. Tenant is explicit single-company operator configuration.
    acl=caller_acl.lookup_caller(p['phone_e164'])
    after=[protected_file(p) for p in (profile_path,registry_path)]
    if any((a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns)!=(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns) for a,b in zip(before,after)):raise Denied('Enrollment changed while reading')
    if revalidate_custody is not None:revalidate_custody()
    projection={k:p.get(k) for k in ('profile_key','phone_e164','subject_key','board','return_surface','enabled','revoked_at','allowed_realms','allowed_accounts')}
    projection['acl']={k:acl.get(k) for k in ('known','role','scopes')}
    if custody_binding is not None:projection['custody_enrollment']=custody_binding
    # PIN hashes/auth blobs are deliberately absent from this projection.
    return {'generation':fingerprint(projection),'owner':p['profile_key'],'tenant':'gus','sender':p['phone_e164'],'conversation':'any;-;'+p['phone_e164'],'enabled':True,'revoked':False}

if __name__ == '__main__':
    import contextlib
    answer={'status':'unavailable'}
    try:
        data=sys.stdin.read(4097)
        if len(data)>4096:raise ValueError()
        identity=json.loads(data)
        identity_valid=importlib.import_module('_secure_enrollment_local.request_registry').identity_valid
        if sys.argv[1:]==['--transport']:
            transport_identity_valid(identity)
        elif not sys.argv[1:]:
            identity_valid(identity)
        else:raise Denied('Unsupported enrollment entrypoint')
        sys.path.insert(0,str(RUNTIME))
        sys.path.insert(0,str(PINNED))
        with open(os.devnull,'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            value=_read_canonical(identity)
        answer={'status':'ok','enrollment':value}
    except EnrollmentRevoked:answer={'status':'revoked'}
    except Exception:pass
    print(json.dumps(answer,separators=(',',':')))

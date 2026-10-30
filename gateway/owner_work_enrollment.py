"""Read existing custody + canonical profile/ACL for native API authority.
No new enrollment store and no synthetic transport message/session/run.
"""
import json,subprocess,sys,os,re,hashlib
from pathlib import Path
# Fixed maintained runtime; the former v25 path was archived and no longer exists.
PINNED=Path('/Users/Chris/.hermes/phone-runtimes/live-20260927-v41-business-calls/processors')
PYTHON=Path('/Users/Chris/.hermes/releases/hermes-agent-20260904-v0210-eea312f8537b/.venv/bin/python')
CHECK_TIMEOUT_SECONDS=10

# Per-profile, success-only memo. One agent construction asks this ~38 times
# (each a fresh isolated interpreter, ~0.25 s) which made named-owner turns
# take 10-35 s to prepare while Chris's legacy lane took 1-2 s. A verified
# answer is reused for at most MEMO_TTL_SECONDS for the SAME profile only;
# failures are never memoized, so revocation is seen within that window and
# denial is immediate. Keys are profiles, values are re-checked owner==profile.
import threading as _threading, time as _time
MEMO_TTL_SECONDS=2.0
_memo={}
_memo_lock=_threading.Lock()

def _clear_memo():
    with _memo_lock:_memo.clear()

def current(profile):
    if type(profile)!=str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',profile):raise PermissionError('Canonical profile required')
    now=_time.monotonic()
    with _memo_lock:
        hit=_memo.get(profile)
        if hit is not None and now-hit[0]<MEMO_TTL_SECONDS and hit[1]['owner']==profile and hit[1]['profile']==profile:
            return dict(hit[1])
        _memo.pop(profile,None)
    value=_check(profile)
    with _memo_lock:_memo[profile]=(_time.monotonic(),dict(value))
    return value

def _check(profile):
    r=subprocess.run([str(PYTHON),'-I',str(Path(__file__).resolve()),profile],capture_output=True,text=True,timeout=CHECK_TIMEOUT_SECONDS,env={'PATH':'/usr/bin:/bin','PYTHONIOENCODING':'utf-8'})
    if r.returncode or len(r.stdout)>2048:raise PermissionError('Owner enrollment unavailable')
    value=json.loads(r.stdout)
    if set(value)!={'owner','profile','board','fingerprint'} or value['owner']!=profile or value['profile']!=profile or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value['board']) or not re.fullmatch('[a-f0-9]{64}',value['fingerprint']):raise PermissionError('Owner enrollment unavailable')
    return value

def read_current(profile,*,bindings_path=None,profiles=None,registry=None):
    # Internal isolated source seam, never callable from model tool arguments.
    from plugins.platforms.photon.secure_intake.explicit_enrollment import read_bindings,resolve_explicit,ENROLLMENT_FILE
    import phone_conversation_auth,caller_acl
    from plugins.platforms.photon.secure_intake.enrollment_guard import protected_file
    sources=[] if profiles is not None and registry is not None else [Path(phone_conversation_auth.DEFAULT_PROFILES),Path(caller_acl.PRIVATE_REGISTRY)]
    sig=lambda st:(st.st_dev,st.st_ino,st.st_mode,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    before=[sig(protected_file(p)) for p in sources]
    path=bindings_path or ENROLLMENT_FILE
    rows,raw=read_bindings(path);matches=[b for b in rows if b['profile']==profile]
    if len(matches)!=1 or not matches[0]['enabled']:raise PermissionError('Current custody enrollment required')
    b=matches[0];ps=profiles() if profiles else phone_conversation_auth.load_profiles();rs=registry() if registry else caller_acl.load_registry(caller_acl.PRIVATE_REGISTRY)
    matches=[p for p in ps if p.get('profile_key')==profile]
    if len(matches)!=1:raise PermissionError('Unique canonical profile required')
    sender=matches[0]['phone_e164']
    # resolve_explicit checks enrollment/phone/profile/ACL consistency; these
    # enrollment source descriptors do not claim an inbound message occurred.
    p,acl,resolved,revalidate=resolve_explicit({'platform':'photon','chat_type':'dm','sender':sender,'conversation':'any;-;'+sender},ps,rs,path=path)
    if resolved!=b:raise PermissionError('Binding changed')
    again=profiles() if profiles else phone_conversation_auth.load_profiles();regagain=registry() if registry else caller_acl.load_registry(caller_acl.PRIVATE_REGISTRY)
    if again!=ps or regagain!=rs or read_bindings(path)[1]!=raw:raise PermissionError('Enrollment sources changed')
    revalidate()
    if before!=[sig(protected_file(p)) for p in sources]:raise PermissionError("Enrollment source replaced")
    fingerprint=hashlib.sha256(json.dumps({'binding':b,'profile':p,'acl':acl},sort_keys=True,default=str).encode()).hexdigest()
    return {'owner':b['owner'],'profile':profile,'board':b['board'],'fingerprint':fingerprint}
if __name__=='__main__':
    try:
        runtime=Path(__file__).resolve().parents[1]
        sys.path.insert(0,str(runtime));sys.path.insert(0,str(PINNED))
        import contextlib
        with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):value=read_current(sys.argv[1])
        print(json.dumps(value))
    except Exception:raise SystemExit(1)

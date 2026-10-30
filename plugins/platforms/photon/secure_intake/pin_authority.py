"""Isolated canonical PIN metadata, same protected explicit enrollment source."""
import json,subprocess,sys
from pathlib import Path
RUNTIME=Path('/Users/Chris/.hermes/releases/hermes-agent-20260923-phone-pin-v14')
PINNED=Path('/Users/Chris/.hermes/phone-runtimes/live-20260923-v29-phone-pin/processors')
PYTHON=Path('/Users/Chris/.hermes/releases/hermes-agent-20260904-v0210-eea312f8537b/.venv/bin/python')
def current(identity):
 r=subprocess.run([str(PYTHON),'-I',str(Path(__file__).resolve())],input=json.dumps(identity),text=True,capture_output=True,timeout=10,env={'PYTHONDONTWRITEBYTECODE':'1'})
 v=json.loads(r.stdout)
 if r.returncode or set(v)!={'subject_key','generation','phone_digest'}:raise PermissionError('PIN enrollment unavailable')
 return v
if __name__=='__main__':
 result={}
 try:
  sys.path.insert(0,str(RUNTIME));sys.path.insert(0,str(PINNED))
  from plugins.platforms.photon.secure_intake.canonical_enrollment import _read_canonical
  from plugins.platforms.photon.secure_intake.request_registry import identity_valid
  from plugins.platforms.photon.secure_intake.explicit_enrollment import resolve_explicit
  import caller_acl,phone_conversation_auth
  raw=sys.stdin.read(4097)
  if len(raw)>4096:raise ValueError()
  i=json.loads(raw);identity_valid(i);_read_canonical(i)
  p,a,b,revalidate=resolve_explicit(i,phone_conversation_auth.load_profiles(),caller_acl.load_registry(caller_acl.PRIVATE_REGISTRY));revalidate()
  result={k:b[k] for k in ('subject_key','generation','phone_digest')}
 except Exception:pass
 print(json.dumps(result))

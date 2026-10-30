"""Fixed served helper. Reads opaque form ID + PIN stdin; outputs status only.
No CLI authority fields. All owner metadata comes from protected existing stores.
"""
import json,os,sqlite3,sys
from pathlib import Path
from pin_commit import submit,Denied,snapshot
RUNTIME=Path('/Users/Chris/.hermes/releases/hermes-agent-20260923-phone-pin-v14')
REGISTRY=None
FORMS=None
PINNED=Path('/Users/Chris/.hermes/phone-runtimes/live-20260923-v29-phone-pin/processors')

def run(body):
 if type(body)!=dict or set(body)!={'id','pin','confirmation'}:raise Denied('Invalid submission')
 import re
 if type(body['id'])!=str or not re.fullmatch('[a-f0-9-]{36}',body['id']):raise Denied('Invalid form')
 global REGISTRY,FORMS
 if REGISTRY is None:
  # Existing protected operator configuration, never request fields.
  sys.path.insert(0,str(RUNTIME))
  from plugins.platforms.photon.secure_intake.composition import load_config
  config=load_config(os.environ['HERMES_PHOTON_INTAKE_CONFIG'])
  REGISTRY=Path(config['registry_file']);FORMS=Path(config['form_directory'])
 snapshot(REGISTRY);snapshot(FORMS/'.reservations.sqlite')
 with sqlite3.connect('file:'+str(FORMS/'.reservations.sqlite')+'?mode=ro',uri=True) as db:
  db.row_factory=sqlite3.Row;row=db.execute('SELECT * FROM secure_reservations WHERE id=?',(body['id'],)).fetchone()
 if not row or row['revoked'] or not row['published']:raise Denied('Unavailable form')
 owner,tenant,request,service,purpose,digest,expires=json.loads(row['binding'])
 if purpose!='enroll-phone-pin':raise Denied('Wrong purpose')
 sys.path.insert(0,str(RUNTIME));sys.path.insert(0,str(PINNED))
 import caller_acl,phone_conversation_auth
 from plugins.platforms.photon.secure_intake.canonical_enrollment import _read_canonical
 from plugins.platforms.photon.secure_intake.explicit_enrollment import resolve_explicit
 def current(_):
  snapshot(REGISTRY)
  with sqlite3.connect('file:'+str(REGISTRY)+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;rows=db.execute("SELECT * FROM ingress_requests WHERE json_extract(binding,'$.request')=?",(request,)).fetchall()
   if len(rows)!=1 or rows[0]['revoked']:raise Denied('Request revoked')
   r=rows[0];i=json.loads(r['identity']);b=json.loads(r['binding'])
   encoded=json.dumps([[k,i[k]] for k in sorted(i)],separators=(',',':'))
   import hashlib
   if b['owner']!=owner or b['tenant']!=tenant or b['purpose']!=purpose or int(b['expires']*1000)!=expires or hashlib.sha256(encoded.encode()).hexdigest()!=digest:raise Denied('Binding changed')
   return b
 def canonical(b):
  with sqlite3.connect('file:'+str(REGISTRY)+'?mode=ro',uri=True) as db:
   r=db.execute("SELECT identity FROM ingress_requests WHERE json_extract(binding,'$.request')=?",(request,)).fetchone();i=json.loads(r[0]);g=db.execute('SELECT generation FROM intake_enrollment_grants WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
  fresh=_read_canonical(i)
  if not g or fresh['generation']!=g[0]:raise Denied('Enrollment changed')
  p,acl,e,revalidate=resolve_explicit(i,phone_conversation_auth.load_profiles(),caller_acl.load_registry(caller_acl.PRIVATE_REGISTRY));revalidate()
  return {'owner':e['owner'],'subject_key':e['subject_key'],'generation':e['generation'],'phone_digest':e['phone_digest'],'phone':p['phone_e164'],'enabled':True,'registry_role':e['registry_role']}
 return submit(caller_acl.PRIVATE_REGISTRY,request_id=request,pin=body['pin'],confirmation=body['confirmation'],current_request=current,canonical=canonical,acl=caller_acl)
if __name__=='__main__':
 result={'success':False}
 try:
  raw=sys.stdin.read(2049)
  if len(raw)>2048:raise Denied('Too large')
  result=run(json.loads(raw))
 except Exception:pass
 print(json.dumps(result))

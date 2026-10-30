"""Inactive hash-only commit seam. Callbacks are trusted server composition only.
No model tool / CLI; existing secure reservation must supply current binding.
Legacy phone writers must adopt this lock before production activation.
"""
import math,re
import fcntl, hashlib, json, os, stat, tempfile, time
from pathlib import Path
PURPOSE='enroll-phone-pin'
class Denied(Exception): pass

def snapshot(path):
 path=Path(path)
 if not path.is_absolute() or path.resolve(strict=True)!=path:raise Denied('Unsafe registry')
 s=path.lstat();p=path.parent.stat()
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_nlink!=1 or p.st_uid!=os.getuid() or p.st_mode&0o022:raise Denied('Unsafe registry')
 raw=path.read_bytes()
 sig=lambda x:(x.st_dev,x.st_ino,x.st_uid,x.st_mode,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
 if sig(path.lstat())!=sig(s):raise Denied('Changed registry')
 return raw

def submit(path, *, request_id, pin, confirmation, current_request, canonical, acl):
 # Caller cannot pass an owner or phone. Both originate in live authority reads.
 if not isinstance(pin,str) or pin!=confirmation or not 6<=len(pin)<=64:raise Denied('PIN must match and contain 6–64 characters')
 if type(request_id)!=str or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}',request_id):raise Denied('Invalid request')
 path=Path(path);snapshot(path)
 lock=path.with_name(path.name+'.lock');fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  s=os.fstat(fd)
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_nlink!=1:raise Denied('Unsafe lock')
  fcntl.flock(fd,fcntl.LOCK_EX)
  if lock.lstat().st_ino!=s.st_ino:raise Denied('Changed lock')
  raw=snapshot(path);data=json.loads(raw)
  def authority():
   b=current_request(request_id)
   if b.get('request')!=request_id or b.get('purpose')!=PURPOSE or b.get('authorized') is not True or b.get('expires',0)<=time.time():raise Denied('Current PIN request required')
   if (type(b.get('owner'))!=str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',b['owner']) or type(b.get('subject_key'))!=str or not 1<=len(b['subject_key'])<=200 or type(b.get('generation'))!=int or b['generation']<1 or type(b.get('expires')) not in (int,float) or not math.isfinite(b['expires'])):raise Denied('Exact binding required')
   c=canonical(b)
   if current_request(request_id)!=b or b['expires']<=time.time():raise Denied('Request changed during authority read')
   if type(c.get('registry_role'))!=str or not c['registry_role']:raise Denied('Exact registry role required')
   if c.get('enabled') is not True or any(c.get(k)!=b.get(k) for k in ('owner','subject_key','generation','phone_digest')):raise Denied('Current exact enrollment required')
   phone=c.get('phone')
   if not isinstance(phone,str) or acl.normalize_phone(phone)!=phone or hashlib.sha256(phone.encode()).hexdigest()!=b['phone_digest']:raise Denied('Exact phone required')
   return b,c
  b,c=authority();people=data.get('people',[])
  matches=[x for x in people if c['phone'] in acl._phones_for_person(x)]
  if len(matches)!=1 or matches[0].get('role')!=c.get('registry_role'):raise Denied('Exact registered person required')
  person=matches[0];fingerprint=hashlib.sha256(json.dumps(b,sort_keys=True).encode()).hexdigest()
  receipts=person.get('pin_enrollment_receipts',{})
  if not isinstance(receipts,dict):raise Denied('Invalid receipts')
  if request_id in receipts:
   if receipts[request_id]!=fingerprint:raise Denied('Request rebound')
   return {'success':True,'already_completed':True,'current':person.get('phone_auth',{}).get('enrollment_request')==request_id}
  if len(receipts)>=4096:raise Denied('Receipt capacity reached')
  hashed=acl.hash_pin(pin)
  if authority()!=(b,c) or snapshot(path)!=raw:raise Denied('Authority changed')
  person['phone_auth']={**person.get('phone_auth',{}),'enrollment_request':request_id,'pin_hash':hashed,'pin_updated_at':int(time.time()),'method':'secure_pin_enrollment'}
  person['pin_enrollment_receipts']={**receipts,request_id:fingerprint}
  output=(json.dumps(data,indent=2)+'\n').encode();temp=None
  try:
   f,temp=tempfile.mkstemp(prefix='.pin-',dir=path.parent)
   with os.fdopen(f,'wb') as out:out.write(output);out.flush();os.fsync(out.fileno())
   if authority()!=(b,c) or snapshot(path)!=raw:raise Denied('Authority changed')
   os.replace(temp,path);temp=None
   d=os.open(path.parent,os.O_RDONLY)
   try:os.fsync(d)
   finally:os.close(d)
  finally:
   if temp is not None:os.unlink(temp)
  return {'success':True,'already_completed':False,'current':True}
 finally:os.close(fd)

"""Explicit server-owned custody enrollment; no model or role-wide authority."""
import hashlib,json,os,re
from pathlib import Path
from .enrollment_guard import protected_file,EnrollmentRevoked
from .errors import Denied
ENROLLMENT_FILE=Path('/Users/Chris/.hermes/private/photon-intake/custody-enrollments.json')
FIELDS={'enabled','owner','profile','board','tenant','subject_key','registry_role','phone_digest','return_surface','stable_account_id','generation'}

def read_bindings(path=ENROLLMENT_FILE):
    path=Path(path);before=protected_file(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        opened=os.fstat(fd);raw=os.read(fd,8193);after=os.fstat(fd)
    finally:os.close(fd)
    current=protected_file(path)
    sig=lambda s:(s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(raw)>8192 or not sig(before)==sig(opened)==sig(after)==sig(current):raise Denied('Enrollment changed')
    data=json.loads(raw)
    if type(data)!=dict or set(data)!={'version','enrollments'} or (type(data['version'])!=int or data['version']!=1) or type(data['enrollments'])!=list or len(data['enrollments'])>100:
        raise Denied('Explicit custody enrollments required')
    owners=set();subjects=set();phones=set();boards=set();ids=set()
    for b in data['enrollments']:
        if type(b)!=dict or set(b)!=FIELDS:raise Denied('Invalid custody enrollment')
        if (any(type(b[k])!=str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',b[k]) for k in ('owner','profile','board','tenant'))
            or b['tenant']!='gus' or b['profile']!=b['owner'] or b['return_surface']!='photon:profile:'+b['profile']
            or type(b['enabled'])!=bool or type(b['generation'])!=int or b['generation']<1
            or any(type(b[k])!=str or not 1<=len(b[k])<=200 for k in ('subject_key','registry_role','stable_account_id'))
            or type(b['phone_digest'])!=str or not re.fullmatch('[0-9a-f]{64}',b['phone_digest'])):raise Denied('Invalid custody enrollment')
        for field,seen in [('owner',owners),('subject_key',subjects),('phone_digest',phones),('board',boards),('stable_account_id',ids)]:
            if b[field] in seen:raise Denied('Ambiguous custody enrollment')
            seen.add(b[field])
    return data['enrollments'],raw

def resolve_explicit(identity,profiles,registry,*,path=ENROLLMENT_FILE):
    # Inputs are fresh canonical source records, never tool parameters.
    import caller_acl
    if identity.get('platform')!='photon' or identity.get('chat_type')!='dm':raise EnrollmentRevoked('Private source required')
    sender=identity.get('sender')
    if type(sender)!=str or not re.fullmatch(r'\+[1-9]\d{7,14}',sender) or identity.get('conversation')!='any;-;'+sender:
        raise EnrollmentRevoked('Private source required')
    bindings,raw=read_bindings(path)
    matches=[b for b in bindings if b['phone_digest']==hashlib.sha256(sender.encode()).hexdigest()]
    if len(matches)!=1 or not matches[0]['enabled']:raise EnrollmentRevoked('Owner enrollment changed')
    b=matches[0]
    matches=[p for p in profiles if p.get('phone_e164')==sender]
    if len(matches)!=1:raise EnrollmentRevoked('Unique canonical profile required')
    p=matches[0]
    if (p.get('enabled') is not True or p.get('revoked_at')
        or any(p.get(k)!=b[v] for k,v in [('profile_key','profile'),('board','board'),('subject_key','subject_key'),('registry_role','registry_role'),('return_surface','return_surface')])
        or p.get('allowed_realms')!=['owner:'+b['owner']] or p.get('allowed_accounts')!=[]):raise EnrollmentRevoked('Canonical scope changed')
    people=registry.get('people')
    if type(people)!=list:raise Denied('Registry unavailable')
    matches=[r for r in people if type(r)==dict and sender in caller_acl._phones_for_person(r)]
    if len(matches)!=1 or matches[0].get('role')!=b['registry_role']:raise EnrollmentRevoked('Unique registry identity required')
    acl=caller_acl.lookup_caller(sender,registry=registry)
    if not acl.get('known') or acl.get('phone_sha256')!=b['phone_digest']:raise EnrollmentRevoked('Registry identity changed')
    # Deliberately no derivation from display name/role; subject is exact operator pairing.
    def revalidate():
        # Kept through the canonical checker's final ACL read. No captured
        # record is authority after a subsequent I/O boundary.
        if read_bindings(path)[1]!=raw:raise EnrollmentRevoked('Custody enrollment changed during read')
    revalidate()
    return p,acl,b,revalidate

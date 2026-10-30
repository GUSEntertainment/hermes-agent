"""Protected recipient custody only; never execution authority or group identity."""
import contextlib,hashlib,json,math,re,subprocess,sys
from pathlib import Path
ROOT=Path('/Users/Chris/.hermes');POLICY=ROOT/'private/document-delivery/gateway-custody.json'
BUNDLE=ROOT/'context-runtimes/app-work-20260924-v2';BUNDLE_SHA='cc12054bd9a63c2edef2b9caa1556a70804d342ebd58cf26506eacd8e5556afe'
PYTHON=ROOT/'releases/hermes-agent-20260925-api-prewarm-v49/.venv/bin/python'
KIND='gateway-document-custody-v1'
def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def policy_at(root=ROOT):
    from gateway.app_business_authority import protected_bytes
    path=Path(root)/'private/document-delivery/gateway-custody.json'
    if not path.exists() and not path.is_symlink():return None,None
    raw=protected_bytes(path,True,root);p=json.loads(raw)
    if type(p)!=dict or set(p)!={'version','enabled_from','bindings'} or p['version']!=KIND or type(p['enabled_from']) not in (int,float) or not math.isfinite(p['enabled_from']) or p['enabled_from']<=0 or type(p['bindings'])!=list or len(p['bindings'])>100:raise PermissionError('Invalid custody policy')
    seen=set()
    for r in p['bindings']:
        fields={'owner','platform','profile','board','chat_id','thread_id','chat_type','enabled','generation'}
        if type(r)!=dict or set(r)!=fields or type(r['enabled'])!=bool or type(r['generation'])!=int or r['generation']<1:raise PermissionError('Invalid custody binding')
        if any(type(r[k])!=str or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',r[k]) for k in ('owner','profile','board')) or r['platform'] not in ('telegram','photon') or r['chat_type'] not in ('group','dm') or type(r['chat_id'])!=str or not r['chat_id'] or (r['thread_id'] is not None and (type(r['thread_id'])!=str or not r['thread_id'])):raise PermissionError('Invalid custody destination')
        key=tuple(r[k] for k in ('platform','profile','board','chat_id','thread_id','chat_type'))
        if key in seen:raise PermissionError('Ambiguous custody destination')
        seen.add(key)
    return p,raw

def current(binding):
    if isinstance(binding,dict) and binding.get('kind')=='owner-delegation-return-v1':
        from gateway.owner_delegation import custody_authority
        return custody_authority(binding)
    if not isinstance(binding,dict) or binding.get('kind') not in ('gateway-return-v1','gateway-conversation-document-v1'):return None
    p,_=policy_at()
    if p is None:return None
    r=subprocess.run([str(PYTHON),'-I','-B',str(Path(__file__).resolve())],input=encode(binding),capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if r.returncode or len(r.stdout)>4096:raise PermissionError('Document custody unavailable')
    value=json.loads(r.stdout)
    if value is not None and (value.get('kind')!=KIND or value.get('source_digest')!=digest(binding) or value.get('domain')!='recipient_documents'):raise PermissionError('Document custody projection invalid')
    return value

def recipient_profile(owner,module,profiles,registry):
    entries=profiles.get('profiles',[])
    selected=[p for p in entries if p.get('profile_key')==owner]
    if len(selected)!=1:raise PermissionError('Unique current recipient required')
    person=selected[0];phone=person.get('phone_e164','')
    if person.get('enabled',True) is not True or person.get('revoked_at'):raise PermissionError('Recipient revoked')
    normalized=module.caller_acl.normalize_phone(phone)
    matches=[p for p in registry.get('people',[]) if normalized and normalized in module.caller_acl._phones_for_person(p)]
    if len(matches)!=1 or matches[0].get('enabled',True) is not True or matches[0].get('revoked_at'):raise PermissionError('Recipient ACL revoked')
    acl=module.caller_acl.lookup_caller(phone,registry=registry)
    expected_role=person.get('registry_role')
    if owner in ('chris','connor'):
        expected_role={'chris':'chris_owner','connor':'admin_internal'}[owner]
        expected_subject='registry:'+owner+'_owner:'+hashlib.sha256(phone.encode()).hexdigest()
        if person.get('subject_key')!=expected_subject or person.get('board')!=owner+'-home' or person.get('return_surface')!='photon:profile:'+owner:raise PermissionError('Legacy recipient identity changed')
    if not expected_role or not acl.get('known') or acl.get('role')!=expected_role:raise PermissionError('Recipient enrollment changed')
    # PIN material is neither required nor part of document custody identity.
    return person,{k:acl.get(k) for k in ('known','role','scopes','phone_sha256')}

def resolve(binding,module,*,root=ROOT):
    from plugins.platforms.photon.secure_intake.explicit_enrollment import resolve_explicit
    p,raw=policy_at(root)
    if p is None:return None
    if type(binding)!=dict or binding.get('kind') not in ('gateway-return-v1','gateway-conversation-document-v1'):return None
    direct=binding['kind']=='gateway-conversation-document-v1'
    if direct and (type(binding.get('received_at')) not in (int,float) or not math.isfinite(binding['received_at']) or binding['received_at']<p['enabled_from']):raise PermissionError('Direct source predates custody')
    target=binding.get('target')
    if type(target)!=dict or any(type(binding.get(k))!=str or not binding[k] for k in (('profile','session_id','message_id','request_sha256') if direct else ('profile','session_id','board','message_id','request_sha256'))) or type(target.get('user_id'))!=str or not target['user_id']:raise PermissionError('Bound source incomplete')
    rows=[r for r in p['bindings'] if all(r[k]==(binding.get(k) if k in ('profile','board') else target.get(k)) for k in (('platform','profile','chat_id','thread_id','chat_type') if direct else ('platform','profile','board','chat_id','thread_id','chat_type')))]
    if not rows:return None
    if len(rows)!=1:raise PermissionError('Ambiguous recipient custody surface')
    rule=rows[0]
    if not rule['enabled']:raise PermissionError('Recipient custody disabled')
    # Current caller/transport enrollment only; no phone PIN or work grant.
    paths=module.Admission().paths
    profiles=module.private_json(paths.profiles)
    registry=module.private_json(paths.registry)
    entries=profiles.get('profiles',[]);owner=rule['owner']
    person,acl=recipient_profile(owner,module,profiles,registry)
    if rule['platform']=='telegram':
        legacy=person.get('legacy_return_surfaces',[])
        if type(legacy)!=list or any(type(value)!=str for value in legacy):raise PermissionError('Invalid legacy recipient surfaces')
        surfaces=legacy+[person.get('return_surface')]
        surface='telegram:'+target['chat_id']+':'+str(target.get('thread_id') or '')
        if owner!='chris' or binding['profile']!='default' or target['chat_type']!='group' or surface not in surfaces:raise PermissionError('Unapproved private recipient surface')
        transport=dict(profile=person,acl=acl)
    else:
        identity=dict(platform='photon',chat_type=target.get('chat_type'),sender=target['user_id'],conversation=target.get('chat_id'))
        if owner in ('chris','connor'):
            if identity['chat_type']!='dm' or identity['sender']!=person['phone_e164'] or identity['conversation']!='any;-;'+person['phone_e164']:raise PermissionError('Canonical private sender required')
            person2,acl2,enrollment,revalidate=person,acl,None,lambda:None
        else:
            person2,acl2,enrollment,revalidate=resolve_explicit(identity,entries,registry,path=Path(root)/'private/photon-intake/custody-enrollments.json')
        if person2!=person or (enrollment is not None and enrollment['owner']!=owner) or target.get('thread_id') is not None or binding['profile'] not in ('default',owner):raise PermissionError('Canonical private sender required')
        revalidate();transport=dict(profile=person2,acl={k:acl2.get(k) for k in ('known','role','scopes','phone_sha256')},enrollment=enrollment)
    if module.private_json(paths.profiles)!=profiles or module.private_json(paths.registry)!=registry or policy_at(root)[1]!=raw:raise PermissionError('Recipient changed during custody validation')
    return dict(kind=KIND,owner=owner,domain='recipient_documents',realm='recipient:'+owner,board=None if direct else binding['board'],source_digest=digest(binding),policy_generation=rule['generation'],policy_fingerprint=digest(rule),transport_fingerprint=digest(transport),enabled_from=p['enabled_from'])

def load_transport_module():
    from gateway.app_business_authority import protected_bytes
    raw=protected_bytes(BUNDLE/'BUNDLE.json',anchor=ROOT)
    if hashlib.sha256(raw).hexdigest()!=BUNDLE_SHA:raise PermissionError('Private bundle changed')
    manifest=json.loads(raw)
    for name,h in manifest['files'].items():
        if not name.startswith('processors/') or len(Path(name).parts)!=2 or not name.endswith('.py') or hashlib.sha256(protected_bytes(BUNDLE/name,anchor=ROOT)).hexdigest()!=h:raise PermissionError('Private dependency changed')
    if {str(p.relative_to(BUNDLE)) for p in (BUNDLE/'processors').glob('*.py')}!=set(manifest['files']):raise PermissionError('Unexpected private module')
    sys.path.insert(0,str(BUNDLE/'processors'));import phone_app_work
    if Path(phone_app_work.__file__).resolve()!=BUNDLE/'processors/phone_app_work.py':raise PermissionError('Private source module mismatch')
    return phone_app_work

def authority_revision():
    """Fence lookup isolated from the gateway's process-global import state."""
    result=subprocess.run([str(PYTHON),'-I','-B',str(Path(__file__).resolve()),'--revision'],capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if result.returncode or len(result.stdout)>128:raise PermissionError('Recipient source fence unavailable')
    value=json.loads(result.stdout)
    if type(value)!=str or not re.fullmatch('[a-f0-9]{64}',value):raise PermissionError('Invalid recipient source fence')
    return value

def _authority_revision_local():
    """Opaque current source fence; never a grant or a substitute for row checks."""
    from gateway.app_business_authority import protected_bytes
    module=load_transport_module();paths=module.Admission().paths
    _,policy=policy_at()
    explicit=ROOT/'private/photon-intake/custody-enrollments.json'
    sources=[policy,protected_bytes(paths.profiles,True,ROOT),protected_bytes(paths.registry,True,ROOT),protected_bytes(explicit,True,ROOT) if explicit.exists() or explicit.is_symlink() else None]
    return digest([hashlib.sha256(value).hexdigest() if value is not None else None for value in sources])

def main():
    runtime=Path(__file__).resolve().parents[1];sys.path.insert(0,str(runtime))
    if sys.argv[1:]==['--revision']:print(encode(_authority_revision_local()));return
    if sys.argv[1:]:raise PermissionError('Unexpected mode')
    if policy_at()[0] is None:print('null');return
    phone_app_work=load_transport_module()
    raw=sys.stdin.buffer.read(16385)
    if len(raw)>16384:raise PermissionError('Oversize source')
    with open('/dev/null','w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):result=resolve(json.loads(raw),phone_app_work)
    print(encode(result))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit(1)

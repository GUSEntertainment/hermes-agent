"""Read-only extraction of pinned Admission.binding; no synthetic app turn.
Only typed current consumer/grant inputs. Source proof is validated separately.
The pinned legacy bundle remains unchanged; no submit/status operation runs.
"""
import hashlib,json,math,re,time

def current_binding(module, admission, consumer, grant_ref):
    if not isinstance(consumer,dict) or set(consumer)!={'owner','domain','agent','profile','audience','grantVersion'} or not isinstance(grant_ref,str):
        raise PermissionError('Invalid private reader binding')
    e={'consumer':consumer,'grant_ref':grant_ref}
    legacy=getattr(module,'PRIVATE_DOCUMENT_BINDING_VERSION',2)==1
    private_json,Denied,Unavailable,caller_acl,CONTRACT,work,Path=(module.private_json,module.Denied,module.Unavailable,module.caller_acl,module.CONTRACT,module.work,module.Path)
    gs=private_json(admission.paths.grants);g=gs.get(e['grant_ref']) if isinstance(gs,dict) else None
    if not isinstance(g,dict) or g.get('enabled') is not True or g.get('revoked_at'):raise Denied('work access unavailable')
    if type(g.get('expires_at')) not in (int,float) or not math.isfinite(g['expires_at']) or g['expires_at']<=time.time():raise Denied('work access expired')
    owner=g.get('owner');key=g.get('profile_key')
    expected=dict(owner=owner,domain=g.get('domain'),agent=g.get('agent'),profile=g.get('profile'),audience=g.get('audience'),grantVersion=g.get('version'))
    if type(e['consumer'].get('grantVersion')) is not int or e['consumer']!=expected or not isinstance(owner,str) or not re.fullmatch(r'[a-z][a-z0-9._-]{0,39}',owner) or owner!=key or g.get('domain')!='owner_private' or g.get('agent')!='margot' or g.get('audience')!='margot-app' or type(g.get('version')) is not int or g['version']<1:raise Denied('consumer mismatch')
    if legacy and owner not in {'chris','connor'}:raise Denied('consumer mismatch')
    profiles=private_json(admission.paths.profiles).get('profiles',[])
    matches=[p for p in profiles if p.get('profile_key')==key and p.get('subject_key')==g.get('subject_key')]
    if len(matches)!=1:raise Denied('profile unavailable')
    p=matches[0];phone=p.get('phone_e164','')
    subject=p.get('subject_key')
    if not re.fullmatch(r'\+[1-9]\d{7,14}',phone) or (not subject or subject!=g.get('subject_key')) or p.get('enabled',True) is not True or p.get('revoked_at'):raise Denied('profile unavailable')
    if legacy and subject!='registry:'+key+'_owner:'+hashlib.sha256(phone.encode()).hexdigest():raise Denied('profile unavailable')
    registry=private_json(admission.paths.registry)
    if not isinstance(registry,dict) or not registry:raise Denied('enrollment unavailable')
    enrolled=caller_acl.lookup_caller(phone,registry=registry)
    if not enrolled.get('known') or not g.get('registry_role') or enrolled.get('role')!=g['registry_role']:raise Denied('enrollment unavailable')
    expected_subjects={'registry:'+enrolled['role'], 'registry:'+enrolled['role']+':'+hashlib.sha256(phone.encode()).hexdigest()}
    if not legacy and subject not in expected_subjects:raise Denied('enrollment subject mismatch')
    for field in ('allowed_realms','allowed_accounts'):
        values=p.get(field)
        if not isinstance(values,list) or any(not isinstance(v,str) or not v for v in values) or len(set(values))!=len(values):raise Denied('profile grant malformed')
    realm=g.get('realm');account=g.get('account')
    if realm not in p.get('allowed_realms',[]) or realm!='owner:'+owner or (account is not None and account not in p.get('allowed_accounts',[])):raise Denied('scope unavailable')
    if not isinstance(g.get('board'),str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,80}',g['board']) or p.get('board')!=g['board']:raise Denied('board mismatch')
    if legacy and g['board']!=key+'-home':raise Denied('board mismatch')
    surface=g.get('return_surface')
    if legacy and (surface!='photon:profile:'+key or p.get('return_surface')!=surface):raise Unavailable('delivery unavailable')
    channel='app' if surface=='kanban:'+g['board'] else 'photon'
    if channel=='photon' and (surface!='photon:profile:'+key or p.get('return_surface')!=surface):raise Unavailable('delivery unavailable')

    bs=private_json(admission.paths.backends)
    if not isinstance(bs,dict) or bs.get('version')!=1 or not isinstance(bs.get('bindings'),list):raise Denied('backend unavailable')
    rows=bs['bindings']; matches=[b for b in rows if b.get('owner')==owner]
    if len(matches)!=1:raise Denied('backend unavailable')
    b=matches[0]
    if type(g.get('private_grant_version')) is not int or type(b.get('grantVersion')) is not int or b.get('enabled') is not True or b.get('domain')!='owner_private' or b.get('agent')!='margot' or b.get('profile')!=g['profile'] or b.get('grantRef')!=g.get('private_grant_ref') or b.get('grantVersion')!=g.get('private_grant_version') or not b.get('verificationReceipt'):raise Denied('private backend changed')
    # One principal per store/profile/read-grant, no aliases borrowing another owner.
    for field in ('storePath','profile','grantRef'):
        if len({r.get(field) for r in rows})!=len(rows):raise Denied('duplicate backend binding')
    store=Path(b.get('storePath',''))
    if not store.is_absolute() or store.is_symlink():raise Denied('private store unavailable')
    b=dict(b,storePath=str(store.resolve()),url=b['url'].rstrip('/'))
    binding=hashlib.sha256(json.dumps([b['domain'],CONTRACT,owner,b['agent'],b['profile'],b['url'],b['keyEnv'],b['storePath'],b['grantRef'],b['grantVersion']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return g,p,b,binding,work._digest({'grant':g,'profile':p,'backend':b,'registry_role':enrolled['role']})

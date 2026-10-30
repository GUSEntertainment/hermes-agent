"""Candidate trusted app-business binding. Does not grant tools or install routes.

A listener must authenticate its own credential and set request_principal before
calling current(). Config is an operator-owned installed profile file. The same
canonical enrollment resolver used by custody supplies current human authority.
"""
import hashlib,json,os,re,stat
from pathlib import Path
import yaml
ROOT=Path("/Users/Chris/.hermes")

def protected(path, anchor=None):
    path = Path(path)
    anchor = Path(anchor) if anchor is not None else path.parent
    if not path.is_relative_to(anchor) or anchor.resolve(strict=True) != anchor:
        raise PermissionError('Untrusted app authority source root')
    st = anchor.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError('Untrusted app authority source directory')
    directory = anchor
    for part in path.parent.relative_to(anchor).parts:
        directory = directory / part
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise PermissionError('Untrusted app authority source directory')
    st = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_nlink != 1):
        raise PermissionError('Untrusted app business source')
    return st


def protected_bytes(path, private=False, anchor=None):
    before=protected(path,anchor)
    if private and before.st_mode&0o077:raise PermissionError('Private source required')
    if before.st_size>262144:raise PermissionError('Oversize authority source')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    identity=lambda st:(st.st_dev,st.st_ino,st.st_uid,st.st_mode,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    try:
        if identity(os.fstat(fd))!=identity(before):raise PermissionError('Source replaced')
        raw=os.read(fd,262145)
        if identity(os.fstat(fd))!=identity(before):raise PermissionError('Source changed')
    finally:os.close(fd)
    if identity(protected(path,anchor))!=identity(before):raise PermissionError('Source replaced')
    return raw

def canonical_enrollment(principal, root):
    if principal not in ('chris','connor'):
        from gateway.owner_work_enrollment import current as owner_current
        return owner_current(principal)
    profiles_path=root/'private'/'phone-assistant'/'caller-profiles.json'
    acl_path=root/'private'/'phone_acl'/'caller_registry.private.json'
    profiles_raw=protected_bytes(profiles_path,True,root);acl_raw=protected_bytes(acl_path,True,root)
    profiles=json.loads(profiles_raw)['profiles'];people=json.loads(acl_raw)['people']
    candidates=[p for p in profiles if p.get('profile_key')==principal]
    if len(candidates)!=1:raise PermissionError('Unique protected owner enrollment required')
    profile=candidates[0];phone=profile.get('phone_e164')
    if not isinstance(phone,str) or not re.fullmatch(r'\+[1-9][0-9]{7,14}',phone):raise PermissionError('Canonical contact required')
    def phones(person):
        v=person.get('phone_e164') or person.get('phones_e164') or person.get('phone_numbers') or person.get('phones') or []
        return [v] if isinstance(v,str) else v
    matches=[p for p in people if phone in phones(p)]
    role={'chris':'chris_owner','connor':'admin_internal'}[principal]
    expected_subject='registry:'+principal+'_owner:'+hashlib.sha256(phone.encode()).hexdigest()
    if len(matches)!=1 or matches[0].get('role')!=role or matches[0].get('enabled') is False or profile.get('enabled') is False or profile.get('subject_key')!=expected_subject or 'business' not in profile.get('allowed_realms',[]):raise PermissionError('Current protected owner enrollment required')
    if protected_bytes(profiles_path,True,root)!=profiles_raw or protected_bytes(acl_path,True,root)!=acl_raw:raise PermissionError('Owner sources changed')
    return {'owner':principal,'profile':principal,'board':profile['board'],'fingerprint':hashlib.sha256(profiles_raw+acl_raw).hexdigest()}

def current(profile, principal, *, root=None, enrollment=None):
    root=ROOT if root is None else Path(root)
    if not isinstance(profile,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,99}',profile):raise PermissionError('Installed profile required')
    if not isinstance(principal,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',principal):raise PermissionError('Authenticated principal required')
    home=root/'profiles'/profile
    if home.is_symlink() or home.resolve()!=home.absolute():raise PermissionError('Canonical profile home required')
    path=home/'config.yaml'
    raw=protected_bytes(path,anchor=root)
    config=yaml.safe_load(raw)
    row=config.get('app_business_authority') if isinstance(config,dict) else None
    if not isinstance(row,dict) or set(row)!={'principal','board','enabled','scope_contract','allowed_boards'} or row.get('enabled') is not True or row.get('principal')!=principal or row.get('scope_contract')!='gus-company-work-v1':raise PermissionError('Installed app authority required')
    if not isinstance(row.get('board'),str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',row['board']):raise PermissionError('Canonical board required')
    if enrollment is None:enrollment=lambda user:canonical_enrollment(user,root)
    owner=enrollment(principal)
    if owner.get('owner')!=principal or owner.get('profile')!=principal or owner.get('board')!=row['board']:raise PermissionError('Current member enrollment mismatch')
    boards=row['allowed_boards']
    if not isinstance(boards,list) or not boards or len(boards)!=len(set(boards)) or any(not isinstance(b,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',b) for b in boards) or row['board'] not in boards:raise PermissionError('Explicit organization board grants required')
    if protected_bytes(path,anchor=root)!=raw:raise PermissionError('Configuration replaced')
    toolsets=config.get('platform_toolsets',{}).get('api_server',config.get('toolsets',[]))
    if not isinstance(toolsets,list) or any(not isinstance(t,str) for t in toolsets):raise PermissionError('Installed tool policy required')
    servers=config.get('mcp_servers') or {}
    if not isinstance(servers,dict):raise PermissionError('Installed MCP policy required')
    enabled_servers={name for name,cfg in servers.items() if isinstance(name,str) and isinstance(cfg,dict) and str(cfg.get('enabled',True)).lower() not in {'false','0','no','off','none'}}
    selected=set(toolsets)
    if 'no_mcp' not in selected:selected.update((selected & enabled_servers) or enabled_servers)
    else:selected-=enabled_servers
    toolsets=sorted(selected-{'no_mcp'})
    return {'toolsets':toolsets,'profile':profile,'principal':principal,'board':row['board'],'scope_contract':row['scope_contract'],'allowed_boards':boards,'fingerprint':hashlib.sha256(raw+json.dumps(owner,sort_keys=True).encode()).hexdigest()}

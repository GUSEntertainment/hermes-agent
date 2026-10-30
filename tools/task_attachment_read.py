"""Read one durable attachment through its live task admission, not a path grant."""
import hashlib,os,stat,json
from pathlib import Path
MAX_BYTES=1024*1024
MAX_TEXT_BYTES=16384

def _unconfined_contract(conn, task_id):
    """Recover only this live process's persisted native admission, not an env grant."""
    from gateway import gus_worker_authority as gwa
    from gateway import member_worker_policy as policy
    from agent.private_worker_mode import _validate_shape, contract_digest, _default_durable_validator
    # An existing descriptor, including malformed/empty, must never downgrade.
    if 'HERMES_WORKER_ADMISSION_FD' in os.environ:
        raise PermissionError('Unusable admission descriptor cannot fall back')
    binding=gwa.current()
    if not binding or binding['task']!=task_id or binding['board']!='chris-home':
        raise PermissionError('No admitted unconfined owner task')
    root=gwa.ROOT
    db=Path(next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),''))
    if db!=root/'kanban/boards/chris-home/kanban.db':raise PermissionError('Unconfined board mismatch')
    before=gwa.protected(db,root)
    if policy.select(task_id,binding['board'],binding['profile']) is not None:
        raise PermissionError('Member workers require authenticated descriptor')
    row=conn.execute('SELECT worker_admission_contract,worker_admission_digest,worker_pid FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not row or row[2]!=os.getpid():raise PermissionError('Native worker PID mismatch')
    contract=json.loads(row[0] or '{}')
    if contract.get('workspace_confinement') is not False or contract.get('member_company_policy'):
        raise PermissionError('Confined worker requires authenticated descriptor')
    # Reuse strict schema validation without changing the actual persisted flag.
    _validate_shape(dict(contract,workspace_confinement=True))
    if contract['task']!=task_id or contract['profile']!=binding['profile']:
        raise PermissionError('Native worker profile mismatch')
    home=root/'profiles'/binding['profile'];config=home/'config.yaml'
    if contract['profile_home']!=str(home):raise PermissionError('Native profile home mismatch')
    from hermes_cli import kanban_db as kb
    if not contract['profile_config_digest'].startswith('native-profile-v2:'):
        raise PermissionError('Legacy profile digest requires fresh native admission')
    config_before=gwa.protected(config,root)
    # Match the exact native producer: labeled config + env + plugin/skill inputs.
    profile_digest=kb._profile_config_digest(str(home))
    config_after=gwa.protected(config,root)
    if (_signature(config_before)!=_signature(config_after)
            or profile_digest!=contract['profile_config_digest']
            or kb._profile_config_digest(str(home))!=profile_digest):
        raise PermissionError('Native profile configuration changed')
    digest=contract_digest(contract)
    if row[1]!=digest or not _default_durable_validator(contract,digest):raise PermissionError('Native admission is not live')
    after=gwa.protected(db,root)
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise PermissionError('Native database changed')
    return contract

def _authority(conn,task_id):
    from agent.private_worker_mode import get_authenticated_worker_contract,contract_digest,_default_durable_validator
    from agent.delegation_context import is_delegated_child_context
    if is_delegated_child_context():raise PermissionError('Attachment bytes require native task owner')
    contract=get_authenticated_worker_contract()
    if contract is None:contract=_unconfined_contract(conn,task_id)
    if not contract or contract.get('task')!=task_id:raise PermissionError('Attachment outside admitted native task')
    if contract.get('member_company_policy'):
        from gateway.member_worker_policy import verify
        verify(contract['member_company_policy'])
    db=Path(next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),'')).absolute()
    identity=contract['board_database'];st=db.stat()
    if str(db)!=identity['path'] or (st.st_dev,st.st_ino)!=(identity['device'],identity['inode']):raise PermissionError('Attachment board mismatch')
    if not _default_durable_validator(contract,contract_digest(contract)):raise PermissionError('Native task claim is no longer live')
    return contract,db

def _signature(st):return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
def _read(path):
    """Descriptor-relative walk prevents symlink/rename escapes during reads."""
    if not path.is_absolute() or '..' in path.parts:raise PermissionError('Invalid attachment path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);opened=[]
    try:
        for index,part in enumerate(path.parts[1:]):
            final=index==len(path.parts)-2
            child=os.open(part,os.O_RDONLY|os.O_NOFOLLOW|(os.O_NONBLOCK if final else os.O_DIRECTORY),dir_fd=fd)
            st=os.fstat(child)
            if st.st_uid not in (0,os.getuid()) or st.st_mode&0o022:os.close(child);raise PermissionError('Unprotected attachment path')
            if final and (not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>MAX_BYTES):os.close(child);raise PermissionError('Attachment type, links or size rejected')
            opened.append((fd,part,_signature(st)));fd=child
        before=os.fstat(fd);parts=[];remaining=MAX_BYTES+1
        while remaining:
            chunk=os.read(fd,min(65536,remaining))
            if not chunk:break
            parts.append(chunk);remaining-=len(chunk)
        data=b''.join(parts)
        if len(data)>MAX_BYTES or len(data)!=before.st_size or _signature(os.fstat(fd))!=_signature(before):raise PermissionError('Attachment changed while reading')
        for parent,name,sig in opened:
            if _signature(os.stat(name,dir_fd=parent,follow_symlinks=False))!=sig:raise PermissionError('Attachment path changed while reading')
        return data
    finally:
        os.close(fd)
        for parent,_,_ in opened:os.close(parent)

def read_attachment(kb,conn,task_id,attachment_id):
    if type(attachment_id)!=int or attachment_id<=0:raise ValueError('Positive attachment_id required')
    contract,db=_authority(conn,task_id)
    attachment=kb.get_attachment(conn,attachment_id)
    if attachment is None or attachment.task_id!=task_id:raise PermissionError('Attachment outside admitted task')
    path=Path(attachment.stored_path)
    root=db.parent/'attachments'/task_id
    if path.parent!=root or path.name!=attachment.filename or path.name in ('','.', '..'):raise PermissionError('Attachment outside canonical task storage')
    data=_read(path)
    if len(data)!=attachment.size:raise PermissionError('Attachment metadata size mismatch')
    # Recheck authority and row after the read. No bytes leave on revocation/race.
    if _authority(conn,task_id)[0]!=contract or kb.get_attachment(conn,attachment_id)!=attachment:raise PermissionError('Attachment authority changed')
    result={'ok':True,'task_id':task_id,'attachment_id':attachment.id,'filename':attachment.filename,'content_type':attachment.content_type,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    if len(data)>MAX_TEXT_BYTES:result['text_omitted']='text_size_limit'
    elif b'\0' in data:result['text_omitted']='binary'
    else:
        try:result['text']=data.decode('utf-8')
        except UnicodeDecodeError:result['text_omitted']='binary'
    return result


def store_workspace_attachment(kb,conn,task_id,file_path,filename=None,content_type=None):
    """Copy admitted workspace bytes without passing them through model context."""
    contract,db=_authority(conn,task_id)
    workspace=Path(contract['workspace']['path'])
    if type(file_path)!=str or not file_path or '..' in Path(file_path).parts:
        raise PermissionError('Invalid workspace attachment path')
    path=Path(file_path)
    if not path.is_absolute():path=workspace/path
    if not path.is_relative_to(workspace) or path==workspace:
        raise PermissionError('Attachment outside admitted workspace')
    data=_read(path)
    if _authority(conn,task_id)[0]!=contract:
        raise PermissionError('Attachment authority changed')
    attachment_id=kb.store_attachment_bytes(conn,task_id,filename or path.name,data,content_type=content_type,uploaded_by='agent',board=db.parent.name)
    return {'task_id':task_id,'attachment_id':attachment_id,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}

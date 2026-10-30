"""Recipient-only produced document custody. Not delivery proof or reviewed task."""
import contextlib
import weakref
import asyncio, hashlib, json, logging, os, re, sqlite3, stat, time
from pathlib import Path
ROOT=Path('/Users/Chris/.hermes/private/document-delivery/direct')
MAX_BYTES=32*1024*1024
EXTENSIONS={'.pdf','.doc','.docx','.txt','.md','.csv','.tsv','.xlsx','.xls','.pptx','.ppt','.rtf','.odt','.ods','.odp'}
KIND='gateway-conversation-document-v1'
SCHEMA="CREATE TABLE documents(id TEXT PRIMARY KEY, source_key TEXT NOT NULL, sha256 TEXT NOT NULL, binding_json TEXT NOT NULL, authority_json TEXT NOT NULL, filename TEXT NOT NULL, created_at REAL NOT NULL, content BLOB NOT NULL, UNIQUE(source_key,sha256))"
TRIGGERS={name:f"CREATE TRIGGER {name} BEFORE {op} ON documents BEGIN SELECT RAISE(ABORT,'immutable document'); END" for name,op in [('immutable_update','UPDATE'),('immutable_delete','DELETE')]}
log=logging.getLogger(__name__)
def encode(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(v):return hashlib.sha256(v).hexdigest()
_events={}  # Process-local genuine event identities; never exposed as tool arguments.
_UNSPECIFIED_AUTHORITY=object()
def bind_event(event):
    """Called only by authorized GatewayRunner with fresh bound session values."""
    from gateway.session_context import get_bound_session_env as get
    if getattr(event,'internal',False):return
    platform=get('HERMES_SESSION_PLATFORM')
    if platform not in ('telegram','photon'):return
    source=event.source
    values={'platform':platform,'chat_id':get('HERMES_SESSION_CHAT_ID'),'thread_id':get('HERMES_SESSION_THREAD_ID') or None,'chat_type':get('HERMES_SESSION_CHAT_TYPE') or 'dm','user_id':get('HERMES_SESSION_USER_ID')}
    if str(getattr(source.platform,'value',source.platform))!=platform or str(source.chat_id)!=values['chat_id'] or str(source.user_id)!=values['user_id'] or (str(source.thread_id) if source.thread_id else None)!=values['thread_id']:return
    message=get('HERMES_SESSION_MESSAGE_ID')
    if not message or str(event.message_id)!=message or not get('HERMES_SESSION_ID') or not get('HERMES_SESSION_KEY'):return
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home());profile=get('HERMES_SESSION_PROFILE') or (home.name if home.parent.name=='profiles' else 'default')
    binding=dict(kind=KIND,profile=profile,session_id=get('HERMES_SESSION_ID'),message_id=message,target=values,request_sha256=sha(event.text.encode()),received_at=event.timestamp.timestamp())
    key=id(event)
    _events[key]=(weakref.ref(event,lambda _: _events.pop(key,None)),encode(binding))

def _identity(st):return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
def _directory(path):
    path=Path(path)
    if path.resolve()!=path:raise PermissionError('Symlink document directory')
    for parent in (path,*path.parents):
        s=parent.stat()
        if s.st_uid not in (0,os.getuid()) or s.st_mode&0o022:raise PermissionError('Unprotected document ancestor')
    if path.stat().st_mode&0o077:raise PermissionError('Document store is not private')
def _db(owner,write=False):
    if not re.fullmatch('[a-z][a-z0-9_-]{0,63}',owner):raise PermissionError('Invalid recipient')
    _directory(ROOT)
    home=ROOT/owner
    if write:
        try:home.mkdir(mode=0o700)
        except FileExistsError:pass
    _directory(home);path=home/'catalog.sqlite'
    if write and not path.exists():
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
    st=path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.getuid() or st.st_mode&0o077:raise PermissionError('Unprotected document index')
    for suffix in ('-journal','-wal','-shm'):
        side=Path(str(path)+suffix)
        if side.exists() or side.is_symlink():
            stside=side.lstat()
            if not stat.S_ISREG(stside.st_mode) or stside.st_nlink!=1 or stside.st_uid!=os.getuid() or stside.st_mode&0o077:raise PermissionError('Unsafe document sidecar')
    c=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=5)
    c.row_factory=sqlite3.Row
    try:
        if write:
            c.execute('PRAGMA journal_mode=DELETE');c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            rows=list(c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            if not rows:
                c.execute(SCHEMA)
                for sql in TRIGGERS.values():c.execute(sql)
        actual={r['name']:r['sql'] for r in c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if actual!={'documents':SCHEMA,**TRIGGERS}:raise PermissionError('Document schema changed')
        now=path.lstat()
        if (st.st_dev,st.st_ino)!=(now.st_dev,now.st_ino):raise PermissionError('Document index replaced')
        return c
    except BaseException:c.close();raise

def _read_source(path):
    p=Path(path)
    if p.suffix.lower() not in EXTENSIONS:return None
    if not p.is_absolute() or p.resolve()!=p:raise PermissionError('Document source symlink')
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_BYTES:raise PermissionError('Unsafe document source')
        with os.fdopen(os.dup(fd),'rb') as f:raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES or _identity(os.fstat(fd))!=_identity(before) or _identity(p.lstat())!=_identity(before):raise PermissionError('Document changed during capture')
        return raw
    finally:os.close(fd)

def _assert_destination(event,binding,metadata):
    target=binding['target'];source=event.source
    observed=dict(platform=str(getattr(source.platform,'value',source.platform)),chat_id=str(source.chat_id),user_id=str(source.user_id),thread_id=str(source.thread_id) if source.thread_id else None)
    if any(target[k]!=v for k,v in observed.items()) or str(event.message_id)!=binding['message_id']:raise PermissionError('Original document event changed')
    for key in ('thread_id','message_thread_id','direct_messages_topic_id'):
        if metadata and metadata.get(key) is not None and str(metadata[key])!=str(target['thread_id']):raise PermissionError('Selected document destination changed')

def capture(event,path,metadata=None,*,expected_authority=_UNSPECIFIED_AUTHORITY):
    token=_events.get(id(event))
    if token is None or token[0]() is not event or getattr(event,'internal',False):return None
    binding=json.loads(token[1])
    _assert_destination(event,binding,metadata)
    from gateway.gateway_document_custody import current
    authority=current(binding)
    if expected_authority is not _UNSPECIFIED_AUTHORITY and authority!=expected_authority:raise PermissionError('Enqueued document authority changed')
    if authority is None:return None
    if binding['received_at']<authority['enabled_from']:return None
    raw=_read_source(path)
    if raw is None:return None
    owner=authority['owner'];source_key=sha(encode({k:binding[k] for k in ('kind','profile','message_id','target')}).encode());digest=sha(raw);identifier=sha((source_key+':'+digest).encode())
    with contextlib.closing(_db(owner,True)) as c, c:
        old=c.execute('SELECT id,binding_json,authority_json FROM documents WHERE source_key=? AND sha256=?',(source_key,digest)).fetchone()
        if current(binding)!=authority:raise PermissionError('Recipient revoked during capture')
        if old:
            # Session IDs may regenerate on replay; stable original sender/message/bytes dedup.
            prior=json.loads(old['authority_json'])
            if json.loads(old['binding_json'])['request_sha256']!=binding['request_sha256'] or prior['owner']!=owner or prior['policy_generation']!=authority['policy_generation']:raise PermissionError('Document replay authority changed')
            return old['id']
        name=Path(path).name
        if any(ord(x)<32 for x in name) or len(name)>255:raise PermissionError('Invalid document filename')
        c.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)',(identifier,source_key,digest,token[1],encode(authority),name,time.time(),raw))
    return identifier


class RowIneligible(PermissionError):
    """Current authority was successfully read but this historical row is ineligible."""

def _authorize_row(row,owner):
    from gateway.gateway_document_custody import current
    binding=json.loads(row['binding_json']);saved=json.loads(row['authority_json']);now=current(binding)
    if now is None or now!=saved or now['owner']!=owner or now.get('domain')!='recipient_documents':raise RowIneligible('Recipient document unavailable')
    return binding,now

def list_documents(owner,*,cursor='',limit=25):
    """Caller must derive owner from authenticated app session, never query params."""
    if not re.fullmatch('[a-z][a-z0-9_-]{0,63}',owner):raise PermissionError('Invalid recipient')
    if type(limit)!=int or not 1<=limit<=50 or (cursor and not re.fullmatch('[a-f0-9]{64}',cursor)):raise ValueError('Invalid page')
    path=ROOT/owner/'catalog.sqlite'
    if not path.exists() and not path.is_symlink():return {'documents':[],'cursor':None}
    original=path.lstat()
    from gateway.gateway_document_custody import authority_revision
    revision=authority_revision()
    with contextlib.closing(_db(owner)) as c:
        rows=c.execute('SELECT id,sha256,binding_json,authority_json,filename,created_at,length(content) AS size FROM documents WHERE id>? ORDER BY id LIMIT ?', (cursor,limit)).fetchall()
        output=[];eligible=[]
        for row in rows:
            try:_authorize_row(row,owner)
            except RowIneligible:continue
            eligible.append(row)
            output.append({'id':row['id'],'sha256':row['sha256'],'filename':row['filename'],'created_at':row['created_at'],'size':row['size'],'domain':'recipient_documents','provenance':'conversation','delivery':'not_attested'})
        for row in eligible:_authorize_row(row,owner)
        if authority_revision()!=revision:raise PermissionError('Recipient authority changed during page')
        _index_unchanged(path,original)
        return {'documents':output,'cursor':rows[-1]['id'] if len(rows)==limit else None}

def read_document(owner,identifier):
    if not re.fullmatch('[a-f0-9]{64}',identifier):raise PermissionError('Invalid document')
    if not re.fullmatch('[a-z][a-z0-9_-]{0,63}',owner):raise PermissionError('Invalid recipient')
    path=ROOT/owner/'catalog.sqlite';original=path.lstat()
    from gateway.gateway_document_custody import authority_revision
    revision=authority_revision()
    with contextlib.closing(_db(owner)) as c:
        row=c.execute('SELECT * FROM documents WHERE id=?',(identifier,)).fetchone()
        if row is None:raise PermissionError('Document unavailable')
        _authorize_row(row,owner)
        raw=bytes(row['content'])
        if len(raw)>MAX_BYTES or sha(raw)!=row['sha256']:raise PermissionError('Document bytes changed')
        _authorize_row(row,owner)
        if authority_revision()!=revision:raise PermissionError('Recipient authority changed during download')
        _index_unchanged(path,original)
        return {'content':raw,'sha256':row['sha256'],'filename':row['filename'],'provenance':'conversation','domain':'recipient_documents'}

def _index_unchanged(path,before):
    _directory(path.parent)
    now=path.lstat()
    if (before.st_dev,before.st_ino,before.st_mode,before.st_uid,before.st_nlink)!=(now.st_dev,now.st_ino,now.st_mode,now.st_uid,now.st_nlink):raise PermissionError('Document index replaced')

async def send_selected(adapter,event,path,metadata=None,*,expected_authority=_UNSPECIFIED_AUTHORITY):
    """Send exact captured bytes, or preserve unconfigured transport behavior.

    Storage failure is visible in this same reply only after attachment success.
    Authority/source errors never fall through to send private bytes.
    """
    token=_events.get(id(event))
    if token is None or token[0]() is not event:
        if expected_authority is not _UNSPECIFIED_AUTHORITY:raise PermissionError('Enqueued document event unavailable')
        return await adapter.send_document(chat_id=event.source.chat_id,file_path=path,metadata=metadata)
    if Path(path).suffix.lower() not in EXTENSIONS:
        return await adapter.send_document(chat_id=event.source.chat_id,file_path=path,metadata=metadata)
    from gateway.gateway_document_custody import current
    binding=json.loads(token[1]);metadata=dict(metadata or {});chat_id=binding['target']['chat_id']
    _assert_destination(event,binding,metadata)
    authority=await asyncio.to_thread(current,binding)
    if expected_authority is not _UNSPECIFIED_AUTHORITY and authority!=expected_authority:raise PermissionError('Enqueued document authority changed')
    if authority is None or binding['received_at']<authority['enabled_from']:
        _assert_destination(event,binding,metadata)
        return await adapter.send_document(chat_id=chat_id,file_path=path,metadata=metadata)
    try:
        if expected_authority is _UNSPECIFIED_AUTHORITY:
            identifier=await asyncio.to_thread(capture,event,path,metadata)
        else:
            identifier=await asyncio.to_thread(capture,event,path,metadata,expected_authority=expected_authority)
        if identifier is None:raise PermissionError('Selected document authority changed')
        artifact=await asyncio.to_thread(read_document,authority['owner'],identifier)
    except PermissionError:
        raise
    except (OSError,sqlite3.Error):
        # A known authorized capture was not durably retained. Do not queue an
        # independent retry/notice when storage itself cannot assure dedup.
        if await asyncio.to_thread(current,binding)!=authority:raise PermissionError('Recipient changed before transport')
        _assert_destination(event,binding,metadata)
        result=await adapter.send_document(chat_id=chat_id,file_path=path,metadata=metadata)
        if getattr(result,'success',False) and not getattr(event,'_desk_failure_notice_attempted',False):
            event._desk_failure_notice_attempted=True
            notice_meta=dict(metadata or {})
            notice_meta['logical_message_id']='desk-retention-'+sha(encode({k:binding[k] for k in ('profile','message_id','target')}).encode())
            try:
                if await asyncio.to_thread(current,binding)!=authority:raise PermissionError('Recipient changed before notice')
                _assert_destination(event,binding,metadata)
                await adapter.send(chat_id=chat_id,content="The attachment was sent, but I couldn't save it to your Desk. Please keep this copy.",metadata=notice_meta)
            except Exception as exc:log.warning('Desk retention notice outcome unknown (%s)',type(exc).__name__)
        return result
    import tempfile
    with tempfile.TemporaryDirectory(prefix='.delivery-',dir=ROOT/authority['owner']) as temp:
        selected=Path(temp)/artifact['filename']
        fd=os.open(selected,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(artifact['content']);stream.flush();os.fsync(stream.fileno())
        if await asyncio.to_thread(current,binding)!=authority:raise PermissionError('Recipient changed before transport')
        _assert_destination(event,binding,metadata)
        return await adapter.send_document(chat_id=chat_id,file_path=str(selected),metadata=metadata)

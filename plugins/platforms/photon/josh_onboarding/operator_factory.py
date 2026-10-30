"""Inactive fixed operator loader. No model-selected paths or bearer values."""
import os,stat,json,sqlite3,hashlib,asyncio
from pathlib import Path
if __package__:
    from .photon_owner_handler import JoshPhotonHandler,Route,Unavailable
else:
    from photon_owner_handler import JoshPhotonHandler,Route,Unavailable
ROOT=Path('/Users/Chris/.hermes/private/josh-photon')
PROFILES=Path('/Users/Chris/.hermes/profiles')
OWNERS=Path('/Users/Chris/.hermes/private/photon-owners')
PORT=8642

def protected(path):
    path=Path(path)
    if path.resolve(strict=True)!=path:raise Unavailable('Noncanonical operator path')
    parent=path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid() or parent.st_mode&0o077:raise Unavailable('Private operator directory required')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_mode&0o077 or before.st_nlink!=1 or before.st_size>65536:raise Unavailable('Private operator file required')
        data=os.read(fd,65537)
        after=os.fstat(fd)
        signature=lambda st:(st.st_dev,st.st_ino,st.st_uid,st.st_mode,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
        if signature(before)!=signature(after) or signature(after)!=signature(path.lstat()) or signature(parent)!=signature(path.parent.lstat()) or path.resolve()!=path:raise Unavailable('Operator file changed')
        return data
    finally:os.close(fd)

def ledger_preflight(path):
    parent=Path(path).parent
    if parent.resolve(strict=True)!=parent or parent.stat().st_uid!=os.getuid() or parent.stat().st_mode&0o077:raise Unavailable('Private ledger directory required')
    for candidate in [Path(path),Path(str(path)+'-wal'),Path(str(path)+'-shm'),Path(str(path)+'-journal')]:
        if candidate.exists() or candidate.is_symlink():
            st=candidate.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1:raise Unavailable('Unsafe ledger')
    if not Path(path).exists():
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)

class OperatorFactory:
    def __init__(self, binding):
        # binding is selected from protected custody records, never model input.
        from gateway.owner_work_enrollment import current
        self.binding=dict(binding);self.owner=binding['owner'];self.board=binding['board']
        verified=current(self.owner)
        if verified['profile']!=binding['profile'] or verified['board']!=self.board:raise Unavailable('Canonical owner mismatch')
        self.fingerprint=verified['fingerprint']
        self.root=ROOT if self.owner=='joshgarcia' else OWNERS/self.owner
        self.profile=PROFILES/binding['profile'];self.endpoint=f'http://127.0.0.1:{PORT}/p/{binding["profile"]}/v1/runs'
        self.admission_bytes=protected(self.root/'admission.json')
        self.admission=json.loads(self.admission_bytes)
        if set(self.admission)!={'owner','board','phone_digest','destination'} or self.admission['owner']!=self.owner or self.admission['board']!=self.board or self.admission['phone_digest']!=binding['phone_digest']:raise Unavailable('Invalid operator identity')
        from .photon_owner_handler import SenderAdmission
        SenderAdmission(self.admission['phone_digest'],self.admission['destination'])
    def validate(self):
        from gateway.owner_work_enrollment import current
        from plugins.platforms.photon.secure_intake.explicit_enrollment import read_bindings
        rows,_=read_bindings()
        if self.binding not in rows or not self.binding['enabled'] or current(self.owner)['fingerprint']!=self.fingerprint:raise Unavailable('Owner enrollment changed')
        if protected(self.root/'admission.json')!=self.admission_bytes:raise Unavailable('Enrollment changed')
    def route(self):
        self.validate()
        obj=json.loads(protected(self.root/'route.json'))
        if set(obj)!={'owner','board','endpoint','generation','enabled','revoked','isolation_receipt'} or obj['owner']!=self.owner or obj['board']!=self.board or obj['endpoint']!=self.endpoint or type(obj['generation']) is not int or obj['generation']<1 or type(obj['enabled']) is not bool or type(obj['revoked']) is not bool:raise Unavailable('Invalid operator route')
        ledger_preflight(self.root/'ingress.sqlite')
        return Route(self.admission['phone_digest'],self.endpoint,obj['generation'],obj['enabled'] and not obj['revoked'],obj['isolation_receipt'])
    def key(self):
        self.validate()
        from agent.secret_scope import load_env_file
        from hermes_cli.auth import has_usable_secret
        path=self.profile/'.env';before=protected(path)
        key=load_env_file(path).get('API_SERVER_KEY','')
        if protected(path)!=before or not has_usable_secret(key,min_length=16):raise Unavailable('Profile credential unavailable')
        return key
    def build(self):
        ledger_preflight(self.root/'ingress.sqlite')
        from .native_runs_client import NativeRunsOwnerHandler
        from gateway.owner_api_intake_factory import build
        intake,_,endpoint=build(self.owner,PORT)
        if endpoint!=self.endpoint:raise Unavailable('Native endpoint mismatch')
        return NativeRunsOwnerHandler(str(self.root/'ingress.sqlite'),self.route,self.key,receipts=intake.receipts,enrolled_phone_digest=self.admission['phone_digest'],enrolled_destination=self.admission['destination'])

class OwnerHandlers:
    """Multiplex existing handler/ledger; one existing lifecycle, no new queue."""
    def __init__(self,handlers,digests,loaders):self.handlers=handlers;self.digests=frozenset(digests);self.loaders=loaders;self.cursor=0
    def _handler(self,event):
        sender=getattr(getattr(event,'source',None),'user_id',None)
        digest=hashlib.sha256(sender.encode()).hexdigest() if isinstance(sender,str) else None
        return self.handlers.get(digest)
    def matches(self,event):
        sender=getattr(getattr(event,'source',None),'user_id',None)
        return isinstance(sender,str) and hashlib.sha256(sender.encode()).hexdigest() in self.digests
    async def handle(self,event):
        handler=self._handler(event)
        if handler is None:raise Unavailable('Enrolled route unavailable')
        return await handler.handle(event)
    async def deliver(self,adapter,event):
        handler=self._handler(event)
        if handler is None:raise Unavailable('Enrolled route unavailable')
        return await handler.deliver(adapter,event)
    async def recover_pending(self,adapter,limit=1):
        if not self.loaders:return {'examined':0,'delivered':0}
        keys=sorted(self.loaders);key=keys[self.cursor%len(keys)];self.cursor=(self.cursor+1)%len(keys)
        if key not in self.handlers:
            try:self.handlers[key]=await asyncio.to_thread(self.loaders[key])
            except Exception:return {'examined':0,'delivered':0,'unavailable':True}
        return await self.handlers[key].recover_pending(adapter,limit=limit)

def build_operator_handler(exclusive_digests):
    # Startup guard is operator config independent of fallible enrollment reads.
    # Every enrolled/revoked sender must stay exclusive even if a route fails.
    from plugins.platforms.photon.secure_intake.explicit_enrollment import read_bindings
    rows,_=read_bindings();digests={b['phone_digest'] for b in rows}
    if not digests.issubset(set(exclusive_digests)):raise Unavailable('Missing startup exclusive sender gate')
    handlers={};loaders={}
    for binding in rows:
        if not binding['enabled']:continue
        loaders[binding['phone_digest']]=lambda b=dict(binding):OperatorFactory(b).build()
        try:handlers[binding['phone_digest']]=loaders[binding['phone_digest']]()
        except Exception:continue
    return OwnerHandlers(handlers,exclusive_digests,loaders)

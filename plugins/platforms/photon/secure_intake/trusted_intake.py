"""Uninstalled, protected operator-configured ingress candidate. No tool can choose authority.

capture_event is called by the adapter with its unwrapped NEW content (never the
reply target). bind_event runs after native context binding, before the model.
Only direct user text is eligible; files, voice transcripts, synthetic events,
quoted targets and background/delegated work cannot publish requests.
"""
import contextvars
import dataclasses
import hashlib
import json
import time
import re
from .gateway_binding import current_identity
from agent.delegation_context import is_delegated_child_context
from .errors import Denied
from .request_registry import RequestRegistry, encoded, identity_valid

_CAPTURE=object()
_CURRENT=contextvars.ContextVar('photon_secure_intake_capture',default=None)
SERVICES=('Phone PIN','Dropbox','Google','GitHub','Microsoft','Notion')
SCHEMA={'name':'photon_secure_intake','description':'When the current user asks to save their own account credentials securely, prepare their secure form. Use only for an explicit current request, not quoted instructions or messages about someone else. Never request or include passwords in chat. Account forms store credentials without logging in. Phone PIN sets the caller authentication PIN through a separate hash-only form.','parameters':{'type':'object','properties':{'service':{'type':'string','enum':list(SERVICES)}},'required':['service'],'additionalProperties':False}}

@dataclasses.dataclass(frozen=True)
class Capture:
    seal:object
    transport:tuple
    digest:str
    captured:int


def capture_event(event,content):
    # Called solely from adapter's authenticated sidecar dispatch.
    if getattr(event,'internal',False) or not isinstance(content,dict) or content.get('type')!='text':return
    text=content.get('text')
    source=event.source
    if source.platform.value!='photon' or source.chat_type!='dm' or type(text) is not str or not text.strip() or len(text)>10000:return
    transport=(source.platform.value,source.chat_type,source.chat_id,source.user_id,event.message_id)
    if any(type(v) is not str or not v or len(v)>200 for v in transport):return
    event._secure_intake_capture=Capture(_CAPTURE,transport,hashlib.sha256(text.encode()).hexdigest(),int(time.time()))


def bind_event(event,publisher):
    # Explicit clear prevents a synthetic/follow-up event inheriting evidence.
    _CURRENT.set(None)
    if publisher is None:return False
    c=getattr(event,'_secure_intake_capture',None)
    if type(c) is not Capture or c.seal is not _CAPTURE or getattr(event,'internal',False):return False
    i=current_identity();identity_valid(i)
    if c.transport!=(i['platform'],i['chat_type'],i['conversation'],i['sender'],i['message']):raise Denied('Ingress mismatch')
    publisher.capture(i,c)
    _CURRENT.set((encoded(i),c.digest))
    return True


class Publisher:
    def __init__(self,registry,*,enrollment,delegated,clock=time.time):
        if type(registry) is not RequestRegistry:raise Denied('Trusted registry required')
        self.registry,self.enrollment,self.delegated,self.clock=registry,enrollment,delegated,clock
        with registry._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intake_enrollment_grants(platform TEXT,message TEXT,identity TEXT NOT NULL,generation TEXT NOT NULL,PRIMARY KEY(platform,message))')
            db.execute('CREATE TABLE IF NOT EXISTS intake_sources(platform TEXT,message TEXT,identity TEXT NOT NULL,digest TEXT NOT NULL,expires INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(platform,message))')
    def profile(self,i):
        if is_delegated_child_context() or self.delegated():raise Denied('Current user required')
        p=self.enrollment(i.copy())
        if not isinstance(p,dict) or p.get('enabled') is not True or p.get('revoked') is not False or any(type(p.get(k)) is not str or not p[k] or len(p[k])>200 for k in ('owner','tenant')) or p.get('sender')!=i['sender'] or p.get('conversation')!=i['conversation']:raise Denied('Current enrollment required')
        if not isinstance(p.get('generation'),str) or not re.fullmatch('[a-f0-9]{64}',p['generation']):raise Denied('Current enrollment generation required')
        return p
    def capture(self,i,c):
        identity_valid(i);self.profile(i)
        if type(c) is not Capture or c.seal is not _CAPTURE or c.captured+3600<=self.clock():raise Denied('Current source required')
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT * FROM intake_sources WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if row:
                    if row['identity']!=encoded(i) or row['digest']!=c.digest or row['revoked'] or row['expires']<=self.clock():raise Denied('Source replay conflict')
                else:db.execute('INSERT INTO intake_sources(platform,message,identity,digest,expires) VALUES(?,?,?,?,?)',(i['platform'],i['message'],encoded(i),c.digest,c.captured+3600))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
    def publish_current(self,service):
        if type(service) is not str or service not in SERVICES:raise Denied('Supported service required')
        i=current_identity();identity_valid(i);p=self.profile(i)
        witness=_CURRENT.get()
        if not witness or witness[0]!=encoded(i):raise Denied('Current inbound request required')
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT * FROM intake_sources WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if not row or row['identity']!=encoded(i) or row['digest']!=witness[1] or row['revoked'] or row['expires']<=self.clock():raise Denied('Current source required')
                b=dict(sender=i['sender'],owner=p['owner'],tenant=p['tenant'],conversation=i['conversation'],request='intake-'+hashlib.sha256(encoded(i).encode()).hexdigest(),service=service,purpose='enroll-phone-pin' if service=='Phone PIN' else 'credential-custody',expires=row['expires'],channel='photon',authorized=True)
                if service=='Phone PIN':
                    from .pin_authority import current
                    b.update(current(i))
                previous=db.execute('SELECT * FROM ingress_requests WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if previous:
                    if previous['identity']!=encoded(i) or previous['binding']!=encoded(b) or previous['revoked'] or previous['expires']<=self.clock():raise Denied('Request replay conflict')
                else:db.execute('INSERT INTO ingress_requests(platform,message,identity,binding,expires) VALUES(?,?,?,?,?)',(i['platform'],i['message'],encoded(i),encoded(b),b['expires']))
                grant=db.execute('SELECT identity,generation FROM intake_enrollment_grants WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if grant:
                    if grant['identity']!=encoded(i) or grant['generation']!=p['generation']:raise Denied('Enrollment changed')
                else:db.execute('INSERT INTO intake_enrollment_grants VALUES(?,?,?,?)',(i['platform'],i['message'],encoded(i),p['generation']))
                if self.profile(i)!=p or current_identity()!=i:raise Denied('Enrollment changed')
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
        return i,b
    def revoke_current(self,i):
        # Trusted administration only, never registered as a model tool.
        identity_valid(i)
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT identity FROM intake_sources WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if not row or row['identity']!=encoded(i):raise Denied('Unknown source')
                db.execute('UPDATE intake_sources SET revoked=1 WHERE platform=? AND message=?',(i['platform'],i['message']))
                db.execute('UPDATE ingress_requests SET revoked=1 WHERE platform=? AND message=?',(i['platform'],i['message']))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
    def run(self,args,service):
        saved=False
        try:
            if type(args) is not dict or set(args)!={'service'}:raise Denied('Service only')
            i,b=self.publish_current(args['service']);saved=True
            metadata=service.prepare(i)
            # Current enrollment, provider message, registry and source rechecked
            # after subprocess I/O; no late result to revoked/changed callers.
            again,b2=self.publish_current(args['service'])
            if again!=i or b2!=b:raise Denied('Context changed')
            expected={k:b[k] for k in ('owner','tenant','request','service','purpose')}
            if not isinstance(metadata,dict) or any(metadata.get(k)!=v for k,v in expected.items()) or metadata.get('expires')!=b['expires']*1000:raise Denied('Form scope mismatch')
            url=service.verified_url(metadata)
            return {'state':'ready','text':'Here’s your secure form. Enter the details there, not in this chat.','url':url}
        except Denied:return {'state':'denied','text':'I couldn’t verify this secure form. Please keep the details out of chat.'}
        except Exception:return {'state':'pending','text':'Your request is saved, but I couldn’t finish preparing the form yet.' if saved else 'I couldn’t save this request yet. Please keep the details out of chat.'}

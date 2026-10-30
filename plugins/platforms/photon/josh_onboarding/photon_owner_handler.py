"""Isolated ingress composition: exact sender -> existing authenticated cockpit.
No model-selected owner, destination, tools or key. No production registration.
"""
import asyncio
import time
import hashlib
import json
import re
import sqlite3
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager

@contextmanager
def database(path):
    connection=sqlite3.connect(path)
    try:
        with connection:yield connection
    finally:connection.close()
from gateway.cockpit_user_routing import bind_cockpit_principal

class Unavailable(PermissionError): pass

@dataclass(frozen=True)
class Route:
    phone_digest: str
    endpoint: str
    generation: int
    enabled: bool
    # Operator attests actual existing backend tool/memory restrictions, not prompt.
    isolation_receipt: str

@dataclass(frozen=True)
class SenderAdmission:
    phone_digest: str
    destination: str
    def __post_init__(self):
        if not isinstance(self.phone_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',self.phone_digest):
            raise ValueError('Explicit enrolled sender digest required')
        if not isinstance(self.destination,str) or not self.destination.startswith('any;-;') or hashlib.sha256(self.destination[6:].encode()).hexdigest()!=self.phone_digest:
            raise ValueError('Explicit enrolled destination must match sender')

class JoshPhotonHandler:
    def __init__(self, db, current_route, current_key, transport=None, *, enrolled_phone_digest, enrolled_destination):
        self._admission=SenderAdmission(enrolled_phone_digest,enrolled_destination)
        self.db=db;self.current_route=current_route;self.current_key=current_key
        self.transport=transport or self._http
        with database(db) as c:
            c.execute('CREATE TABLE IF NOT EXISTS inbound(message TEXT PRIMARY KEY,digest TEXT NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL,result TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS reply_intents(message TEXT PRIMARY KEY,logical_id TEXT UNIQUE NOT NULL,destination_digest TEXT NOT NULL,payload_digest TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS source_bindings(message TEXT PRIMARY KEY,phone_digest TEXT NOT NULL,destination_digest TEXT NOT NULL,generation INTEGER NOT NULL,event_digest TEXT NOT NULL,endpoint_digest TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS recovery_cursor(slot INTEGER PRIMARY KEY,last_row INTEGER NOT NULL)')

    async def _authority(self,fn,*args,deadline=None):
        remaining=(deadline-time.monotonic()) if deadline is not None else getattr(self,'budget',6)
        if remaining<=0:raise TimeoutError('Authority budget exhausted')
        return await asyncio.wait_for(asyncio.to_thread(fn,*args),remaining)

    def matches(self,event):
        source=getattr(event,'source',None)
        sender=getattr(source,'user_id',None)
        platform=getattr(getattr(source,'platform',None),'value',None)
        return platform=='photon' and isinstance(sender,str) and hashlib.sha256(sender.encode()).hexdigest()==self._admission.phone_digest

    @staticmethod
    def _http(endpoint, headers, body):
        req=urllib.request.Request(endpoint,data=json.dumps(body).encode(),headers=headers,method='POST')
        # Redirects must never carry the authenticated owner key to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        with urllib.request.build_opener(NoRedirect).open(req,timeout=90) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise Unavailable('response too large')
        value=json.loads(raw)
        text=value['choices'][0]['message']['content']
        if not isinstance(text,str):raise Unavailable('invalid reply')
        return text

    async def handle(self,event):
        source=event.source
        if not self.matches(event):return None
        route=self.current_route()
        if route.phone_digest!=self._admission.phone_digest:raise Unavailable('enrolled sender changed')
        sender=source.user_id or ''
        # Unrelated senders stay on their existing handlers. A matched but disabled
        # sender is NEVER allowed to fall through to Chris's root assistant.
        if (not route.enabled or not route.isolation_receipt or route.generation<1
                or source.chat_type!='dm' or source.chat_id!='any;-;'+sender
                or not event.message_id or event.message_type.value!='text'
                or getattr(source,'profile',None)):
            raise Unavailable('owner route unavailable')
        from urllib.parse import urlsplit
        try:
            url=urlsplit(route.endpoint)
            port=url.port
        except ValueError:
            raise Unavailable('reviewed local owner backend required') from None
        allowed_paths={'/v1/chat/completions','/p/joshgarcia/v1/chat/completions'}
        # Exact serialization rejects URL parser normalization (whitespace, empty
        # query/fragment delimiters, credentials and encoded path variants).
        if (url.scheme!='http' or url.hostname!='127.0.0.1'
                or port is None or not 1 <= port <= 65535
                or url.path not in allowed_paths
                or route.endpoint != f'http://127.0.0.1:{port}{url.path}'):
            raise Unavailable('reviewed local owner backend required')
        lane=bind_cockpit_principal('joshgarcia')
        if not isinstance(event.text,str) or not event.text.strip() or len(event.text)>32000:
            raise Unavailable('bounded text required')
        digest=hashlib.sha256(json.dumps([sender,source.chat_id,event.text],separators=(',',':')).encode()).hexdigest()
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT digest,generation,state,result FROM inbound WHERE message=?',(event.message_id,)).fetchone()
            if row:
                if row[0]!=digest or row[1]!=route.generation:raise Unavailable('conflicting replay')
                # Returning None would fall through and execute on the root agent.
                if row[2]=='complete':return {'handled':True,'duplicate':True,'text':None}
                return {'handled':True,'state':'uncertain','text':None}
            c.execute('INSERT INTO inbound VALUES(?,?,?, ?,NULL)',(event.message_id,digest,route.generation,'started'))
            c.execute('INSERT INTO source_bindings VALUES(?,?,?,?,?,?)',(event.message_id,route.phone_digest,hashlib.sha256(source.chat_id.encode()).hexdigest(),route.generation,digest,hashlib.sha256(route.endpoint.encode()).hexdigest()))
        body={'model':'hermes-agent','stream':False,'messages':[{'role':'user','content':event.text}],
              'requesting_user':lane.user,'board':lane.board,'audience':lane.audience}
        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.current_key(),
                 'X-Hermes-Session-Key':lane.session_key}
        if self.current_route()!=route:raise Unavailable('owner route changed')
        try:
            result=await asyncio.to_thread(self.transport,route.endpoint,headers,body)
            if self.current_route()!=route:raise Unavailable('owner route changed')
            if not isinstance(result,str) or len(result)>100000:raise Unavailable('invalid reply')
        except Exception:
            with database(self.db) as c:c.execute("UPDATE inbound SET state='uncertain' WHERE message=?",(event.message_id,))
            raise Unavailable('owner backend result unverified') from None
        logical_id='josh-reply-'+hashlib.sha256(json.dumps([route.phone_digest,event.message_id],separators=(',',':')).encode()).hexdigest()
        with database(self.db) as c:
            c.execute("UPDATE inbound SET state='complete',result=? WHERE message=?",(result,event.message_id))
            c.execute('INSERT INTO reply_intents VALUES(?,?,?,?,?,NULL)',(event.message_id,logical_id,hashlib.sha256(source.chat_id.encode()).hexdigest(),hashlib.sha256(result.encode()).hexdigest(),'pending'))
        return {'handled':True,'text':result,'board':lane.board}

    async def deliver(self,adapter,event):
        """Complete one quick reply through the existing adapter's receipt guard.

        Claimed/uncertain sends are never replayed without external receipt
        reconciliation. A completed backend with no send claim is resumable.
        """
        if not self.matches(event):raise Unavailable('owner return unavailable')
        source=event.source
        if source.chat_type!='dm' or getattr(source,'profile',None) or source.chat_id!=self._admission.destination:
            raise Unavailable('owner return unavailable')
        event_digest=hashlib.sha256(json.dumps([source.user_id,source.chat_id,event.text],separators=(',',':')).encode()).hexdigest()
        return await self._deliver_saved(adapter,event.message_id,event_digest)

    async def _deliver_saved(self,adapter,source_id,event_digest=None):
        route=await self._authority(self.current_route)
        if not route.enabled or route.generation<1 or not route.isolation_receipt or route.phone_digest!=self._admission.phone_digest:
            raise Unavailable('owner return unavailable')
        destination=self._admission.destination
        destination_digest=hashlib.sha256(destination.encode()).hexdigest()
        logical_id='josh-reply-'+hashlib.sha256(json.dumps([route.phone_digest,source_id],separators=(',',':')).encode()).hexdigest()
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT digest,generation,state,result FROM inbound WHERE message=?',(source_id,)).fetchone()
            binding=c.execute('SELECT phone_digest,destination_digest,generation,event_digest,endpoint_digest FROM source_bindings WHERE message=?',(source_id,)).fetchone()
            expected=(route.phone_digest,destination_digest,route.generation,row[0] if row else None,hashlib.sha256(route.endpoint.encode()).hexdigest())
            if not row or binding!=expected or (event_digest is not None and row[0]!=event_digest) or row[1]!=route.generation:
                raise Unavailable('reply source mismatch')
            if row[2]!='complete':return {'handled':True,'state':'backend_uncertain'}
            content=row[3]
            if not isinstance(content,str) or not content:raise Unavailable('reply content unavailable')
            payload_digest=hashlib.sha256(content.encode()).hexdigest()
            existing=c.execute('SELECT logical_id,destination_digest,payload_digest,state,receipt FROM reply_intents WHERE message=?',(source_id,)).fetchone()
            if existing:
                if existing[:3]!=(logical_id,destination_digest,payload_digest):raise Unavailable('reply intent mismatch')
                if existing[3]!='pending':return {'handled':True,'state':existing[3],'receipt':existing[4]}
            else:
                raise Unavailable('reply intent missing')
        # Persist intent first. Current revocation remains authoritative before claim.
        if await self._authority(self.current_route)!=route:raise Unavailable('owner return changed')
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("UPDATE reply_intents SET state='sending' WHERE message=? AND state='pending'",(source_id,)).rowcount!=1:
                return {'handled':True,'state':'already_claimed'}
        # Final fresh check completed before CAS; no await from claim to send.
        try:
            receipt=await adapter.send(destination,content,metadata={'logical_message_id':logical_id})
            message_id=getattr(receipt,'message_id',None)
            success=getattr(receipt,'success',False) is True and isinstance(message_id,str) and bool(message_id.strip())
        except BaseException:
            with database(self.db) as c:c.execute("UPDATE reply_intents SET state='uncertain' WHERE message=? AND state='sending'",(source_id,))
            raise
        state='delivered' if success else 'uncertain'
        with database(self.db) as c:
            c.execute("UPDATE reply_intents SET state=?,receipt=? WHERE message=? AND state='sending'",(state,message_id if success else None,source_id))
        return {'handled':True,'state':state,'receipt':message_id if success else None}

    async def recover_pending(self,adapter,limit=25):
        """Bounded adapter lifecycle hook; no scheduler, no uncertain reset."""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded recovery limit required')
        counts={'examined':0,'delivered':0,'withheld':0}
        try:
            with database(self.db) as c:
                c.execute('BEGIN IMMEDIATE')
                cursor=c.execute('SELECT last_row FROM recovery_cursor WHERE slot=1').fetchone()
                after=cursor[0] if cursor else 0
                rows=c.execute("SELECT rowid,message FROM reply_intents WHERE state='pending' AND rowid>? ORDER BY rowid LIMIT ?",(after,limit)).fetchall()
                if not rows:
                    rows=c.execute("SELECT rowid,message FROM reply_intents WHERE state='pending' ORDER BY rowid LIMIT ?",(limit,)).fetchall()
                if rows:c.execute('INSERT OR REPLACE INTO recovery_cursor VALUES(1,?)',(rows[-1][0],))
        except Exception:
            return dict(counts,unavailable=True)
        for (_,source_id) in rows:
            counts['examined']+=1
            try:
                result=await self._deliver_saved(adapter,source_id)
                counts['delivered' if result['state']=='delivered' else 'withheld']+=1
            except Exception:
                counts['withheld']+=1
        return counts

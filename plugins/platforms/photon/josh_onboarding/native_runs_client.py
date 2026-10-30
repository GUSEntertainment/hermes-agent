"""Inactive sibling owner transport. Same reply ledger; no scheduler or sends."""
import asyncio
import hashlib
import json
import re
import time
import urllib.request
from .photon_owner_handler import JoshPhotonHandler,Unavailable,database

class NativeRunsOwnerHandler(JoshPhotonHandler):
    def __init__(self,*args,receipts,budget=6,**kwargs):
        super().__init__(*args,**kwargs)
        self.receipts=receipts;self.budget=budget
        with database(self.db) as c:c.execute('CREATE TABLE IF NOT EXISTS native_runs(message TEXT PRIMARY KEY,handle TEXT NOT NULL,run_id TEXT)')

    @staticmethod
    def _http(endpoint,headers,body,timeout):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**kw):return None
        req=urllib.request.Request(endpoint,data=json.dumps(body).encode() if body is not None else None,headers=headers,method='POST' if body is not None else 'GET')
        with urllib.request.build_opener(NoRedirect).open(req,timeout=timeout) as response:raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise Unavailable('Bounded native response required')
        return json.loads(raw)

    async def handle(self,event):
        if not self.matches(event):return None
        deadline=time.monotonic()+self.budget
        route=await self._authority(self.current_route,deadline=deadline);source=event.source
        if not route.enabled or not route.isolation_receipt or route.generation<1 or route.phone_digest!=self._admission.phone_digest or source.chat_type!='dm' or source.chat_id!=self._admission.destination or getattr(source,'profile',None) or getattr(event,'internal',False) or event.message_type.value!='text':raise Unavailable('Current private source required')
        capture=getattr(event,'_secure_intake_capture',None)
        # Receipt validates actual sealed capture. Event/body must still be exact.
        if type(event.text) is not str or len(event.text)>10000 or capture is None or hashlib.sha256(event.text.encode()).hexdigest()!=capture.digest or capture.transport!=('photon','dm',source.chat_id,source.user_id,event.message_id):raise Unavailable('Exact captured current text required')
        enrollment=json.loads(await self._authority(self.receipts._enrollment,capture.transport,deadline=deadline))
        if route.endpoint!=enrollment['endpoint'] or not route.endpoint.endswith('/v1/runs'):raise Unavailable('Reviewed native runs endpoint required')
        handle=await self._authority(self.receipts.publish,capture,deadline=deadline)
        event_digest=hashlib.sha256(json.dumps([source.user_id,source.chat_id,event.text],separators=(',',':')).encode()).hexdigest()
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT digest,generation,state FROM inbound WHERE message=?',(event.message_id,)).fetchone()
            native=c.execute('SELECT handle,run_id FROM native_runs WHERE message=?',(event.message_id,)).fetchone()
            if row:
                if row[0]!=event_digest or row[1]!=route.generation or not native or native[0]!=handle:raise Unavailable('Conflicting replay')
                if row[2]=='complete':return {'handled':True,'duplicate':True,'text':None}
                if not native[1]:return {'handled':True,'state':'uncertain','text':None}
                run_id=native[1]
            else:
                run_id=None
                c.execute('INSERT INTO inbound VALUES(?,?,?,?,NULL)',(event.message_id,event_digest,route.generation,'started'))
                c.execute('INSERT INTO source_bindings VALUES(?,?,?,?,?,?)',(event.message_id,route.phone_digest,hashlib.sha256(source.chat_id.encode()).hexdigest(),route.generation,event_digest,hashlib.sha256(route.endpoint.encode()).hexdigest()))
                c.execute('INSERT INTO native_runs VALUES(?,?,NULL)',(event.message_id,handle))
        async def current():
            if await self._authority(self.current_route,deadline=deadline)!=route or await self._authority(self.receipts.publish,capture,deadline=deadline)!=handle:raise Unavailable('Owner authority changed')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Polling budget exhausted')
            return remaining
        headers={'Authorization':'Bearer '+await self._authority(self.current_key,deadline=deadline),'Content-Type':'application/json','X-Hermes-Intake-Receipt':handle,'Idempotency-Key':'photon-run-'+hashlib.sha256((route.phone_digest+event.message_id).encode()).hexdigest()}
        async def request(url,body):
            remaining=await current()
            request_headers=headers if body is not None else {'Authorization':headers['Authorization']}
            value=await asyncio.wait_for(asyncio.to_thread(self.transport,url,request_headers,body,remaining),remaining)
            await current()
            return value
        try:
            if run_id is None:
                value=await request(route.endpoint,{'input':event.text})
                run_id=value.get('run_id')
                if type(run_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',run_id):raise Unavailable('Native run ID required')
                with database(self.db) as c:c.execute('UPDATE native_runs SET run_id=? WHERE message=?',(run_id,event.message_id))
            while True:
                value=await request(route.endpoint+'/'+run_id,None)
                if value.get('run_id',run_id)!=run_id:raise Unavailable('Wrong native run')
                if value.get('status')=='completed':
                    result=value.get('output')
                    if type(result) is not str or not result or len(result)>100000:raise Unavailable('Verified native output required')
                    logical='josh-reply-'+hashlib.sha256(json.dumps([route.phone_digest,event.message_id],separators=(',',':')).encode()).hexdigest()
                    with database(self.db) as c:
                        c.execute('BEGIN IMMEDIATE')
                        previous=c.execute('SELECT state,result FROM inbound WHERE message=?',(event.message_id,)).fetchone()
                        if previous[0]=='complete':
                            if previous[1]!=result:raise Unavailable('Conflicting native output')
                            return {'handled':True,'duplicate':True,'text':None}
                        c.execute("UPDATE inbound SET state='complete',result=? WHERE message=?",(result,event.message_id))
                        c.execute('INSERT OR IGNORE INTO reply_intents VALUES(?,?,?,?,?,NULL)',(event.message_id,logical,hashlib.sha256(source.chat_id.encode()).hexdigest(),hashlib.sha256(result.encode()).hexdigest(),'pending'))
                    return {'handled':True,'text':result,'state':'complete'}
                if value.get('status') in ('failed','cancelled','interrupted'):
                    return {'handled':True,'state':value['status'],'text':None}
                await asyncio.sleep(min(.1,await current()))
        except TimeoutError:
            return {'handled':True,'state':'pending' if run_id else 'uncertain','text':None}
        except Exception:
            return {'handled':True,'state':'unverified','text':None}

    async def recover_pending(self,adapter,limit=25):
        """Existing connect/reconnect recovery hook: GET known runs, then deliver.

        Source expiry prevents new admission, not retrieval of an already saved
        native run. Current enrollment/revocation and exact durable route still
        gate each read/return. Never retry a POST whose ID was not saved.
        """
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            cursor=c.execute('SELECT last_row FROM recovery_cursor WHERE slot=2').fetchone()
            after=cursor[0] if cursor else 0
            query="SELECT n.rowid,n.message,n.handle,n.run_id FROM native_runs n JOIN inbound i ON i.message=n.message WHERE i.state!='complete' AND n.rowid>? ORDER BY n.rowid LIMIT ?"
            selected=c.execute(query,(after,max(1,min(limit,25)))).fetchall()
            if not selected:selected=c.execute(query,(0,max(1,min(limit,25)))).fetchall()
            if selected:c.execute('INSERT OR REPLACE INTO recovery_cursor VALUES(2,?)',(selected[-1][0],))
            rows=[row[1:] for row in selected]
        for message,handle,run_id in rows:
            try:
                route=await self._authority(self.current_route)
                def authority():
                    self.receipts._current_storage()
                    with self.receipts.registry._db() as r:
                        receipt=r.execute('SELECT * FROM transport_receipts WHERE handle=?',(handle,)).fetchone()
                    if not receipt or receipt['revoked']:raise Unavailable('Revoked source')
                    t=json.loads(receipt['transport']);p=json.loads(receipt['enrollment'])
                    if self.receipts._enrollment(t)!=receipt['enrollment'] or self.current_route()!=route or not route.enabled or not route.isolation_receipt or route.endpoint!=p['endpoint'] or route.phone_digest!=self._admission.phone_digest:raise Unavailable('Owner changed')
                    with database(self.db) as c:
                        source=c.execute('SELECT phone_digest,destination_digest,generation,endpoint_digest FROM source_bindings WHERE message=?',(message,)).fetchone()
                    if source!=(route.phone_digest,hashlib.sha256(self._admission.destination.encode()).hexdigest(),route.generation,hashlib.sha256(route.endpoint.encode()).hexdigest()) or t[4]!=message:raise Unavailable('Source changed')
                    self.receipts._current_storage()
                    return dict(receipt)
                durable = await self._authority(authority)
                if run_id is None:
                    claimed = durable.get("run")
                    if claimed:
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",claimed):raise Unavailable("Invalid claimed run")
                        with database(self.db) as c:c.execute("UPDATE native_runs SET run_id=? WHERE message=? AND run_id IS NULL",(claimed,message))
                        run_id=claimed
                    elif durable["expires"]<=self.receipts.clock():
                        self._save_failure_reply(message, route, "Sorry — I lost the connection before I could start on that. Nothing was done yet; could you send it again?")
                        continue
                    else:continue
                headers={'Authorization':'Bearer '+await self._authority(self.current_key)}
                value=await asyncio.wait_for(asyncio.to_thread(self.transport,route.endpoint+'/'+run_id,headers,None,self.budget),self.budget)
                await self._authority(authority)
                if value.get('run_id',run_id)!=run_id:continue
                if value.get('status') in ('failed','cancelled','interrupted'):
                    self._save_failure_reply(message,route,"That request stopped before I could deliver a result. I can’t confirm whether any computer action finished, so I won’t repeat it automatically.")
                    continue
                if value.get('status')!='completed':continue
                result=value.get('output')
                if type(result) is not str or not result or len(result)>100000:continue
                logical='josh-reply-'+hashlib.sha256(json.dumps([route.phone_digest,message],separators=(',',':')).encode()).hexdigest()
                with database(self.db) as c:
                    c.execute('BEGIN IMMEDIATE')
                    previous=c.execute('SELECT state,result FROM inbound WHERE message=?',(message,)).fetchone()
                    if previous[0]=='complete':
                        if previous[1]!=result:raise Unavailable('Conflicting native output')
                        continue
                    c.execute("UPDATE inbound SET state='complete',result=? WHERE message=?",(result,message))
                    c.execute('INSERT OR IGNORE INTO reply_intents VALUES(?,?,?,?,?,NULL)',(message,logical,hashlib.sha256(self._admission.destination.encode()).hexdigest(),hashlib.sha256(result.encode()).hexdigest(),'pending'))
            except Exception:continue
        return await super().recover_pending(adapter,limit=limit)

    def _save_failure_reply(self, message, route, result):
        # A terminal response is not task success. Preserve the source/run and
        # use the same existing receipt-protected, once-only delivery ledger.
        logical='josh-reply-'+hashlib.sha256(json.dumps([route.phone_digest,message],separators=(',',':')).encode()).hexdigest()
        with database(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT state FROM inbound WHERE message=?',(message,)).fetchone()
            if not row or row[0]=='complete':return
            c.execute("UPDATE inbound SET state='complete',result=? WHERE message=?",(result,message))
            c.execute('INSERT OR IGNORE INTO reply_intents VALUES(?,?,?,?,?,NULL)',(message,logical,hashlib.sha256(self._admission.destination.encode()).hexdigest(),hashlib.sha256(result.encode()).hexdigest(),'pending'))

    async def _deliver_saved(self,adapter,source_id,event_digest=None):
        with database(self.db) as c:native=c.execute('SELECT handle FROM native_runs WHERE message=?',(source_id,)).fetchone()
        if not native:raise Unavailable('Native source required')
        def verify_receipt():
            self.receipts._current_storage()
            with self.receipts.registry._db() as db:r=db.execute('SELECT * FROM transport_receipts WHERE handle=?',(native[0],)).fetchone()
            if not r or r['revoked'] or self.receipts._enrollment(json.loads(r['transport']))!=r['enrollment']:raise Unavailable('Return authority revoked')
            self.receipts._current_storage()
        await self._authority(verify_receipt)
        return await super()._deliver_saved(adapter,source_id,event_digest)

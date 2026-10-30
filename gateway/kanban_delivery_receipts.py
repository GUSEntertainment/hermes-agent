"""Durable completed-result intent, scoped to an existing authorized subscription.

This module grants no delivery authority. The watcher re-runs its ordinary
profile/topic/report-back checks before calling deliver on recovered intents.
"""
import hashlib,json,os,stat,tempfile
from pathlib import Path
from hermes_cli import kanban_db as kb

class DeliveryHeld(RuntimeError): pass

def sha(data):return hashlib.sha256(data).hexdigest()
def identity(sub):
 return sha(json.dumps([sub.get(k) or '' for k in ('task_id','platform','chat_id','thread_id','notifier_profile','user_id','user_id_alt','chat_type','delivery_mode','delivery_metadata')],separators=(',',':'),sort_keys=True).encode())
def schema(c):
 c.execute('CREATE TABLE IF NOT EXISTS kanban_return_events (subscription TEXT NOT NULL,event_id INTEGER NOT NULL,state TEXT NOT NULL DEFAULT "pending",event_json TEXT NOT NULL,PRIMARY KEY(subscription,event_id))')
 c.execute('CREATE TABLE IF NOT EXISTS kanban_return_parts (subscription TEXT NOT NULL,event_id INTEGER NOT NULL,part TEXT NOT NULL,content_hash TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,PRIMARY KEY(subscription,event_id,part))')

def stage_and_recover(c,sub):
 """Persist before legacy cursor claim; crash leaves recoverable pending intent."""
 schema(c)
 with kb.write_txn(c):
  _,events=kb.unseen_events_for_sub(c,task_id=sub['task_id'],platform=sub['platform'],chat_id=sub['chat_id'],thread_id=sub.get('thread_id'),kinds=('completed',))
  for ev in events:
   snapshot=json.dumps({key:getattr(ev,key) for key in ('id','task_id','kind','payload','created_at','run_id')},sort_keys=True)
   c.execute('INSERT OR IGNORE INTO kanban_return_events(subscription,event_id,event_json) VALUES (?,?,?)',(identity(sub),ev.id,snapshot))
 recovered=[]
 for row in c.execute('SELECT event_id,event_json FROM kanban_return_events WHERE subscription=? AND state="pending"',(identity(sub),)):
  event=json.loads(row['event_json'])
  if event['id']!=row['event_id'] or event['task_id']!=sub['task_id'] or event['kind']!='completed':raise DeliveryHeld('Invalid persisted completion intent')
  recovered.append(kb.Event(**event))
 return recovered

def reviewed_artifacts(c,task_id):
 """Current independent PASS plus exact immutable native attachment bytes."""
 task=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
 goal=c.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(task_id,)).fetchone()
 if not goal:return []
 if not task or task['status']!='done' or task['current_run_id'] is not None or goal['phase']!='completed':raise DeliveryHeld('Review not currently completed')
 maker=c.execute("SELECT profile FROM task_runs WHERE task_id=? AND outcome='review_requested' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
 reviewer=goal['reviewer_profile'];version=goal['current_state_version'];digest=goal['review_artifact_digest']
 bare=str(digest or '').removeprefix('sha256:')
 if not isinstance(version,int) or version<1 or not maker or maker[0]==reviewer or not reviewer or goal['required_reviewer_profile']!=reviewer or task['assignee']!=reviewer or task['current_state_version']!=version or goal['review_state_version']!=version:raise DeliveryHeld('Independent current reviewer proof missing')
 verdicts=c.execute('SELECT * FROM review_verdicts WHERE task_id=? AND review_round=(SELECT MAX(review_round) FROM review_verdicts WHERE task_id=?)',(task_id,task_id)).fetchall()
 if not verdicts or any(v['verdict']!='PASS' or v['reviewer_profile']!=reviewer or v['state_version']!=version or v['artifact_digest']!=digest or v['artifact_ref']!=goal['review_artifact_ref'] for v in verdicts):raise DeliveryHeld('Current PASS does not bind artifact')
 db=Path(c.execute('PRAGMA database_list').fetchone()[2]);root=db.parent/'attachments'/task_id;out=[]
 for row in c.execute('SELECT * FROM task_attachments WHERE task_id=? ORDER BY id LIMIT 30',(task_id,)):
  path=Path(row['stored_path']);name=row['filename']
  if Path(name).name!=name or path!=root/name:continue
  try:
   if any(p.is_symlink() for p in [path,*path.parents]):continue
   fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
   with os.fdopen(fd,'rb') as f:
    before=os.fstat(f.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.getuid() or before.st_mode&0o022 or before.st_size!=row['size'] or before.st_size>25*1024*1024:continue
    data=f.read(25*1024*1024+1);after=os.fstat(f.fileno())
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):continue
   if sha(data)==bare:out.append({'id':row['id'],'name':name,'bytes':data,'sha256':bare})
  except OSError:continue
 if Path(str(goal['review_artifact_ref'])).is_absolute() and not out:raise DeliveryHeld('Reviewed durable artifact missing or changed')
 return out[:1] # Current native review binds one artifact; identical copies deduplicate.

def begin(c,sub,event_id,part,digest):
 with kb.write_txn(c):
  row=c.execute('SELECT * FROM kanban_return_parts WHERE subscription=? AND event_id=? AND part=?',(identity(sub),event_id,part)).fetchone()
  if row:
   if row['content_hash']!=digest:raise DeliveryHeld('Prepared result changed')
   if row['state']=='accepted':return False
   raise DeliveryHeld('Delivery uncertain; reconcile provider receipt before retry')
  c.execute('INSERT INTO kanban_return_parts VALUES (?,?,?,?,?,NULL)',(identity(sub),event_id,part,digest,'inflight'))
 return True

def finish(c,sub,event_id,part,state,receipt):
 with kb.write_txn(c):c.execute('UPDATE kanban_return_parts SET state=?,receipt=? WHERE subscription=? AND event_id=? AND part=?',(state,json.dumps(receipt,sort_keys=True),identity(sub),event_id,part))

async def deliver(c,sub,event_id,text,adapter,metadata,*,allow_artifacts=True):
 schema(c)
 from gateway.owner_delegation import verify_return
 verify_return(c,sub)
 from gateway.secondary_documents import current as secondary_current
 try:secondary = secondary_current(c, sub)
 except PermissionError as exc:raise DeliveryHeld("Secondary document authorization unavailable") from exc
 # Resolve and validate bytes before first external effect. Trusted/redacted
 # report-back never gains raw artifact access through this path.
 artifacts=reviewed_artifacts(c,sub['task_id']) if allow_artifacts else []
 if secondary and (not allow_artifacts or not artifacts):raise DeliveryHeld('Secondary document requires current approved artifact')
 if sub.get('platform') == 'api_server':
  from gateway.owner_app_returns import persist
  persist(c,sub,event_id,text,artifacts)
  return
 member=sub.get('platform') in ('photon','api_server')
 if member:
  from gateway.kanban_watchers import member_completion_text
  _t=kb.get_task(c,sub['task_id'])
  text=member_completion_text(getattr(_t,'title','') or '',bool(artifacts))
 texts=[text[i:i+1800] for i in range(0,len(text),1800)] or ['']
 sent_any=False
 parts=[(f'text:{i}',sha(value.encode()),value,None) for i,value in enumerate(texts)]+[(f"attachment:{a['id']}",a['sha256'],None,a) for a in artifacts]
 for part,digest,content,artifact in parts:
  verify_return(c,sub)
  try:secondary_current(c, sub)
  except PermissionError as exc:raise DeliveryHeld("Secondary document authorization no longer current") from exc
  current=kb.list_notify_subs(c,sub['task_id'],include_internal_metadata=True)
  if not any(identity(row)==identity(sub) for row in current):raise DeliveryHeld('Return subscription revoked or changed')
  current_task=kb.get_task(c,sub['task_id'])
  if current_task is None or current_task.status!='done' or current_task.current_run_id is not None:raise DeliveryHeld('Task is no longer completed')
  if artifact is not None:
   if not any(a['id']==artifact['id'] and a['sha256']==digest for a in reviewed_artifacts(c,sub['task_id'])):raise DeliveryHeld('Artifact approval changed before upload')
  # Known disconnect is a preflight hold, not an ambiguous network attempt.
  if getattr(getattr(adapter,'platform',None),'value',None)=='telegram' and getattr(adapter,'_bot',None) is None:
   raise DeliveryHeld('Authorized Telegram adapter unavailable before send; waiting for reconnect')
  photon=getattr(getattr(adapter,'platform',None),'value',None)=='photon'
  if photon and getattr(adapter,'_http_client',None) is None:
   raise DeliveryHeld('Authorized Photon adapter unavailable before send; waiting for reconnect')
  part_metadata=dict(metadata)
  if photon:part_metadata['logical_message_id']='native-return:'+sha(f'{identity(sub)}:{event_id}:{part}'.encode())
  if not begin(c,sub,event_id,part,digest):continue
  sent_any=True
  try:
   if artifact is None:result=await send_once(adapter,sub['chat_id'],part_metadata,text=content)
   else:
    # Upload an immutable private snapshot; never let adapter reopen mutable
    # task workspace paths after hash validation.
    with tempfile.TemporaryDirectory(prefix='hermes-reviewed-return-') as tmp:
     name,data=artifact['name'],artifact['bytes']
     if member:
      from gateway.member_document import member_file
      name,data=member_file(name,data,getattr(kb.get_task(c,sub['task_id']),'title','') or '')
     path=Path(tmp)/name;path.write_bytes(data);path.chmod(0o600)
     result=await send_once(adapter,sub['chat_id'],part_metadata,path=str(path))
   message_id=getattr(result,'message_id',None)
   telegram=getattr(getattr(adapter,'platform',None),'value',None)=='telegram'
   legacy_no_receipt=result is None and not telegram and not photon
   if not legacy_no_receipt and (getattr(result,'success',False) is not True or not message_id):raise DeliveryHeld('Provider acceptance/message ID unconfirmed')
   finish(c,sub,event_id,part,'accepted',{'message_id':str(message_id) if message_id else None,'receipt_quality':'adapter_completed_without_id' if legacy_no_receipt else ('sidecar_message_id' if photon else 'provider_message_id'),'content':content,'attachment_id':artifact['id'] if artifact else None,'sha256':digest,'chat_id':sub['chat_id'],'thread_id':sub.get('thread_id'),'profile':sub.get('notifier_profile'),'transport_guarantee':'single_attempt' if str(getattr(getattr(adapter,'platform',None),'value',''))=='telegram' else ('single_sidecar_attempt' if photon else 'adapter_boundary_only'),'logical_message_id':part_metadata.get('logical_message_id')})
  except BaseException as exc:
   finish(c,sub,event_id,part,'uncertain',{'reason':type(exc).__name__,'sha256':digest})
   if not isinstance(exc,Exception):raise
   raise DeliveryHeld('Result delivery held uncertain; no automatic retry') from exc
 with kb.write_txn(c):c.execute('UPDATE kanban_return_events SET state="accepted" WHERE subscription=? AND event_id=?',(identity(sub),event_id))
 # Accepted return note -> owner's live DM transcript, so a reply has context.
 try:
  from gateway.mirror import find_owner_dm_session,mirror_proactive_message
  plat=str(sub.get('platform') or getattr(getattr(adapter,'platform',None),'value','') or '')
  sid=find_owner_dm_session(plat,str(sub['chat_id']),sub.get('thread_id'))
  if sid and sent_any:mirror_proactive_message(plat,str(sub['chat_id']),text,source='delegated work return',ref=f"task {sub['task_id']}",thread_id=sub.get('thread_id'),session_id=sid)
 except Exception:pass


def record_problem(c,sub,event_id,reason):
 schema(c)
 part='problem:'+sha(reason.encode())
 with kb.write_txn(c):
  inserted=c.execute('INSERT OR IGNORE INTO kanban_return_parts VALUES (?,?,?,?,?,?)',(identity(sub),event_id,part,sha(reason.encode()),'held',json.dumps({'reason':reason}))).rowcount
  if inserted:kb._append_event(c,sub['task_id'],'delivery_problem',{'event_id':event_id,'reason':reason,'action':'Reconcile provider receipt or repair verified artifact; do not resend uncertain parts.'})


async def send_once(adapter,chat_id,metadata,*,text=None,path=None):
 if getattr(getattr(adapter,'platform',None),'value',None)!='telegram':
  return await adapter.send(chat_id,text,metadata=metadata) if path is None else await adapter.send_document(chat_id=chat_id,file_path=path,metadata=metadata)
 # Reuse the already selected/authorized live bot and its native topic routing.
 # Bypass retries/format fallbacks only for this durable delivery path.
 from gateway.platforms.base import SendResult
 from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id
 if adapter._bot is None:raise DeliveryHeld('Authorized Telegram adapter disconnected')
 thread=adapter._metadata_thread_id(metadata)
 kwargs=adapter._thread_kwargs_for_send(chat_id,thread,metadata,reply_to_message_id=None,reply_to_mode=adapter._reply_to_mode)
 kwargs.update(adapter._notification_kwargs(metadata))
 if path is None:
  msg=await adapter._bot.send_message(chat_id=normalize_telegram_chat_id(chat_id),text=text,**kwargs)
 else:
  with open(path,'rb') as f:msg=await adapter._bot.send_document(chat_id=normalize_telegram_chat_id(chat_id),document=f,filename=Path(path).name,**kwargs)
 return SendResult(success=True,message_id=str(msg.message_id))

"""Secondary document intent core; no sender, scheduler, or model-facing tool.

Only the fixed protected app request ledger and fresh company/enrollment
authority can authorize this lane. Caller JSON is never an authority source. Existing notifier retains all transport authorization.
"""
import hashlib
import json
import math
import time
import sqlite3
import re
from pathlib import Path
from hermes_cli import kanban_db as kb

MARKER = 'secondary_document_request'

class Held(PermissionError):
    pass

def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

POLICY = Path('/Users/Chris/.hermes/private/document-delivery/policy.json')

def resolve_request(request_id):
    """Only fixed protected policy/app ledger and canonical enrollment confer scope."""
    from gateway import app_business_authority as auth
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id):
        raise Held('Invalid request identifier')
    raw = auth.protected_bytes(POLICY, private=True, anchor=auth.ROOT)
    policy = json.loads(raw)
    if set(policy) != {'version','enabled_from','app_db','automatic_owners','notifier_profiles'} or policy['version'] != 'desk-imessage-v1' or policy['automatic_owners'] != ['chris']:
        raise Held('Protected delivery policy unavailable')
    db = Path(policy['app_db'])
    before = auth.protected(db, auth.ROOT.parent)
    with __import__('contextlib').closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=1)) as app:
        app.row_factory = sqlite3.Row
        if app.execute("SELECT 1 FROM sqlite_master WHERE name='private_store_identity'").fetchone():
            raise Held('Private store cannot become company delivery')
        row = app.execute("""SELECT r.*,h.owner,h.taskId,h.turnId,h.conversationId,h.title,
          t.createdAt AS source_created_at,t.message AS source_message,c.owner AS conversation_owner,c.domain
          FROM document_delivery_requests r JOIN handoffs h ON h.id=r.handoff_id
          JOIN turns t ON t.id=r.source_request_id JOIN conversations c ON c.id=h.conversationId
          WHERE r.id=? AND t.id=h.turnId AND t.conversationId=c.id""",(request_id,)).fetchone()
        if row is None and app.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_document_delivery_requests'").fetchone():
            row=app.execute('''SELECT r.*,NULL AS handoff_id,c.owner,r.task_id AS taskId,t.id AS turnId,c.id AS conversationId,
              '' AS title,t.createdAt AS source_created_at,t.message AS source_message,c.owner AS conversation_owner,c.domain,
              t.runId AS source_run_id,c.sessionId AS source_session_id,c.binding AS source_binding
              FROM native_document_delivery_requests r JOIN turns t ON t.id=r.source_request_id
              JOIN conversations c ON c.id=t.conversationId WHERE r.id=?''',(request_id,)).fetchone()
            if row and (row['run_id']!=row['source_run_id'] or row['session_id']!=row['source_session_id'] or row['binding']!=row['source_binding']):
                raise Held('Native app source changed')
        filings = [] if row is None else [dict(f) for f in app.execute('SELECT filingKey,result FROM delegation_filings WHERE owner=? AND turnId=?',(row['owner'],row['turnId']))]
    after = auth.protected(db, auth.ROOT.parent)
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or row is None:
        raise Held('App request unavailable or replaced')
    r=dict(row);owner=r['owner']
    if r['domain']!='gus' or owner!=r['conversation_owner'] or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',owner) or r['policy_version']!=policy['version']:
        raise Held('Owned company request required')
    if r['mode'] not in ('explicit','urgent','upcoming') or (r['mode']!='explicit' and owner not in policy['automatic_owners']):
        raise Held('No secondary delivery opt-in')
    profile='app-gus-'+owner+'-margot'
    authority=auth.current(profile,owner)
    native_source=None
    if r.get('handoff_id') is None:
        if r.get('profile')!=profile or r.get('board')!=authority['board'] or not re.fullmatch(r'run_[A-Za-z0-9_-]{1,100}',r.get('run_id','')) or not r.get('session_id'):
            raise Held('Native app run binding mismatch')
        native_source={k:r[k] for k in ('run_id','session_id','profile','board','binding')}

    # Canonical enrollment validates current profile, registry and membership.
    # Its protected source is reread around extraction of the destination.
    enrollment=auth.canonical_enrollment(owner,auth.ROOT)
    profiles_path=auth.ROOT/'private'/'phone-assistant'/'caller-profiles.json'
    profile_raw=auth.protected_bytes(profiles_path,True,auth.ROOT)
    matches=[v for v in json.loads(profile_raw)['profiles'] if v.get('profile_key')==owner]
    if len(matches)!=1 or matches[0].get('enabled', True) is not True or matches[0].get('revoked_at'):
        raise Held('Current owner destination unavailable')
    peer=matches[0].get('phone_e164','')
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}',peer) or matches[0].get('board')!=authority['board']:
        raise Held('Canonical owner destination mismatch')
    notifier=policy['notifier_profiles'].get(owner)
    if not isinstance(notifier,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,99}',notifier):
        raise Held('Existing authorized notifier profile required')
    if auth.canonical_enrollment(owner,auth.ROOT)!=enrollment or auth.protected_bytes(profiles_path,True,auth.ROOT)!=profile_raw or auth.protected_bytes(POLICY,True,auth.ROOT)!=raw:
        raise Held('Current authority changed during lookup')
    target=dict(platform='photon',chat_id='any;-;'+peer,thread_id=None,user_id=peer,user_id_alt=None,
                chat_type='dm',notifier_profile=notifier,delivery_mode='notify',delivery_metadata={MARKER:request_id})
    candidates=[]
    for filing in filings:
        if not isinstance(filing['result'],str):continue
        result=json.loads(filing['result'])
        if result.get('ok') is True and result.get('taskId')==r['taskId'] and re.fullmatch(r'app-'+re.escape(r['turnId'])+r'-[0-9]+',filing['filingKey']):
            candidates.append(dict(key=filing['filingKey']+('-claire' if result.get('viaClaire') is True else ''),title=r['title']))
    if len(candidates)>1:raise Held('Ambiguous original filing')
    filing=candidates[0] if candidates else None
    source={k:r[k] for k in ('id','handoff_id','source_request_id','mode','deadline','policy_version','created_at','revoked_at','owner','taskId','turnId','conversationId','source_created_at','domain','title')}
    if native_source is not None:filing={'native_app':native_source}
    source['filing']=filing
    source['message_sha256']=hashlib.sha256(r['source_message'].encode()).hexdigest()
    return dict(request_id=request_id,task_id=r['taskId'],owner=owner,board=authority['board'],profile=profile,
                created_at=r['created_at']/1000,source_created_at=r['source_created_at']/1000,
                enabled_from=policy['enabled_from']/1000,mode='explicit_imessage' if r['mode']=='explicit' else 'time_sensitive',
                deadline=r['deadline']/1000 if r['deadline'] is not None else (r['created_at']/1000+86400 if r['mode']=='urgent' else None),revoked=r['revoked_at'] is not None,
                consented=True,authority=authority,target=target,filing=filing,source_digest=digest({'source':source,'enrollment':enrollment,'policy':policy}))

def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS secondary_document_intents (request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, authority_json TEXT NOT NULL, subscription TEXT NOT NULL UNIQUE)')

def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def validate(c, request_id, *, now=None):
    a = resolve_request(request_id)
    now = time.time() if now is None else now
    required = {'request_id','task_id','owner','board','profile','created_at','source_created_at','enabled_from','mode','deadline','revoked','consented','authority','target','filing','source_digest'}
    if not isinstance(a, dict) or set(a) != required or a['request_id'] != request_id:
        raise Held('Exact protected request required')
    if a['revoked'] or a['consented'] is not True or a['mode'] not in ('explicit_imessage','time_sensitive'):
        raise Held('No current secondary delivery consent')
    if not all(finite(a[k]) for k in ('created_at','source_created_at','enabled_from')) or not finite(now) or not a['enabled_from'] <= a['source_created_at'] <= a['created_at'] <= now:
        raise Held('Request outside activation window')
    if a['mode'] == 'time_sensitive' and (not finite(a['deadline']) or a['deadline'] < now):
        raise Held('Time-sensitive request no longer current')
    authority = a['authority']
    if authority.get('scope_contract') != 'gus-company-work-v1' or authority.get('principal') != a['owner'] or authority.get('profile') != a['profile'] or authority.get('board') != a['board']:
        raise Held('Current company authority mismatch')
    db = next((r[2] for r in c.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not db or Path(db).name != 'kanban.db' or Path(db).parent.name != a['board']:
        raise Held('Wrong native board')
    task = c.execute('SELECT * FROM tasks WHERE id=?', (a['task_id'],)).fetchone()
    row = c.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (a['task_id'],)).fetchone()
    if not task or task['created_at'] < a['enabled_from']:
        raise Held('No eligible original task')
    if row:
        binding = json.loads(row[0])
        if binding.get('kind') != 'native-api-return-v1' or binding.get('authority') != authority or binding.get('profile') != a['profile'] or binding.get('board') != a['board'] or binding.get('session_id') != task['session_id'] or not binding.get('run_id'):
            raise Held('Original native return authority mismatch')
        if isinstance(a['filing'],dict) and 'native_app' in a['filing']:
            original=a['filing']['native_app']
            if binding.get('profile')!=original['profile'] or binding.get('board')!=original['board'] or binding.get('run_id')!=original['run_id'] or binding.get('session_id')!=original['session_id'] or binding.get('target',{}).get('platform')!='api_server':
                raise Held('Native task did not originate from this app turn')

    else:
        filing=a['filing']
        if isinstance(filing,dict) and 'native_app' in filing:raise Held('Native app task requires immutable return intent')
        from hermes_cli.owner_cell import _routing_metadata
        meta=_routing_metadata(task['body'])
        if not filing or task['idempotency_key']!=filing['key'] or task['title']!=filing['title'] or task['tenant'] not in ({'agent-os:company:chris','agent-os:cockpit'} if a['owner']=='chris' else {'agent-os:company:'+a['owner']}) or any(meta.get(k)!=v for k,v in {'requesting_user':a['owner'],'audience':a['owner'],'cockpit_board':a['board'],'source_session_key':'agent-os:cockpit:'+a['owner']}.items()):
            raise Held('Original app filing authority mismatch')
    target = a['target']
    if set(target) != {'platform','chat_id','thread_id','user_id','user_id_alt','chat_type','notifier_profile','delivery_mode','delivery_metadata'} or target['platform'] != 'photon' or target['chat_type'] != 'dm' or target['delivery_mode'] != 'notify' or target['thread_id'] is not None:
        raise Held('Canonical personal Photon destination required')
    if target['delivery_metadata'] != {MARKER: request_id}:
        raise Held('Unexpected secondary routing metadata')
    # Re-read the external authority after all database work: revocation during
    # lookup cannot be masked by the first snapshot.
    if resolve_request(request_id) != a:
        raise Held('Secondary authority changed during validation')
    return a

def register(c, request_id, *, now=None):
    from gateway import kanban_delivery_receipts as receipts
    schema(c)
    with kb.write_txn(c):
        a = validate(c, request_id, now=now)
        sub = dict(a['target'], task_id=a['task_id'])
        canonical_sub = dict(sub, delivery_metadata={**sub['delivery_metadata'], '_kanban_subscription_origin_task_id':a['task_id']})
        key = receipts.identity(canonical_sub)
        old = c.execute('SELECT * FROM secondary_document_intents WHERE request_id=? OR task_id=?', (request_id,a['task_id'])).fetchone()
        raw = encode(a)
        if old:
            if old['request_id'] != request_id or old['authority_json'] != raw or old['subscription'] != key:
                raise Held('Existing secondary intent differs; no replacement')
            return 'existing'
        if any(s['platform'] == 'photon' for s in kb.list_notify_subs(c, a['task_id'], include_internal_metadata=True)):
            raise Held('Original Photon subscription already owns delivery')
        c.execute('INSERT INTO secondary_document_intents VALUES (?,?,?,?)', (request_id,a['task_id'],raw,key))
        kb.add_notify_sub(c, **sub, inherit_children=False)
        task = kb.get_task(c,a['task_id'])
        if task.status == 'done':
            events = c.execute("SELECT * FROM task_events WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1", (a['task_id'],)).fetchall()
            if not events or events[0]['created_at'] < a['source_created_at']:
                raise Held('No authorized future completion to stage')
            ev = events[0]
            snapshot = {k:ev[k] for k in ('id','task_id','kind','created_at','run_id')}
            snapshot['payload'] = json.loads(ev['payload']) if ev['payload'] else None
            receipts.schema(c)
            c.execute('INSERT INTO kanban_return_events(subscription,event_id,event_json) VALUES (?,?,?)', (key,ev['id'],encode(snapshot)))
        if resolve_request(request_id) != a:
            raise Held('Authority changed before registration commit')
    return 'registered'

def current(c, sub, *, now=None):
    """Called before EACH external part; unmarked legacy subscriptions untouched."""
    from gateway.kanban_delivery_receipts import identity
    metadata = sub.get('delivery_metadata') or {}
    if isinstance(metadata,str):
        metadata = json.loads(metadata)
    request_id = metadata.get(MARKER)
    if request_id is None:
        if sub.get('platform') == 'photon' and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='secondary_document_intents'").fetchone() and c.execute('SELECT 1 FROM secondary_document_intents WHERE task_id=?',(sub['task_id'],)).fetchone():
            raise Held('Secondary routing marker removed or conflicting subscription')
        return False
    a = validate(c,request_id,now=now)
    row = c.execute('SELECT * FROM secondary_document_intents WHERE request_id=?',(request_id,)).fetchone()
    if not row or row['authority_json'] != encode(a) or row['task_id'] != sub['task_id'] or row['subscription'] != identity(sub):
        raise Held('Secondary receipt missing or replaced')
    return True

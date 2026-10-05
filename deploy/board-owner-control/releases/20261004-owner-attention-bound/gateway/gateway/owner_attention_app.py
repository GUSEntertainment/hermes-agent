"""Passive attention receipts. Never completion, delivery approval, or a model turn."""
import json
from pathlib import Path
from contextlib import closing
from gateway import owner_delegation as work
from gateway.kanban_delivery_receipts import identity
from hermes_cli import kanban_db as kb


def _source(conn, item):
    from gateway import owner_attention as attention
    sub = item['sub']
    owner = item['requester']
    enrolled = work.enrollment(owner)
    actual = Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'))
    if actual != work.database(enrolled['board']) or sub.get('platform') != 'api_server':
        raise PermissionError('Canonical owner app board required')
    from gateway.owner_attention_binding import validate as validate_follow_up
    follow_up = validate_follow_up(conn, sub, owner)
    if follow_up is None and not any(identity(s) == identity(sub) for s in kb.list_notify_subs(conn, item['task_id'], include_internal_metadata=True)):
        raise PermissionError('Original app subscription changed')
    row = conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (item['task_id'],)).fetchone()
    binding = json.loads(row[0]) if row else None
    if follow_up is not None:
        binding = follow_up['binding']
    meta = attention._json(sub.get('delivery_metadata'))
    if meta.get('_kanban_inherited_subscription'):
        raise PermissionError('Inherited app destination denied')
    if follow_up is not None:
        if binding.get('kind') not in {work.KIND, 'native-api-return-v1'}:
            raise PermissionError('Current app follow-up source required')
    elif binding and binding.get('kind') == work.KIND:
        work.verify_return(conn, sub)
        _, task, _, binding = work.verify_task(conn, item['task_id'])
        if binding.get('principal') != owner:
            raise PermissionError('Private owner changed')
    elif binding and binding.get('kind') == 'native-api-return-v1':
        from gateway.app_business_authority import current
        profile = binding.get('profile')
        authority = binding.get('authority', {})
        fresh = current(profile, owner) if authority.get('scope_contract') == 'gus-company-work-v1' else enrolled
        if (authority != fresh or binding.get('board') != enrolled['board']
                or ('principal' not in authority and profile != ('default' if owner == 'chris' else owner))):
            raise PermissionError('Native app enrollment changed')
        task = conn.execute('SELECT * FROM tasks WHERE id=?', (item['task_id'],)).fetchone()
        if not task or task['session_id'] != binding.get('session_id'):
            raise PermissionError('Native app source changed')
    else:
        # Legacy app handoffs have no native API run. The app must additionally
        # match its existing same-owner handoff ledger; never invent a thread.
        if owner != 'chris' or sub.get('notifier_profile') not in {None, '', 'default'}:
            raise PermissionError('Missing enrolled native app source')
        from gateway.kanban_watchers import _has_exact_notification_origin
        if not _has_exact_notification_origin(kb.get_task(conn, item['task_id']), {**sub, 'delivery_metadata':meta}):
            raise PermissionError('Original app handoff missing')
        return dict(kind='handoff', taskId=item['task_id'], board=enrolled['board'])
    target = binding.get('target', {})
    for key in ('platform','chat_id','notifier_profile','user_id','user_id_alt','chat_type','delivery_mode'):
        if not (key == 'delivery_mode' and follow_up is not None) and sub.get(key) != target.get(key):
            raise PermissionError('App subscription changed')
    if (sub.get('thread_id') or None) != (target.get('thread_id') or None):
        raise PermissionError('App thread changed')
    if any(meta.get(k) != v for k,v in target.get('delivery_metadata', {}).items()):
        raise PermissionError('App session key changed')
    sid, run, key = binding.get('session_id'), binding.get('run_id'), meta.get('session_key')
    if not all(isinstance(v,str) and 0 < len(v) <= 200 for v in (sid,run,key)):
        raise PermissionError('Complete original app run required')
    return dict(kind='native', taskId=item['task_id'], board=enrolled['board'],
                sessionId=sid, runId=run, sessionKey=key, bindingDigest=work.digest(binding))


def persist(conn, delivery, text):
    from gateway import owner_attention as attention
    if not isinstance(text,str) or not 0 < len(text) <= 16000:
        raise ValueError('Bounded attention text required')
    with kb.write_txn(conn):
        items = []
        for supplied in delivery['items']:
            item = attention._validate(conn, supplied['sub'], supplied['event_id'])
            if item['requester'] != delivery['requester'] or attention.destination(item) != delivery['destination']:
                raise PermissionError('Attention destination changed')
            items.append({**item, 'source':_source(conn,item)})
        if not items or len(items) > 100:
            raise ValueError('Bounded current attention required')
        conn.execute('CREATE TABLE IF NOT EXISTS owner_attention_app (attention_id TEXT PRIMARY KEY,owner TEXT NOT NULL,payload TEXT NOT NULL)')
        payload=dict(attentionId=delivery['destination'], deliveryId=delivery['delivery_id'],
                     owner=delivery['requester'], updatedAt=delivery['created_at']*1000, items=items, summary=text)
        raw=work.encode(payload)
        if len(raw.encode()) > 262144:
            raise ValueError('Attention record too large')
        old=conn.execute('SELECT owner FROM owner_attention_app WHERE attention_id=?',(delivery['destination'],)).fetchone()
        if old and old[0] != payload['owner']:
            raise PermissionError('Attention owner changed')
        conn.execute('INSERT INTO owner_attention_app VALUES(?,?,?) ON CONFLICT(attention_id) DO UPDATE SET payload=excluded.payload',
                     (delivery['destination'],payload['owner'],raw))
    return {'message_id':'app-attention:'+delivery['destination'], 'accepted_locally':True}


def read(owner):
    """Owner derived by protected app backend. Enrollment/source rechecked here."""
    from gateway import owner_attention as attention
    authority=work.enrollment(owner)
    with closing(work._connect(work.database(authority['board']))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_attention_app'").fetchone():
            return []
        rows=conn.execute('SELECT payload FROM owner_attention_app WHERE owner=? ORDER BY attention_id LIMIT 100',(owner,)).fetchall()
        result=[]
        for row in rows:
            p=json.loads(row[0]);active=[];sources=[]
            try:
                for item in p['items']:
                    source=_source(conn,item)
                    if source != item['source'] or item['requester'] != owner:
                        raise PermissionError('Attention source changed')
                    sources.append(source)
                    try: active.append(attention._validate(conn,item['sub'],item['event_id']))
                    except ValueError: pass  # resolved/superseded, not revoked authority
            except PermissionError:
                continue
            summary=p['summary'] if len(active)==len(p['items']) else attention._text(active) if active else 'This blocker has cleared; the work is no longer waiting on this request.'
            result.append(dict(attentionId=p['attentionId'],deliveryId=p['deliveryId'],owner=owner,
                               updatedAt=p['updatedAt'],sources=sources,summary=summary,resolved=not active))
        if work.enrollment(owner) != authority:
            raise PermissionError('Owner enrollment changed during read')
        return result


def source_binding(conn, sub, requester):
    return _source(conn, dict(sub=sub, requester=requester, task_id=sub["task_id"]))

"""Passive, source-bound reminders for explicitly requested owner holds.

Never wakes an agent, starts a worker, changes a card, or grants an approval.
The service is opt-in; each hold must independently opt in through owner control.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import time
from zoneinfo import ZoneInfo

from hermes_cli import kanban_db as kb
from gateway import owner_delegation as work
from gateway.kanban_delivery_receipts import identity, send_once

DAY = 24 * 60 * 60


def settings(config):
    if not isinstance(config, dict) or not isinstance(config.get('kanban'), dict):
        return None
    value = config['kanban'].get('owner_attention')
    return value if isinstance(value, dict) and value.get('enabled') is True else None


def opted(event):
    p = event.payload if hasattr(event, 'payload') else event
    return (isinstance(p, dict) and p.get('owner_board_hold') is True
            and p.get('owner_attention_requested') is True
            and p.get('kind') in {'needs_input', 'capability'}
            and isinstance(p.get('requester'), str) and bool(p['requester']))


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS owner_attention_deliveries (
        destination TEXT NOT NULL, delivery_id TEXT NOT NULL, state TEXT NOT NULL,
        created_at INTEGER NOT NULL, receipt TEXT, PRIMARY KEY(destination,delivery_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS owner_attention_destinations (
        destination TEXT PRIMARY KEY, last_attempt INTEGER NOT NULL, snoozed_until INTEGER NOT NULL DEFAULT 0)''')


def _json(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def _validate(conn, sub, event_id=None):
    """Return current exact owner hold, never inherited or secondary delivery."""
    task = kb.get_task(conn, sub['task_id'])
    if task is None or task.status not in {'blocked', 'triage'} or (task.current_run_id is not None or task.claim_lock or task.worker_pid):
        raise ValueError('Owner hold is no longer idle')
    from hermes_cli.owner_work_preparation import _idle
    _idle(conn, dict(conn.execute('SELECT * FROM tasks WHERE id=?',(task.id,)).fetchone()))
    current = kb.list_notify_subs(conn, sub['task_id'], include_internal_metadata=True)
    if not any(identity(row) == identity(sub) for row in current):
        raise PermissionError('Owner notification source was revoked')
    meta = _json(sub.get('delivery_metadata'))
    if meta.get('_kanban_inherited_subscription'):
        raise PermissionError('Inherited notification is not the requester')
    row = conn.execute("SELECT id,kind,payload,created_at FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected','capability_preflight_blocked','status','triaged','unblocked','review_work_resumed','review_requested','changes_requested','completed','archived','owner_work_requested') ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
    if not row or (event_id is not None and row['id'] != event_id):
        raise ValueError('Owner blocker changed')
    if row['kind'] not in {'blocked','block_loop_detected','capability_preflight_blocked'}:
        raise ValueError('Owner hold was superseded by another state')
    payload = _json(row['payload'])
    if payload.get('kind') not in {'needs_input','capability'} or task.block_kind != payload['kind']:
        raise ValueError('Not a current owner-action blocker')
    binding = conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (task.id,)).fetchone()
    private_binding = _json(binding[0]) if binding else {}
    if not opted(payload):
        # Existing authenticated work requests and private delegation contracts
        # also request accountability for their later native worker blockers.
        order = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_work_requested' AND id<? ORDER BY id DESC LIMIT 1", (task.id,row['id'])).fetchone()
        actor = _json(order[0]).get('actor') if order else None
        requester = ('chris' if actor == 'default' else actor) or (
            private_binding.get('principal') if private_binding.get('kind') == work.KIND else None)
        if not requester and private_binding.get('kind') == 'native-api-return-v1':
            authority = private_binding.get('authority') or {}
            requester = authority.get('principal') or authority.get('owner')
        if not requester:
            raise ValueError('Owner did not request follow-up on this blocker')
        payload = {**payload, 'requester':requester}
    if private_binding.get('kind') == work.KIND:
        if private_binding.get('principal') != payload['requester']:
            raise PermissionError('Blocker requester differs from private source')
        work.verify_return(conn, sub)  # includes fresh enrollment, board and target
    elif sub.get('platform') == 'api_server':
        from gateway.owner_attention_app import source_binding
        source_binding(conn, sub, payload['requester'])
    else:
        # UUID task sessions retain their canonical source key in native metadata.
        # The origin marker alone is insufficient: every explicit subscription
        # receives it, including separately selected destinations.
        from types import SimpleNamespace
        from gateway.kanban_watchers import _session_id_matches_subscription, _has_exact_notification_origin
        exact = (_session_id_matches_subscription(task, sub) or (
            _has_exact_notification_origin(task, {**sub,'delivery_metadata':meta})
            and _session_id_matches_subscription(SimpleNamespace(session_id=meta.get('session_key')), sub)))
        if (payload['requester'] != 'chris' or sub.get('notifier_profile') not in {None, '', 'default'} or not exact):
            raise PermissionError('No authenticated original owner source')
    from gateway.secondary_documents import current as secondary_current
    if secondary_current(conn, sub):
        raise PermissionError('Document-only secondary destination is not an owner inbox')
    return {'task_id':task.id, 'event_id':row['id'], 'created_at':row['created_at'],
            'requester':payload['requester'], 'reason':str(payload.get('reason') or ''),
            'title':task.title, 'sub':sub}


def destination(item):
    sub = item['sub']
    # Exclude task-specific metadata, retain exact owner, thread and session.
    meta = _json(sub.get('delivery_metadata'))
    material = [item['requester'], *(sub.get(k) or '' for k in
        ('platform','chat_id','thread_id','notifier_profile','user_id','user_id_alt','chat_type')),
        meta.get('session_key') or '']
    return sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def collect(conn, subs, config, *, now=None):
    cfg = settings(config)
    if cfg is None:
        return []
    now = int(time.time() if now is None else now)
    schema(conn)
    groups = {}
    for sub in subs:
        if sub.get('platform') not in {'telegram','photon','api_server'}:
            continue
        try:
            item = _validate(conn, sub)
            # A passive app inbox does not interrupt anyone. Publish the ask
            # immediately; quiet hours apply only to outward push channels.
            if sub.get('platform') != 'api_server':
                tz = (cfg.get('timezones') or {}).get(item['requester']) or cfg.get('default_timezone')
                local = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else None
                if local is None or not 9 <= local.hour < 18:
                    continue  # absent timezone never guesses the owner's daytime
            groups.setdefault(destination(item), []).append(item)
        except (PermissionError, ValueError, TypeError, KeyError):
            continue
    due = []
    for key, items in groups.items():
        row = conn.execute('SELECT * FROM owner_attention_destinations WHERE destination=?', (key,)).fetchone()
        if row and (row['snoozed_until'] > now or row['last_attempt'] + DAY > now):
            continue
        # Each day's follow-up is a distinct authorized logical message. An
        # uncertain send is not retried; its attempt still consumes today's slot.
        items.sort(key=lambda x:(x['created_at'], x['task_id']))
        stamp = (row['last_attempt'] if row else 0)
        delivery_id = sha256(json.dumps([stamp, [(i['task_id'],i['event_id'],identity(i['sub'])) for i in items]], sort_keys=True).encode()).hexdigest()
        due.append({'destination':key, 'delivery_id':delivery_id, 'items':items,
                    'sub':items[0]['sub'], 'requester':items[0]['requester'], 'created_at':now})
    return due


def _text(items):
    from gateway.kanban_watchers import _safe_review_reason
    lines = ["I need your help to move this work forward:"]
    for item in items[:8]:
        title = _safe_review_reason(item['title'], 90)
        reason = _safe_review_reason(item['reason'], 170)
        lines.append(f"• {title} ({item['task_id']}): {reason or 'Please check the blocker on this card.'}")
    if len(items)>8:
        lines.append(f"Plus {len(items)-8} other items in this same conversation's board.")
    lines.append("Reply here when the missing access or decision is ready. I’ll keep this to one daytime reminder a day while it’s unresolved.")
    return '\n'.join(lines)


def _claim(conn, delivery, config, now):
    if settings(config) is None:
        return None
    # Re-read source, current blocker and quiet period immediately before effect.
    current = collect(conn, [i['sub'] for i in delivery['items']], config, now=now)
    fresh = next((d for d in current if d['destination']==delivery['destination'] and d['delivery_id']==delivery['delivery_id']),None)
    if fresh is None:
        return None
    with kb.write_txn(conn):
        for i in fresh['items']:
            _validate(conn, i['sub'], i['event_id'])
        row = conn.execute('SELECT * FROM owner_attention_destinations WHERE destination=?',(fresh['destination'],)).fetchone()
        if row and (row['snoozed_until']>now or row['last_attempt']+DAY>now):
            return None
        inserted = conn.execute('INSERT OR IGNORE INTO owner_attention_deliveries VALUES(?,?,?,?,NULL)',(fresh['destination'],fresh['delivery_id'],'inflight',now)).rowcount
        if inserted:
            conn.execute('INSERT INTO owner_attention_destinations VALUES(?,?,0) ON CONFLICT(destination) DO UPDATE SET last_attempt=excluded.last_attempt',(fresh['destination'],now))
        return fresh if inserted else None


async def deliver(conn, delivery, adapter, config, *, now=None):
    """One provider attempt; unavailable adapters remain queued without a claim."""
    now = int(time.time() if now is None else now)
    sub = delivery['sub']
    platform = sub.get('platform')
    if platform != 'api_server' and adapter is None:
        return False
    if platform == 'telegram' and getattr(adapter,'_bot',None) is None:
        return False
    if platform == 'photon' and getattr(adapter,'_http_client',None) is None:
        return False
    fresh = _claim(conn, delivery, config, now)
    if fresh is None:
        return False
    text = _text(fresh['items'])
    from gateway.kanban_watchers import _external_delivery_metadata
    meta = _external_delivery_metadata(dict(_json(sub.get('delivery_metadata'))))
    if sub.get('thread_id'):
        meta['thread_id'] = sub['thread_id']
    if platform == 'photon':
        meta['logical_message_id'] = 'owner-attention:'+fresh['delivery_id']
    try:
        if platform == 'api_server':
            from gateway.owner_attention_app import persist
            result = persist(conn, fresh, text)
            message_id = result.get('message_id')
            if result.get('accepted_locally') is not True or not message_id:
                raise RuntimeError('Local app receipt not confirmed')
        else:
            result = await send_once(adapter, sub['chat_id'], meta, text=text)
            message_id = getattr(result,'message_id',None)
            if getattr(result,'success',False) is not True or not message_id:
                raise RuntimeError('Provider receipt not confirmed')
        with kb.write_txn(conn):
            conn.execute("UPDATE owner_attention_deliveries SET state='accepted',receipt=? WHERE destination=? AND delivery_id=?",(json.dumps({'message_id':str(message_id)}),fresh['destination'],fresh['delivery_id']))
    except BaseException as exc:
        with kb.write_txn(conn):
            conn.execute("UPDATE owner_attention_deliveries SET state='uncertain',receipt=? WHERE destination=? AND delivery_id=?",(json.dumps({'reason':type(exc).__name__}),fresh['destination'],fresh['delivery_id']))
        with kb.write_txn(conn):
            for item in fresh['items']:
                kb._append_event(conn,item['task_id'],'owner_attention_delivery_held',{'reason':'Provider receipt unconfirmed; do not repeat this send. A distinct daily follow-up may be due later.','delivery_id':fresh['delivery_id']})
        if not isinstance(exc,Exception):
            raise
        return False
    # App consumer records the passive note in its original conversation.
    if platform == 'api_server':
        return True
    # Preserve reply continuity, best effort and strictly in the same source.
    try:
        from gateway.mirror import find_owner_dm_session, mirror_proactive_message
        sid = find_owner_dm_session(platform,str(sub['chat_id']),sub.get('thread_id'))
        if sid:
            mirror_proactive_message(platform,str(sub['chat_id']),text,source='owner blocker follow-up',ref=fresh['delivery_id'],thread_id=sub.get('thread_id'),session_id=sid)
    except Exception:
        pass
    return True


def owns_event(conn, sub, event, config):
    """Prevent duplicate legacy pings/model wakes for this passive-only hold."""
    if settings(config) is None or event.kind not in {'blocked','block_loop_detected','capability_preflight_blocked'}:
        return False
    if sub.get('platform') not in {'telegram','photon','api_server'}:
        return False
    try:
        current = _validate(conn, sub)
        return event.id <= current['event_id']
    except (PermissionError, ValueError, TypeError, KeyError):
        return False

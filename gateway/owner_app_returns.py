"""Durable app replies: persist locally, never call the non-push API adapter.

The app consumes these receipts into its existing owner-scoped conversation
notice ledger. Original run/session matching, not model output, selects a thread.
"""
import json
from pathlib import Path
from gateway import owner_delegation as work
from hermes_cli import kanban_db as kb


def pump(conn):
    """Local notifier lane, independent of multiplexed API adapter registration."""
    actual=Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'))
    try:
        canonical=work.database(actual.parent.name)
    except PermissionError:
        # Non-canonical board path (default board, HERMES_KANBAN_DB override):
        # not an owner board, so there are no app returns. Never abort the tick.
        return 0
    if actual!=canonical:return 0
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_delegated_work'").fetchone():return 0
    from gateway.kanban_delivery_receipts import stage_and_recover,reviewed_artifacts,record_problem
    count=0
    for sub in kb.list_notify_subs(conn,include_internal_metadata=True):
        if sub.get('platform')!='api_server':continue
        row=conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(sub['task_id'],)).fetchone()
        if not row or json.loads(row[0]).get('kind')!=work.KIND:continue
        binding=json.loads(row[0])
        if sub.get('notifier_profile')!=binding.get('principal'):continue
        for event in stage_and_recover(conn,sub):
            try:
                work.verify_return(conn,sub)
                _,task,_,_=work.verify_task(conn,sub['task_id'])
                text=str((event.payload or {}).get('summary') or task.get('result') or 'Your delegated work is complete.')[:30000]
                files=reviewed_artifacts(conn,sub['task_id'])
                # 2026-10-05 (Chris): members get one human line; the result
                # itself is the Desk document, never raw worker text.
                from gateway.kanban_watchers import member_completion_text
                text=member_completion_text(str(task.get('title') or ''),False)
                if files:text=text.rstrip('.')+" — it's on your Desk."
                text=text.replace(' is finished —',' is ready —')
                persist(conn,sub,event.id,text,files);count+=1
            except (PermissionError,RuntimeError,ValueError,OSError):
                record_problem(conn,sub,event.id,'Private app return awaits current source and review verification')
    return count


def persist(conn, sub, event_id, text, artifacts):
    from gateway.kanban_delivery_receipts import DeliveryHeld, identity
    work.verify_return(conn, sub)
    _, task, _, binding = work.verify_task(conn, sub['task_id'])
    if binding['target']['platform'] != 'api_server':
        raise DeliveryHeld('App return requires original app source')
    if task['status'] != 'done' or task['current_run_id'] is not None:
        raise DeliveryHeld('App result is not completed')
    row = conn.execute('SELECT kind,task_id FROM task_events WHERE id=?', (event_id,)).fetchone()
    if not row or row['kind'] != 'completed' or row['task_id'] != sub['task_id']:
        raise DeliveryHeld('Original completion event required')
    payload = dict(owner=binding['principal'], task_id=sub['task_id'], event_id=event_id,
                   session_id=binding['session_id'], run_id=binding['run_id'],
                   session_key=binding['target']['delivery_metadata']['session_key'],
                   binding_digest=work.digest(binding), text=text,
                   artifacts=[{k: a[k] for k in ('id','name','sha256')} for a in artifacts])
    raw = work.encode(payload)
    with kb.write_txn(conn):
        if not any(identity(row)==identity(sub) for row in kb.list_notify_subs(conn,sub['task_id'],include_internal_metadata=True)):
            raise DeliveryHeld('App return subscription revoked')
        from gateway.kanban_delivery_receipts import reviewed_artifacts
        fresh=reviewed_artifacts(conn,sub['task_id'])
        if payload['artifacts'] != [{k:a[k] for k in ('id','name','sha256')} for a in fresh]:
            raise DeliveryHeld('App return approval changed')
        conn.execute('CREATE TABLE IF NOT EXISTS owner_app_replies(task_id TEXT NOT NULL,event_id INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(task_id,event_id))')
        old = conn.execute('SELECT payload FROM owner_app_replies WHERE task_id=? AND event_id=?', (sub['task_id'],event_id)).fetchone()
        if old and old[0] != raw:
            raise DeliveryHeld('Persisted app reply changed')
        conn.execute('INSERT OR IGNORE INTO owner_app_replies VALUES(?,?,?)', (sub['task_id'],event_id,raw))
        work.verify_return(conn, sub)
        conn.execute('UPDATE kanban_return_events SET state="accepted" WHERE subscription=? AND event_id=?', (identity(sub),event_id))
    # Put the returned note into the original app session transcript so the
    # owner's next message in that thread can reference it.
    try:
        from gateway.mirror import mirror_proactive_message
        if not old:  # first persist only; retries must not duplicate the turn
            mirror_proactive_message('api_server', str(sub.get('chat_id') or ''), text,
                                     source='delegated work return', ref=f"task {sub['task_id']}",
                                     session_id=binding['session_id'])
    except Exception:
        pass
    return payload


def read(conn, *, owner, session_id, run_id):
    """Read only after the app authenticates and binds the original source turn."""
    authority=work.enrollment(owner)
    actual=Path(next(r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'))
    if actual!=work.database(authority['board']):
        raise PermissionError('Private result owner required')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_app_replies'").fetchone():
        return []
    rows = conn.execute('SELECT * FROM owner_app_replies WHERE json_extract(payload,\'$.session_id\')=? AND json_extract(payload,\'$.run_id\')=? ORDER BY event_id LIMIT 100', (session_id,run_id)).fetchall()
    from gateway.kanban_delivery_receipts import reviewed_artifacts
    results=[]
    for row in rows:
        p=json.loads(row['payload'])
        _,task,_,binding=work.verify_task(conn,row['task_id'])
        if (p['owner']!=owner or p['session_id']!=session_id or p['run_id']!=run_id
                or binding['principal']!=owner or p['binding_digest']!=work.digest(binding) or binding['target']['platform']!='api_server'
                or binding['session_id']!=session_id or binding['run_id']!=run_id
                or p['session_key']!=binding['target']['delivery_metadata']['session_key']
                or task['status']!='done' or task['current_run_id'] is not None):
            raise PermissionError('App return source changed')
        approved=reviewed_artifacts(conn,task['id'])
        if p['artifacts'] != [{k:a[k] for k in ('id','name','sha256')} for a in approved]:
            raise PermissionError('App return review changed')
        results.append(p)
    return results

"""Companion to original task return; no replacement task, send, or backfill."""
import json,hashlib
from gateway.gateway_document_custody import current,encode,digest,KIND,policy_at
DDL='CREATE TABLE task_document_custody(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,owner TEXT NOT NULL,domain TEXT NOT NULL,reference TEXT NOT NULL UNIQUE,record_json TEXT NOT NULL)'
def schema(c,*,create=True):
    row=c.execute("SELECT type,sql FROM sqlite_master WHERE name='task_document_custody'").fetchone()
    norm=lambda x:''.join(x.lower().split()).replace('ifnotexists','')
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='task_document_custody'").fetchone():raise PermissionError('Unexpected custody trigger')
    if row is not None and (row[0]!='table' or norm(row[1])!=norm(DDL)):raise PermissionError('Custody schema incompatible')
    if row is None:
        if not create:return False
        c.execute(DDL)
    return True

def ensure(c,task_id,binding,*,replay=False):
    if not c.in_transaction:raise RuntimeError('Task creation transaction required')
    if binding.get('kind') not in ('gateway-return-v1','owner-delegation-return-v1'):return
    present=schema(c,create=False)
    if not present and (replay or (binding.get('kind')!='owner-delegation-return-v1' and policy_at()[0] is None)):return
    schema(c)
    existing=c.execute('SELECT record_json FROM task_document_custody WHERE task_id=?',(task_id,)).fetchone()
    # Never backfill historical returns on replay or owner-policy activation.
    if replay and existing is None:return
    if binding.get('kind') not in ('gateway-return-v1','owner-delegation-return-v1'):return
    authority=current(binding)
    if authority is None:
        if existing:raise PermissionError('Previously bound custody unavailable')
        return
    task=c.execute('SELECT session_id,created_at FROM tasks WHERE id=?',(task_id,)).fetchone()
    original=c.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(task_id,)).fetchone()
    if not task or task[0]!=binding['session_id'] or not original or original[0]!=encode(binding):raise PermissionError('Original task source mismatch')
    if task[1]<authority['enabled_from']:
        if existing:raise PermissionError('Custody activation changed')
        return
    value=dict(version=KIND,task_id=task_id,authority=authority,source=binding)
    raw=encode(value)
    if existing:
        if existing[0]!=raw:raise PermissionError('Committed custody changed')
    else:c.execute('INSERT INTO task_document_custody VALUES(?,?,?,?,?)',(task_id,authority['owner'],authority['domain'],'gw_'+hashlib.sha256((binding['board']+':'+task_id).encode()).hexdigest(),raw))
    if current(binding)!=authority:raise PermissionError('Custody changed before commit')

def verify(c,row,owner,*,resolver=None):
    """Readonly consumption; owner comes from an independently verified app grant."""
    schema(c,create=False)
    v=json.loads(row['record_json']);binding=v['source'];authority=(resolver or current)(binding)
    if authority is None or v['version']!=KIND or v['task_id']!=row['task_id'] or v['authority']!=authority or authority['owner']!=owner or row['owner']!=owner or row['domain']!='recipient_documents':raise PermissionError('Current custody denied')
    if row['reference']!='gw_'+hashlib.sha256((binding['board']+':'+row['task_id']).encode()).hexdigest():raise PermissionError('Custody reference changed')
    original=c.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(row['task_id'],)).fetchone()
    task=c.execute('SELECT session_id,created_at FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
    if not original or original[0]!=encode(binding) or not task or task[0]!=binding['session_id'] or task[1]<authority['enabled_from']:raise PermissionError('Original task source changed')
    return authority

"""Compact native-run child evidence. No session text, caller profile or session IDs."""
import contextlib, hashlib, json, os, sqlite3, stat, re, math
from pathlib import Path
LIMIT=16
TABLE='native_child_execution_receipts'
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))
def sha(value):return hashlib.sha256(value.encode()).hexdigest()
TABLE_SQL="CREATE TABLE native_child_execution_receipts (task_id TEXT NOT NULL,run_id INTEGER NOT NULL,receipt_json TEXT NOT NULL,receipt_sha256 TEXT NOT NULL,PRIMARY KEY(task_id,run_id))"
TRIGGERS={
 'native_child_receipt_no_update':"CREATE TRIGGER native_child_receipt_no_update BEFORE UPDATE ON native_child_execution_receipts BEGIN SELECT RAISE(ABORT,'immutable native child receipt'); END",
 'native_child_receipt_no_delete':"CREATE TRIGGER native_child_receipt_no_delete BEFORE DELETE ON native_child_execution_receipts BEGIN SELECT RAISE(ABORT,'immutable native child receipt'); END",
 'native_child_receipt_no_replace':"CREATE TRIGGER native_child_receipt_no_replace BEFORE INSERT ON native_child_execution_receipts WHEN EXISTS(SELECT 1 FROM native_child_execution_receipts WHERE task_id=NEW.task_id AND run_id=NEW.run_id) BEGIN SELECT RAISE(ABORT,'immutable native child receipt'); END",
}
def validate_existing_schema(conn):
    """Read-only admission. False means clean first install; partial/drift denies."""
    expected={TABLE:('table',TABLE_SQL),**{k:('trigger',v) for k,v in TRIGGERS.items()}}
    rows=conn.execute("SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?,?) OR (type='trigger' AND tbl_name=?)",(*expected,TABLE)).fetchall()
    if not rows:return False
    actual={r[0]:(r[1],r[2]) for r in rows}
    if actual!=expected:raise PermissionError('Incompatible native child receipt table or trigger definitions')
    columns=[(r[1],r[2],r[3],r[5]) for r in conn.execute('PRAGMA table_info(native_child_execution_receipts)')]
    if columns!=[('task_id','TEXT',1,1),('run_id','INTEGER',1,2),('receipt_json','TEXT',1,0),('receipt_sha256','TEXT',1,0)] or conn.execute('PRAGMA foreign_key_list(native_child_execution_receipts)').fetchone():raise PermissionError('Incompatible native child receipt schema')
    return True

def schema(conn):
    if not validate_existing_schema(conn):
        conn.execute(TABLE_SQL)
        for sql in TRIGGERS.values():conn.execute(sql)
    validate_existing_schema(conn)
def authority(conn,task_id):
    from tools.task_attachment_read import _authority
    return _authority(conn,task_id)[0]
def session_snapshot(contract,parent):
    from agent.private_session_binding import validate_parent
    if validate_parent(contract, parent):
        # The live parent is bound but private transcripts/child history are
        # deliberately not persisted. Unavailable evidence is never zero children.
        raise ValueError('Confined session history intentionally unavailable')
    # The admission's profile home is trusted; never accept another session DB.
    path=Path(contract['profile_home'])/'state.db'
    for p in [path,*path.parents]:
        st=p.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid not in (0,os.getuid()) or st.st_mode&0o022:raise PermissionError('Unsafe session database ancestry')
    before=path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise PermissionError('Unsafe session database')
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row
        owner=db.execute('SELECT id,parent_session_id,source,profile_name FROM sessions WHERE id=?',(parent,)).fetchone()
        if owner is None or owner['parent_session_id'] is not None or owner['source']!='kanban' or owner['profile_name']!=contract['profile']:raise PermissionError('Unbound native parent session')
        rows=db.execute('SELECT id,parent_session_id,model,started_at,ended_at,api_call_count,tool_call_count FROM sessions WHERE parent_session_id=? ORDER BY started_at,id LIMIT ?',(parent,LIMIT+1)).fetchall()
        if len(rows)>LIMIT:raise ValueError('Child evidence exceeds bounded limit')
        children=[dict(r) for r in rows]
        for child in children:
            model=child.get('model')
            child['model']=model if isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',model) else 'unavailable'
            for key in ('api_call_count','tool_call_count'):
                if not isinstance(child[key],int) or not 0<=child[key]<=1000000:raise ValueError('Invalid child counter')
            if not isinstance(child['started_at'],(int,float)) or not math.isfinite(child['started_at']) or child['started_at']<0:raise ValueError('Invalid child start')
            if child['ended_at'] is not None and (not isinstance(child['ended_at'],(int,float)) or not math.isfinite(child['ended_at']) or child['ended_at']<child['started_at']):raise ValueError('Invalid child end')
    after=path.stat()
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise PermissionError('Session database replaced')
    return children

def capture(conn,task_id,*,server_session_id=None):
    """Called inside the review transition transaction; authority rechecked after IO."""
    contract=authority(conn,task_id)
    schema(conn)
    row=conn.execute('SELECT current_run_id FROM tasks WHERE id=?',(task_id,)).fetchone()
    run_id=row[0] if row else None
    run=conn.execute('SELECT profile,worker_admission_digest FROM task_runs WHERE id=? AND task_id=?',(run_id,task_id)).fetchone()
    if not run or run[0]!=contract['profile']:raise PermissionError('Native run profile mismatch')
    from gateway.session_context import _SESSION_ID, _UNSET
    contextual=_SESSION_ID.get()
    if contextual is _UNSET:contextual=None
    # This keyword comes from agent.session_id through the executor/registry,
    # never from the model's args or process-global environment.
    parent=server_session_id if isinstance(server_session_id,str) and server_session_id else contextual
    if contextual and server_session_id and contextual!=server_session_id:
        parent=None  # Ambiguous supplemental identity is unavailable, not authority.

    goal=conn.execute('SELECT current_state_version FROM owner_cell_goals WHERE task_id=?',(task_id,)).fetchone()
    payload={'state_version':goal[0] if goal else None,'version':1,'task_id':task_id,'run_id':run_id,'profile':contract['profile'],'parent_session_id':parent,'admission_digest':run[1]}
    try:
        if not parent:raise ValueError('Live native parent session unavailable')
        children=session_snapshot(contract,parent)
        payload.update(status='absent' if not children else 'incomplete' if any(c['ended_at'] is None for c in children) else 'ended',children=children,child_count=len(children))
    except PermissionError:
        raise
    except (OSError,sqlite3.Error,ValueError) as exc:
        payload.update(status='unavailable',children=[],child_count=None,error_type=type(exc).__name__)
    # A denied identity is never recast as an empty/absent child list.
    if authority(conn,task_id)!=contract:raise PermissionError('Admission changed during child evidence capture')
    validate_existing_schema(conn)
    raw=canonical(payload);digest=sha(raw)
    existing=conn.execute('SELECT receipt_json,receipt_sha256 FROM native_child_execution_receipts WHERE task_id=? AND run_id=?',(task_id,run_id)).fetchone()
    if existing:
        if existing[0]!=raw or existing[1]!=digest:raise PermissionError('Native receipt replacement refused')
    else:conn.execute('INSERT INTO native_child_execution_receipts VALUES (?,?,?,?)',(task_id,run_id,raw,digest))
    return payload

def current_task(conn,task_id):
    """Only the admitted current worker reads evidence for its own task."""
    contract=authority(conn,task_id)
    if not validate_existing_schema(conn):return {'status':'unavailable','reason':'no_native_receipt'}
    goal=conn.execute('SELECT current_state_version,phase,required_reviewer_profile FROM owner_cell_goals WHERE task_id=?',(task_id,)).fetchone()
    if not goal or goal[1]!='review' or goal[2]!=contract['profile']:return {'status':'unavailable','reason':'not_current_independent_review'}
    # Latest maker handoff before the current reviewer run; do not use old attempts.
    row=conn.execute("SELECT id,profile,worker_admission_digest FROM task_runs WHERE task_id=? AND outcome='review_requested' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
    if not row:return {'status':'unavailable','reason':'no_maker_handoff'}
    receipt=conn.execute('SELECT receipt_json,receipt_sha256 FROM native_child_execution_receipts WHERE task_id=? AND run_id=?',(task_id,row[0])).fetchone()
    if not receipt:return {'status':'unavailable','reason':'no_native_receipt'}
    raw,digest=receipt
    if sha(raw)!=digest:raise PermissionError('Native child receipt digest mismatch')
    value=json.loads(raw)
    if value.get('state_version')!=goal[0]:raise PermissionError('Stale native child receipt state')
    if value.get('task_id')!=task_id or value.get('run_id')!=row[0] or value.get('profile')!=row[1] or value.get('admission_digest')!=row[2]:raise PermissionError('Native child receipt binding mismatch')
    if authority(conn,task_id)!=contract:raise PermissionError('Admission changed during receipt read')
    # Internal session lineage never enters model context. Configuration strings
    # are not trusted display text; only bounded provider model identifiers pass.
    children=[]
    for child in value['children']:
        model=child.get('model')
        model=model if isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',model) else 'unavailable'
        children.append({'model':model,'state':'ended' if child.get('ended_at') is not None else 'incomplete','api_call_count':child.get('api_call_count'),'tool_call_count':child.get('tool_call_count'),'duration_seconds':round(child['ended_at']-child['started_at'],3) if child.get('ended_at') is not None else None})
    return {'status':value['status'],'child_count':value['child_count'],'children':children,'proof':'native maker run snapshot; ended is not a success verdict'}

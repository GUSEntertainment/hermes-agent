"""Follow-up ownership from the actual authenticated caller, not model routes.

Separate from immutable original completion/document return intents. This binds
only passive attention; it never changes the task, claims work or grants actions.
"""
import json
from pathlib import Path
import time
from gateway.kanban_delivery_receipts import identity
from gateway import owner_delegation as work
from hermes_cli import kanban_db as kb, board_owner_manage as control

MARKER = '_owner_attention_binding'
TABLE = 'owner_attention_sources'


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS owner_attention_sources(task_id TEXT PRIMARY KEY,requester TEXT NOT NULL,source_json TEXT NOT NULL,subscription TEXT NOT NULL,subscription_json TEXT NOT NULL,updated_at INTEGER NOT NULL)')


def _row(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():
        return None
    return conn.execute('SELECT * FROM owner_attention_sources WHERE task_id=?',(task_id,)).fetchone()


def bind_current(conn, task_id, *, action):
    if action not in {'work','block','follow_up'}:
        raise ValueError('Unsupported follow-up binding action')
    grant = control.require_owner()
    from hermes_cli.owner_work_preparation import _connection_board
    board = _connection_board(conn, grant)
    if kb.get_task(conn,task_id) is None:
        raise ValueError('Unknown task on the authenticated board')
    from gateway.session_context import get_bound_session_env as get
    session = get('HERMES_SESSION_ID')
    if not session:
        raise PermissionError('No authenticated current conversation for follow-up')
    requester = grant['principal'] if grant else 'chris'
    from hermes_cli.task_return_intents import capture
    binding = capture(conn,session_id=session,request={'task_id':task_id,'action':action,'tenant':work.tenant(requester)})
    if not binding or binding.get('board') != board or binding.get('target',{}).get('platform') not in {'telegram','photon','api_server'}:
        raise PermissionError('No supported authenticated follow-up conversation')
    return _store(conn, task_id, dict(requester=requester, grant=grant, board=board, binding=binding))


def _store(conn, task_id, source):
    """Internal operator/gateway storage; not a model-callable destination API."""
    from hermes_cli.owner_work_preparation import _connection_board
    grant=control.require_owner()
    if source.get('grant') != grant or source.get('requester') != (grant['principal'] if grant else 'chris'):
        raise PermissionError('Follow-up source does not belong to the current owner')
    board=_connection_board(conn,grant)
    if source.get('board')!=board or kb.get_task(conn,task_id) is None:
        raise PermissionError('Follow-up source outside current owner board')
    target = dict(source['binding']['target'])
    digest = work.digest(source)
    target['delivery_metadata'] = {**target.get('delivery_metadata',{}), MARKER:digest}
    target['delivery_mode'] = 'notify'
    _schema(conn)
    with kb.write_txn(conn):
        if control.require_owner() != grant or _connection_board(conn,grant) != board:
            raise PermissionError('Owner context changed before follow-up binding')
        sub=dict(task_id=task_id, **target)
        conn.execute('INSERT INTO owner_attention_sources VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET requester=excluded.requester,source_json=excluded.source_json,subscription=excluded.subscription,subscription_json=excluded.subscription_json,updated_at=excluded.updated_at',
            (task_id,source['requester'],work.encode(source),identity(sub),work.encode(sub),int(time.time())))
        validate(conn,sub,source['requester'])
    return {'follow_up_bound':True,'follow_up_surface':target['platform'],'task_id':task_id,'board':board,'worker_started':False}


def validate(conn, sub, requester=None):
    """Fresh enrollment and exact subscription checks before passive delivery."""
    row=_row(conn,sub['task_id'])
    if row is None:
        return None
    source=json.loads(row['source_json'])
    if requester is not None and source['requester'] != requester:
        raise PermissionError('Follow-up belongs to another owner')
    if row['requester'] != source['requester'] or row['subscription'] != identity(sub):
        raise PermissionError('Superseded or changed follow-up conversation')
    grant=source['grant']
    if grant is not None:
        if control.refresh_member_grant(grant) != grant or grant['principal'] != source['requester']:
            raise PermissionError('Follow-up owner enrollment changed')
        control._validate_member_connection(conn,grant)
    elif source['requester'] != 'chris':
        raise PermissionError('Invalid root follow-up owner')
    from hermes_cli.owner_work_preparation import _connection_board
    if _connection_board(conn,grant) != source['board']:
        raise PermissionError('Follow-up moved outside enrolled board')
    binding=source['binding'];meta=sub.get('delivery_metadata') or {}
    if isinstance(meta,str):meta=json.loads(meta)
    if meta.get(MARKER) != work.digest(source):
        raise PermissionError('Follow-up binding changed')
    target=binding['target']
    for key in ('platform','chat_id','thread_id','user_id','user_id_alt','chat_type','notifier_profile'):
        if (sub.get(key) or None) != (target.get(key) or None):
            raise PermissionError('Follow-up target changed')
    if binding['kind']==work.KIND:
        work.custody_authority(binding)  # fresh canonical API/Photon owner identity
    elif binding['kind']=='native-api-return-v1':
        from gateway.app_business_authority import current
        authority=binding['authority']
        if authority.get('scope_contract')=='margot-owner-private-v1':
            from gateway.app_private_board_authority import current as private_current
            fresh=private_current(binding['profile'],source['requester'],root=control.ROOT)
        else:
            fresh=current(binding['profile'],source['requester']) if authority.get('scope_contract')=='gus-company-work-v1' else work.enrollment(source['requester'])
        if authority!=fresh:raise PermissionError('Native app follow-up enrollment changed')
    elif binding['kind']!='gateway-return-v1' or source['requester']!='chris' or binding['profile'] not in ('default',''):
        raise PermissionError('Unsupported follow-up source authority')
    return source


def subscriptions(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():
        return []
    return [json.loads(r[0]) for r in conn.execute('SELECT subscription_json FROM owner_attention_sources')]


def count_sources(board):
    import sqlite3
    path=kb.kanban_db_path(board=board)
    if not path.is_file():return 0
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():return 0
        return conn.execute('SELECT COUNT(*) FROM owner_attention_sources').fetchone()[0]

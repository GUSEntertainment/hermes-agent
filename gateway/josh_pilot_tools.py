"""Staged own-board read pilot, using native kanban storage and registry."""
import os
import json
import sqlite3
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_SCHEMA_NAMES = ContextVar('josh_schema_names', default=None)

@contextmanager
def schema_scope():
    """Freeze schema selection during construction, never dispatch grants."""
    token = _SCHEMA_NAMES.set(frozenset(allowed_names()))
    try:
        yield
    finally:
        _SCHEMA_NAMES.reset(token)

def schema_names():
    snapshot = _SCHEMA_NAMES.get()
    return snapshot if snapshot is not None else frozenset(allowed_names())
from gateway.josh_board_authority import _execution, admitted_board
TOOL='josh_task_status'
LIST_TOOL='own_work_list'
TOOLSET='josh_status_pilot'

def trusted_member():
    """Owner profile opted into full standard tools (t_70638db4, Chris).
    Board/terminal walls still apply via admitted_board and host_tool_authority."""
    from gateway.josh_board_authority import _execution
    from hermes_constants import get_hermes_home
    scope=_execution.get()
    if not scope or not scope[0] or scope[0]!=scope[1]:return False
    home=Path(get_hermes_home())
    if home!=PRODUCTION_ANCHOR/'.hermes'/'profiles'/scope[0]:return False
    try:
        import yaml
        cfg=yaml.safe_load((home/'config.yaml').read_text(encoding='utf-8')) or {}
    except Exception:return False
    return cfg.get('trusted_member') is True

def pilot_required():
    if os.environ.get('HERMES_SESSION_SOURCE')!='kanban' and trusted_member():return False
    from gateway.member_worker_policy import authenticated_private_policy
    if authenticated_private_policy() is not None:return False
    from gateway.josh_board_authority import app_binding
    if app_binding() is not None:return False
    from gateway.josh_board_authority import restricted
    required=restricted()
    if required:
        if os.environ.get('HERMES_KANBAN_HOME','').strip():raise PermissionError('Owner-work database override denied')
        database=os.environ.get('HERMES_KANBAN_DB','').strip()
        if database:
            # Native worker admission stamps its exact database. This is not
            # permission to select a different owner's store through env.
            board=admitted_board(None)
            expected=PRODUCTION_BOARDS/board/'kanban.db'
            if os.environ.get('HERMES_SESSION_SOURCE')!='kanban' or database!=str(expected) or expected.resolve(strict=True)!=expected:
                raise PermissionError('Owner-work database override denied')
        admitted_board(None)
    return required

def _own_path():
    board=admitted_board(None)
    if not board:raise PermissionError('Own board required')
    return PRODUCTION_BOARDS/board/'kanban.db'

def status(args, **kwargs):
    if not pilot_required():raise PermissionError('Josh pilot authority required')
    if set(args)!={'task_id'} or not isinstance(args['task_id'],str) or not 1<=len(args['task_id'])<=200:
        raise ValueError('One bounded task_id required')
    admitted_board(None)
    result=_read_native(args['task_id'], _own_path(), PRODUCTION_ANCHOR)
    admitted_board(None)
    return result

PRODUCTION_ANCHOR=Path('/Users/Chris')
PRODUCTION_BOARDS=Path('/Users/Chris/.hermes/kanban/boards')

def _snapshot_path(path,anchor):
    path=Path(path);anchor=Path(anchor)
    if not path.is_absolute() or path.resolve(strict=True)!=path or anchor.resolve(strict=True)!=anchor:
        raise PermissionError('Noncanonical native board path')
    path.relative_to(anchor)
    snapshot=[]
    chain=[anchor]
    for component in path.parent.relative_to(anchor).parts:chain.append(chain[-1]/component)
    for parent in chain:
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o022:
            raise PermissionError('Unsafe native board directory')
        snapshot.append((str(parent),st.st_dev,st.st_ino,st.st_uid,st.st_mode))
    for file in [path,Path(str(path)+'-wal'),Path(str(path)+'-shm')]:
        try:st=file.lstat()
        except FileNotFoundError:
            if file==path:raise
            snapshot.append((str(file),None));continue
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o022 or st.st_nlink!=1:
            raise PermissionError('Unsafe native board file')
        identity=(str(file),st.st_dev,st.st_ino,st.st_uid,st.st_mode)
        snapshot.append(identity+(st.st_size,st.st_mtime_ns,st.st_ctime_ns) if file==path else identity)
    return tuple(snapshot)

def _unchanged(before,after):
    # SQLite may create owner-owned WAL coordination files on a read-only
    # connection. Existing sidecar inode changes still fail closed.
    return len(before)==len(after) and all(a==b or (len(a)==2 and a[1] is None and a[0]==b[0]) for a,b in zip(before,after))

def _read_native(task_id,path,anchor):
    # Internal fixture seam only; model/registered handler cannot supply paths.
    from hermes_cli import kanban_db as kb
    before=_snapshot_path(path,anchor)
    connection=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        if not _unchanged(before,_snapshot_path(path,anchor)):raise PermissionError('Native board changed during open')
        task=kb.get_task(connection,task_id)
        result={'found':False} if task is None else {'found':True,'id':task.id,'title':task.title,'status':task.status,'result':task.result}
        if not _unchanged(before,_snapshot_path(path,anchor)):raise PermissionError('Native board changed during read')
        return json.dumps(result)
    finally:connection.close()

def list_work(args, **kwargs):
    if not pilot_required():raise PermissionError('Authenticated own-work authority required')
    admitted_board(None)
    if type(args) is not dict or set(args)-{'query','status','limit'}:raise ValueError('Only query, status and limit are supported')
    query=args.get('query','');state=args.get('status');limit=args.get('limit',10)
    if type(query) is not str or len(query)>200 or type(limit) is not int or not 1<=limit<=20:raise ValueError('Bounded query and limit required')
    from hermes_cli import kanban_db as kb
    if state is not None and (type(state) is not str or state not in kb.VALID_STATUSES):raise ValueError('Invalid task status')
    result=_list_native(_own_path(),PRODUCTION_ANCHOR,query=query,status=state,limit=limit)
    if not pilot_required():raise PermissionError('Own-work authority changed')
    admitted_board(None)
    return result

def _list_native(path,anchor,*,query='',status=None,limit=10):
    # Same existing native storage / same protected path. No recompute, migrate,
    # fallback board or model-selected path. Search window is explicit in output.
    from hermes_cli import kanban_db as kb
    before=_snapshot_path(path,anchor)
    connection=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
    try:
        if not _unchanged(before,_snapshot_path(path,anchor)):raise PermissionError('Native board changed during open')
        tasks=kb.list_tasks(connection,status=status,limit=201,order_by='created-desc')
        window=tasks[:200];needle=query.casefold().strip()
        matching=[t for t in window if not needle or any(needle in (v or '').casefold() for v in (t.title,t.body,t.result))]
        items=[{'id':t.id,'title':t.title[:300],'status':t.status,'assignee':t.assignee,'created_at':t.created_at,'summary':(t.result or t.body or '')[:320]} for t in matching[:limit]]
        if not _unchanged(before,_snapshot_path(path,anchor)):raise PermissionError('Native board changed during read')
        return json.dumps({'items':items,'has_more':len(matching)>limit or len(tasks)>200,'searched_tasks':len(window),'search_window':'up to 200 most recently created own-board tasks','read_only':True})
    finally:connection.close()

def allowed_names():
    if os.environ.get('HERMES_SESSION_SOURCE')=='kanban':
        from gateway.native_owner_board import current as native_current
        native_current()
        return {'kanban_show','kanban_complete','kanban_block','kanban_heartbeat'}
    from gateway.owner_api_intake import active, TOOL as INTAKE
    intake_active = active()
    names = {TOOL,INTAKE} if intake_active else {TOOL}
    if intake_active:
        from gateway.password_recovery_tools import TOOL as RESET
        names.add('owner_password_reset')
    from gateway.owner_device_tools import enabled, TOOL as DEVICE
    if enabled(): names.add(DEVICE)
    from gateway.owner_google_tools import enabled as google_enabled, TOOL as GOOGLE
    if google_enabled(): names.add(GOOGLE)
    from gateway.owner_delegation import active as delegation_active, TOOLS as DELEGATION_TOOLS
    if delegation_active(): names.update(DELEGATION_TOOLS)
    from gateway.member_board_tools import enabled as board_enabled, TOOLS as BOARD_TOOLS
    if board_enabled(): names.update(BOARD_TOOLS)
    return names

def register():
    from tools.registry import registry
    from hermes_cli.kanban_db import VALID_STATUSES
    registry.register(name=LIST_TOOL,toolset=TOOLSET,schema={'name':LIST_TOOL,'description':'List your own work or search titles, descriptions and results within up to 200 recent tasks in your workspace. Read-only; use returned task IDs for status details.','parameters':{'type':'object','properties':{'query':{'type':'string','maxLength':200},'status':{'type':'string','enum':sorted(VALID_STATUSES)},'limit':{'type':'integer','minimum':1,'maximum':20}},'additionalProperties':False}},handler=list_work)
    registry.register(name=TOOL,toolset=TOOLSET,schema={'name':TOOL,'description':'Read the status of a task in your own workspace.','parameters':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},handler=status)

def assert_schemas(schemas):
    actual = {x['function']['name'] for x in schemas}
    difference = actual ^ schema_names()
    if difference:
        raise PermissionError('Unexpected Josh pilot tools: difference ' + repr(sorted(difference)))

def prepare():
    register()
    from gateway.member_board_tools import register as register_board
    register_board(TOOLSET)
    from gateway.owner_delegation import register as register_delegation
    register_delegation()
    from gateway.owner_api_intake import register as register_intake
    register_intake()
    from gateway.owner_api_intake import active
    if active():
        from gateway.password_recovery_tools import register as register_reset
        register_reset(TOOLSET)
    from gateway.owner_device_tools import register as register_device
    register_device()
    from gateway.owner_google_tools import register as register_google
    register_google()
    from model_tools import get_tool_definitions
    assert_schemas(get_tool_definitions(enabled_toolsets=[TOOLSET],quiet_mode=True,skip_tool_search_assembly=True))
    return [TOOLSET]

"""Request-scoped provenance for native API-created tasks; no task producer."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import time
_ACTIVE=ContextVar('native_task_receipt_scope',default=None)

@contextmanager
def bind(*, profile, principal, run_id, session_id, persist, authenticated=False):
    # This binding is constructed by authenticated API dispatch, never tool args.
    effective_profile=profile or 'default'
    # execution_scope has already verified the installed company listener and
    # its exact human principal. Revalidate that binding; no display-name map.
    from gateway.josh_board_authority import app_binding
    app = app_binding() if authenticated else None
    if app is not None:
        if principal != app['principal'] or (profile is not None and profile != app['profile']):
            raise PermissionError('Native receipt outside authenticated company listener')
        effective_profile = app['profile']
    # Root API key authentication can have no cockpit principal configured.
    # Only the already-authenticated handler may supply authenticated=True.
    valid_principal=app is not None or (principal==profile if profile else principal in (None, '', 'default', 'chris'))
    if not authenticated or not valid_principal or not run_id or not session_id:
        yield
        return
    token=_ACTIVE.set((effective_profile,run_id,session_id,persist))
    try:yield
    finally:_ACTIVE.reset(token)

def record_created(conn, task):
    binding=_ACTIVE.get()
    if binding is None:return False
    profile,run,session,persist=binding
    if task is None or task.session_id!=session:return False
    # Board comes from the actual opened DB, never model-supplied display text.
    filename=next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),'')
    if not filename or Path(filename).name!='kanban.db':return False
    board=Path(filename).parent.name
    event={'event':'task.created','run_id':run,'session_id':session,
           'profile':profile,'board':board,'task_id':task.id,
           'source':'native_server','timestamp':time.time()}
    # Receipt persistence failure must not imply that task creation failed.
    # Caller returns the original task ID with false receipt status; never replay.
    try:return persist(event) is not None
    except Exception:return False


def current():
    """Internal request-scoped authenticated binding, never a model argument."""
    return _ACTIVE.get()

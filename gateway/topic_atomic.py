"""Authenticated deterministic Telegram topic creation, one native transaction.

No CLI/env authority bridge. Only the gateway interceptor binds the original
source/session; raw routing metadata cannot create a return destination.
"""
import argparse
import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from contextvars import copy_context


class TopicContractUnavailable(PermissionError):
    code = 'legal_owner_cell_contract_unavailable'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _command(cmd, board):
    if cmd[:4] != ['hermes', 'kanban', '--board', board] or cmd[4] != 'create':
        raise PermissionError('Only canonical same-board task creation is admitted')
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('title')
    for key in ('body', 'assignee', 'tenant', 'created-by', 'idempotency-key', 'workspace', 'reasoning-effort', 'edge-role'):
        p.add_argument('--'+key)
    p.add_argument('--priority', type=int, default=0)
    p.add_argument('--parent', action='append', default=[])
    p.add_argument('--json', action='store_true')
    return p.parse_args(cmd[5:])


def dispatch(envelope, *, board, revalidate, request_sha256):
    from gateway import department_topic_router as router
    from gateway.session_context import get_bound_session_env as get, set_session_vars, clear_session_vars
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban import _parse_workspace_flag
    from hermes_cli import task_return_intents as returns
    from hermes_constants import get_hermes_home
    if board != 'chris-home' or get('HERMES_SESSION_PLATFORM') != 'telegram':
        raise PermissionError('Authenticated canonical topic source required')
    if not all(get(k) for k in ('HERMES_SESSION_USER_ID','HERMES_SESSION_ID','HERMES_SESSION_CHAT_ID','HERMES_SESSION_MESSAGE_ID','HERMES_SESSION_KEY')):
        raise PermissionError('Incomplete authenticated source')
    for name, key in [('source_chat_id','CHAT_ID'),('source_thread_id','THREAD_ID'),('source_message_id','MESSAGE_ID')]:
        if str(envelope[name]) != str(get('HERMES_SESSION_'+key)):
            raise PermissionError('Envelope differs from authenticated message')
    expected = 'telegram:'+get('HERMES_SESSION_CHAT_ID')+':'+get('HERMES_SESSION_THREAD_ID')
    if envelope['return_surface'] != expected:
        raise PermissionError('Return target differs from original topic')
    revalidate()
    decision=router.resolve_department_topic(chat_id=get('HERMES_SESSION_CHAT_ID'),thread_id=get('HERMES_SESSION_THREAD_ID'),message_id=get('HERMES_SESSION_MESSAGE_ID'))
    canonical=decision.envelope.as_dict()
    if any(envelope.get(k)!=v for k,v in canonical.items()):
        raise PermissionError('Envelope differs from protected canonical topic policy')
    if envelope['department_id']=='legal':
        raise TopicContractUnavailable('Legal owner-cell contract requires a separate protected review repair; no task created')
    router._verify_reviewed_artifact(router._SAFE_DELEGATE_HELPER,router._REVIEWED_SAFE_DELEGATE_SHA256,'safe-delegate helper')
    router._verify_reviewed_artifact(router._DEFAULT_ROLE_REGISTRY_PATH,router._REVIEWED_ROLE_REGISTRY_SHA256,'role registry')
    helper_bytes=router._SAFE_DELEGATE_HELPER.read_bytes()
    # 2026-09-25 (Chris): helper digest pin removed.
    name='canonical_topic_builder_'+uuid.uuid4().hex
    spec=importlib.util.spec_from_file_location(name,router._SAFE_DELEGATE_HELPER)
    helper=importlib.util.module_from_spec(spec)
    sys.modules[name]=helper
    try:
        exec(compile(helper_bytes,str(router._SAFE_DELEGATE_HELPER),'exec'),helper.__dict__)
    finally:
        sys.modules.pop(name,None)
    if len(request_sha256)!=64 or any(c not in '0123456789abcdef' for c in request_sha256):
        raise PermissionError('Original inbound content digest required')
    stable_envelope=json.loads(_json(envelope))
    intake=stable_envelope.get('intake') or {}
    if intake.get('request_text') is not None:
        if hashlib.sha256(intake['request_text'].encode()).hexdigest()!=request_sha256:
            raise PermissionError('Envelope content differs from original inbound message')
    elif intake.get('handle'):
        if not router._PRIVATE_INTAKE_HANDLE_RE.fullmatch(intake['handle']):
            raise PermissionError('Invalid private intake handle')
        intake['handle']='original-content-sha256:'+request_sha256
    else:
        raise PermissionError('Original inbound content missing')
    request={'content_sha256':request_sha256,'envelope':stable_envelope,'user_id':get('HERMES_SESSION_USER_ID'),'profile':get('HERMES_SESSION_PROFILE') or 'default'}
    request_hash=hashlib.sha256(_json(request).encode()).hexdigest()
    session=get('HERMES_SESSION_ID')
    # Canonical board comes from protected activation, never CLI paths/env.
    home=Path(get_hermes_home())
    if home.name != '.hermes':raise PermissionError('Topic creation requires main gateway home')
    if helper.HERMES_HOME!=home:
        raise PermissionError('Canonical helper authority configuration mismatch')
    db=home/'kanban/boards'/board/'kanban.db'
    c=kb.connect(db)
    try:
        c.execute('CREATE TABLE IF NOT EXISTS topic_atomic_receipts (message_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, final_task TEXT NOT NULL, result_json TEXT NOT NULL)')
        c.commit()
        with kb.write_txn(c):
            binding=returns.capture(c,session_id=session,request=request)
            if binding is None:raise PermissionError('Native return authority missing')
            key=envelope['idempotency_key']
            old=c.execute('SELECT * FROM topic_atomic_receipts WHERE message_key=?',(key,)).fetchone()
            if old:
                if old['request_hash']!=request_hash:raise PermissionError('Topic replay differs from original request')
                # Preserve original creation session while checking current source
                # identity/target and authority. Never reset cursor on replay.
                saved=json.loads(c.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(old['final_task'],)).fetchone()[0])
                for field in ('board','profile','message_id','target'):
                    if saved[field]!=binding[field]:raise PermissionError('Topic return authority changed')
                returns.ensure(c,old['final_task'],saved,replay=True)
                revalidate()
                return json.loads(old['result_json'])
            final=[];created=[];output=[]
            def create(cmd):
                a=_command(cmd,board)
                kind,path=_parse_workspace_flag(a.workspace)
                if a.idempotency_key and c.execute("SELECT 1 FROM tasks WHERE idempotency_key=?",(a.idempotency_key,)).fetchone():
                    raise PermissionError('Legacy task exists without atomic return provenance; repair required')
                def isolated_create():
                    set_session_vars(platform='')
                    return kb.create_task(c,title=a.title,body=a.body,assignee=a.assignee,tenant=a.tenant,created_by=a.created_by,priority=a.priority,parents=a.parent,parent_edge_role=a.edge_role or 'dependency',workspace_kind=kind,workspace_path=path,idempotency_key=a.idempotency_key,reasoning_effort=a.reasoning_effort,session_id=session,board=board)
                tid=copy_context().run(isolated_create)
                goal=c.execute('SELECT required_reviewer_profile FROM owner_cell_goals WHERE task_id=?',(tid,)).fetchone()
                if envelope['department_id'] != 'legal' and (goal is None or not goal['required_reviewer_profile']):
                    raise PermissionError('Canonical topic requires native independent review')
                created.append(tid)
                return CompletedProcess(cmd,0,_json({'id':tid}),'')
            def subscribe(route,tid,profile):
                if tid not in created or route.board!=board or helper.telegram_notify_target(route)!={'platform':'telegram','chat_id':binding['target']['chat_id'],'thread_id':binding['target']['thread_id']}:
                    raise PermissionError('Canonical chain return mismatch')
                if profile is not None and profile!=binding['profile']:raise PermissionError('Return profile mismatch')
                final.append(tid)
                return {'ok':True,'atomic':True}
            args=router.build_safe_delegate_command(envelope,board=board)[2:]
            code=helper.main(args,native_hooks={'create':create,'subscribe':subscribe,'command_board_root':True,'registry':router._DEFAULT_ROLE_REGISTRY_PATH},result_callback=output.append)
            if code or len(output)!=1 or len(final)!=1:raise RuntimeError('Canonical chain did not produce one final result target')
            returns.ensure(c,final[0],binding)
            revalidate()
            router._verify_reviewed_artifact(router._SAFE_DELEGATE_HELPER,router._REVIEWED_SAFE_DELEGATE_SHA256,'safe-delegate helper')
            router._verify_reviewed_artifact(router._DEFAULT_ROLE_REGISTRY_PATH,router._REVIEWED_ROLE_REGISTRY_SHA256,'role registry')
            c.execute('INSERT INTO topic_atomic_receipts VALUES(?,?,?,?)',(key,request_hash,final[0],_json(output[0])))
            return output[0]
    finally:c.close()

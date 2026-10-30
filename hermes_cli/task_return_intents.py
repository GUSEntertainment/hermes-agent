"""Atomic return intent for authenticated native API tasks, not model authority.

Only existing server-bound native request and session ContextVars activate this
lane. CLI/env-only callers retain their existing semantics. No delivery occurs
here: the existing notifier consumes the subscription committed with the task.
"""
import hashlib
import json
from pathlib import Path

KIND = 'native-api-return-v1'

def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def capture(conn, *, session_id, request):
    from gateway.owner_delegation import active, return_binding
    if active():
        return return_binding(conn, session_id=session_id, request=request)
    from gateway.native_task_receipts import current
    scope = current()
    if scope is None:
        from gateway import delegation_handoff
        if delegation_handoff._token() is not None:
            return _capture_handoff(conn, session_id=session_id, request=request)
        return _capture_gateway(conn, session_id=session_id, request=request)
    from gateway.session_context import get_bound_session_env as get
    from gateway.josh_board_authority import app_binding
    from gateway.native_task_routing import enrollment
    profile, run, session, _persist = scope
    if session_id != session or get('HERMES_SESSION_PLATFORM') != 'api_server':
        raise PermissionError('Authenticated task return session missing')
    if get('HERMES_SESSION_ID') != session:
        raise PermissionError('Authenticated task return session mismatch')
    chat = get('HERMES_SESSION_CHAT_ID')
    key = get('HERMES_SESSION_KEY')
    if not chat or not key:
        raise PermissionError('Authenticated task return target missing')
    db = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not db or Path(db).name != 'kanban.db':
        raise PermissionError('Native board database required')
    board = Path(db).parent.name
    app = app_binding()
    authority = app if app is not None else enrollment(profile)
    allowed = authority.get('allowed_boards', [authority.get('board')])
    if board not in allowed:
        raise PermissionError('Task return outside authenticated board')
    target = dict(platform='api_server', chat_id=chat,
                  thread_id=get('HERMES_SESSION_THREAD_ID') or None,
                  user_id=get('HERMES_SESSION_USER_ID') or None,
                  user_id_alt=get('HERMES_SESSION_USER_ID_ALT') or None,
                  chat_type=get('HERMES_SESSION_CHAT_TYPE') or 'dm',
                  notifier_profile=profile, delivery_mode='notify+wake',
                  delivery_metadata={'session_key': key})
    # The canonical builder stamps this informational field anew on retry.
    # Preserve every authority/package field and the user's body verbatim.
    from hermes_cli.owner_cell import _routing_metadata, _ROUTING_METADATA_RE
    request = dict(request)
    body = request.get('body')
    metadata = _routing_metadata(body)
    if isinstance(metadata.get('delegation_package'), dict):
        metadata['delegation_package'].pop('created_at', None)
        request['body'] = {'text': _ROUTING_METADATA_RE.sub('', body or ''),
                           'routing_metadata': metadata}
    return dict(kind=KIND, profile=profile, run_id=run, session_id=session,
                board=board, authority=authority, target=target,
                request_sha256=hashlib.sha256(_encode(request).encode()).hexdigest())

def _capture_handoff(conn, *, session_id, request):
    # Registry-verified run/session handoff (gateway/delegation_handoff.py).
    # Invalid, expired, reused or foreign tokens fail closed.
    from gateway import delegation_handoff
    from hermes_cli.owner_cell import _routing_metadata, _ROUTING_METADATA_RE
    request = dict(request)
    body = request.get('body')
    metadata = _routing_metadata(body)
    if isinstance(metadata.get('delegation_package'), dict):
        metadata['delegation_package'].pop('created_at', None)
        request['body'] = {'text': _ROUTING_METADATA_RE.sub('', body or ''),
                           'routing_metadata': metadata}
    digest = hashlib.sha256(_encode(request).encode()).hexdigest()
    snap = delegation_handoff.consume(digest)
    if snap is None or session_id != snap['session_id']:
        raise PermissionError('Delegation handoff session mismatch')
    db = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not db or Path(db).name != 'kanban.db':
        raise PermissionError('Native board database required')
    board = Path(db).parent.name
    authority = snap['authority']
    if board not in authority.get('allowed_boards', [authority.get('board')]):
        raise PermissionError('Task return outside authenticated board')
    return dict(kind=KIND, profile=snap['profile'], run_id=snap['run_id'], session_id=snap['session_id'],
                board=board, authority=authority, target=snap['target'], request_sha256=digest)


def _capture_gateway(conn, *, session_id, request):
    from gateway.session_context import get_bound_session_env as get
    platform = get('HERMES_SESSION_PLATFORM')
    if platform not in ('telegram', 'photon'):
        return None
    # These values originate in GatewayRunner._set_session_env after source
    # authorization. Never read the process environment or model parameters.
    session = get('HERMES_SESSION_ID')
    chat = get('HERMES_SESSION_CHAT_ID')
    user = get('HERMES_SESSION_USER_ID')
    message = get('HERMES_SESSION_MESSAGE_ID')
    key = get('HERMES_SESSION_KEY')
    if not all((session, chat, user, message, key)) or session_id != session:
        raise PermissionError('Complete bound gateway source required')
    db = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not db or Path(db).name != 'kanban.db':
        raise PermissionError('Native board database required')
    board = Path(db).parent.name
    from gateway.josh_board_authority import admitted_board
    if admitted_board(board) != board:
        raise PermissionError('Gateway task return outside admitted board')
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    profile = get('HERMES_SESSION_PROFILE') or (home.name if home.parent.name == 'profiles' else 'default')
    thread = get('HERMES_SESSION_THREAD_ID') or None
    chat_type = get('HERMES_SESSION_CHAT_TYPE') or 'dm'
    metadata = {'session_key': key, 'chat_type': chat_type}
    if thread:
        metadata['thread_id'] = thread
        if platform == 'telegram' and chat_type.lower() in ('dm', 'direct', 'private'):
            metadata['telegram_dm_topic_reply_fallback'] = True
            if thread != '1':metadata['direct_messages_topic_id'] = thread
            metadata['telegram_reply_to_message_id'] = message
    target = dict(platform=platform, chat_id=chat, thread_id=thread,
                  user_id=user, user_id_alt=get('HERMES_SESSION_USER_ID_ALT') or None,
                  chat_type=chat_type, notifier_profile=profile,
                  delivery_mode='notify+wake', delivery_metadata=metadata)
    return dict(kind='gateway-return-v1', profile=profile, session_id=session,
                board=board, message_id=message, target=target,
                request_sha256=hashlib.sha256(_encode(request).encode()).hexdigest())


def ensure(conn, task_id, binding, *, replay=False):
    """Must run within the creator's SQLite transaction; replay never resets cursors."""
    if not conn.in_transaction:
        raise RuntimeError('Task return requires the task creation transaction')
    raw = _encode(binding)
    old = conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?', (task_id,)).fetchone()
    if replay:
        if old is None or old[0] != raw:
            raise PermissionError('Task idempotency key belongs to a different return intent')
    elif old is not None:
        raise PermissionError('Task return intent already exists')
    else:
        conn.execute('INSERT INTO task_return_intents(task_id,binding_json) VALUES (?,?)', (task_id, raw))
    target = binding['target']
    old_sub = conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?',
                           (task_id, target['platform'], target['chat_id'], target['thread_id'] or '')).fetchone()
    if replay and old_sub is None:
        raise PermissionError('Committed return subscription missing; delivery history requires repair')
    if old_sub is not None:
        for key in ('user_id', 'user_id_alt', 'chat_type', 'notifier_profile', 'delivery_mode'):
            if old_sub[key] != target[key]:
                raise PermissionError('Committed return subscription changed')
        metadata = json.loads(old_sub['delivery_metadata'] or '{}')
        if any(metadata.get(k) != v for k, v in target['delivery_metadata'].items()):
            raise PermissionError('Committed return subscription changed')
    else:
        from hermes_cli.kanban_db import add_notify_sub
        add_notify_sub(conn, task_id=task_id, **target)

    # Separate private recipient custody, committed with this original task and
    # subscription. Unknown surfaces remain ordinary returns, with no backfill.
    from hermes_cli.task_document_custody import ensure as document_custody
    document_custody(conn, task_id, binding, replay=replay)

"""Registry-verified run/session handoff for app-delegated task returns.

An authenticated /v1/runs turn snapshots its native-api return binding (the
same server-bound ContextVars task_return_intents.capture() reads) into a
private registry. The app's signed server route may then mint ONE short-lived
token for that exact run+session and pass it to the safe_delegate_card helper
via env (never argv). capture() in the helper's process verifies the token
against this registry and binds the native-api-return-v1 intent. No token, no
ambient trust: subprocess callers without a valid handoff keep old behaviour;
an invalid/expired/foreign token fails closed.
"""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import os
import secrets
import sqlite3
import time

ENV = 'HERMES_DELEGATION_HANDOFF'
TTL = 600
SNAPSHOT_TTL = 86400
ROOT = Path(os.environ.get('HERMES_DELEGATION_HANDOFF_ROOT') or '/Users/Chris/.hermes')


def _path():
    return ROOT / 'delegation_handoffs.db'


def _connect():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    conn.execute('CREATE TABLE IF NOT EXISTS run_bindings(run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,'
                 ' principal TEXT, binding_json TEXT NOT NULL, created_at REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS tokens(token_sha256 TEXT PRIMARY KEY, run_id TEXT NOT NULL,'
                 ' session_id TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL, request_sha256 TEXT)')
    return conn


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot(principal=None):
    """Called inside _run_agent_sync while the native binding is live."""
    from gateway.native_task_receipts import current
    scope = current()
    if scope is None:
        return False
    from gateway.session_context import get_bound_session_env as get
    profile, run, session, _persist = scope
    if get('HERMES_SESSION_PLATFORM') != 'api_server' or get('HERMES_SESSION_ID') != session:
        return False
    chat, key = get('HERMES_SESSION_CHAT_ID'), get('HERMES_SESSION_KEY')
    if not chat or not key:
        return False
    from gateway.josh_board_authority import app_binding
    from gateway.native_task_routing import enrollment
    app = app_binding()
    authority = app if app is not None else enrollment(profile)
    target = dict(platform='api_server', chat_id=chat,
                  thread_id=get('HERMES_SESSION_THREAD_ID') or None,
                  user_id=get('HERMES_SESSION_USER_ID') or None,
                  user_id_alt=get('HERMES_SESSION_USER_ID_ALT') or None,
                  chat_type=get('HERMES_SESSION_CHAT_TYPE') or 'dm',
                  notifier_profile=profile, delivery_mode='notify+wake',
                  delivery_metadata={'session_key': key})
    binding = dict(profile=profile, run_id=run, session_id=session, authority=authority, target=target)
    now = time.time()
    with closing(_connect()) as conn:
        conn.execute('DELETE FROM run_bindings WHERE created_at<?', (now - SNAPSHOT_TTL,))
        conn.execute('DELETE FROM tokens WHERE expires_at<?', (now - SNAPSHOT_TTL,))
        conn.execute('INSERT OR REPLACE INTO run_bindings VALUES (?,?,?,?,?)',
                     (run, session, principal, json.dumps(binding, sort_keys=True), now))
    return True


def mint(run_id, session_id):
    """Mint a single-use token for an authenticated owner of run_id."""
    with closing(_connect()) as conn:
        row = conn.execute('SELECT session_id FROM run_bindings WHERE run_id=?', (run_id,)).fetchone()
        if row is None or not session_id or row[0] != session_id:
            raise PermissionError('No verified run binding for this session')
        token = secrets.token_urlsafe(32)
        conn.execute('INSERT INTO tokens(token_sha256,run_id,session_id,expires_at) VALUES (?,?,?,?)',
                     (_sha(token), run_id, session_id, time.time() + TTL))
    return token


def _token():
    value = os.environ.get(ENV, '').strip()
    return value or None


def session():
    """Peek the session the env token is bound to (no consumption); None when absent."""
    token = _token()
    if token is None:
        return None
    with closing(_connect()) as conn:
        row = conn.execute('SELECT session_id, expires_at FROM tokens WHERE token_sha256=?', (_sha(token),)).fetchone()
    if row is None or row[1] < time.time():
        raise PermissionError('Delegation handoff invalid or expired')
    return row[0]


def consume(request_sha256):
    """Atomically bind the token to one request; same request may replay (idempotency)."""
    token = _token()
    if token is None:
        return None
    digest = _sha(token)
    with closing(_connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT run_id, session_id, expires_at, used_at, request_sha256 FROM tokens'
                               ' WHERE token_sha256=?', (digest,)).fetchone()
            if row is None or row[2] < time.time():
                raise PermissionError('Delegation handoff invalid or expired')
            run_id, session_id, _exp, used, bound = row
            if used is not None and bound != request_sha256:
                raise PermissionError('Delegation handoff already used')
            found = conn.execute('SELECT session_id, binding_json FROM run_bindings WHERE run_id=?', (run_id,)).fetchone()
            if found is None or found[0] != session_id:
                raise PermissionError('Delegation handoff run binding missing')
            if used is None:
                conn.execute('UPDATE tokens SET used_at=?, request_sha256=? WHERE token_sha256=?',
                             (time.time(), request_sha256, digest))
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise
    return json.loads(found[1])

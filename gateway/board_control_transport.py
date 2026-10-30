"""One-use member board-controller requests; no identity from model arguments.

The installer creates the key and ledger explicitly. Normal requests never
create authority material. Same-user trusted operators remain outside the
worker sandbox boundary; workers must not read this private directory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

ROOT = Path('/Users/Chris/.hermes')
PURPOSE = 'hermes-board-owner-control-v1'
DOMAIN = (PURPOSE + '\0').encode()
TTL_SECONDS = 30
MAX_BYTES = 32768
GRANT_FIELDS = {'principal', 'profile', 'board', 'fingerprint'}
FIELDS = {'purpose', 'payload', 'grant', 'session_id', 'issued_at', 'expires_at',
          'nonce', 'payload_sha256', 'signature'}


def key_path():
    return ROOT / 'private' / 'board-owner-control' / 'key'


def ledger_path():
    return ROOT / 'private' / 'board-owner-control' / 'nonces.sqlite3'


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise PermissionError('Invalid controller request encoding') from exc
    if len(raw) > MAX_BYTES:
        raise PermissionError('Controller request exceeds size limit')
    return raw


def _grant(value):
    if type(value) is not dict or set(value) != GRANT_FIELDS:
        raise PermissionError('Exact member grant required')
    for name in ('principal', 'profile', 'board'):
        if type(value[name]) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,99}', value[name]):
            raise PermissionError('Canonical member grant required')
    if type(value['fingerprint']) is not str or not re.fullmatch('[a-f0-9]{64}', value['fingerprint']):
        raise PermissionError('Current enrollment fingerprint required')
    return dict(value)


def _deny_worker():
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import get_bound_session_env
    if (is_delegated_child_context() or os.environ.get('HERMES_KANBAN_TASK')
            or os.environ.get('HERMES_SESSION_SOURCE') in ('kanban', 'cron')
            or get_bound_session_env('HERMES_SESSION_SOURCE') in ('kanban', 'cron')):
        raise PermissionError('A direct member session is required')


def _bound_session(session_id):
    from gateway.session_context import get_bound_session_env
    if type(session_id) is not str or not session_id or len(session_id) > 256:
        raise PermissionError('Current bound session required')
    bound = {get_bound_session_env(name) for name in
             ('HERMES_SESSION_ID', 'HERMES_SESSION_KEY')}
    bound.discard('')
    bound.discard(None)
    if bound:
        if session_id not in bound:
            raise PermissionError('Controller session mismatch')
        return
    # Photon fallback is the exact registered inbound message, never env data.
    from plugins.platforms.photon.secure_intake.gateway_binding import current_identity
    from plugins.platforms.photon.secure_intake.request_registry import encoded
    from plugins.platforms.photon.secure_intake.trusted_intake import _CURRENT
    identity = current_identity()
    witness = _CURRENT.get()
    if (identity.get('platform') != 'photon' or identity.get('chat_type') != 'dm'
            or identity.get('message') != session_id or not witness
            or witness[0] != encoded(identity)):
        raise PermissionError('Current authenticated transport session required')


def _key():
    from gateway.app_business_authority import protected_bytes
    raw = protected_bytes(key_path(), private=True, anchor=ROOT)
    if len(raw) != 32:
        raise PermissionError('Controller signing key is unavailable')
    return raw


def _sign(unsigned, key):
    return hmac.new(key, DOMAIN + _canonical(unsigned), hashlib.sha256).hexdigest()


def issue(payload, grant, session_id):
    """Sign only the actual current member scope and exact requested operation."""
    _deny_worker()
    from hermes_cli.board_owner_manage import require_owner
    grant = _grant(grant)
    if require_owner() != grant:
        raise PermissionError('Controller grant does not match current member')
    _bound_session(session_id)
    if type(payload) is not dict:
        raise PermissionError('Controller payload must be an object')
    # Detach mutable inputs before hashing/signing or crossing the process boundary.
    payload = json.loads(_canonical(payload))
    if payload.get('board') != grant['board']:
        from hermes_cli.board_owner_manage import administrator_board
        if administrator_board(grant, payload.get('board')) is None:
            raise PermissionError('Controller request must name the enrolled board')
    now = int(time.time())
    unsigned = dict(purpose=PURPOSE, payload=payload, grant=grant, session_id=session_id,
                    issued_at=now, expires_at=now + TTL_SECONDS,
                    nonce=secrets.token_hex(16),
                    payload_sha256=hashlib.sha256(_canonical(payload)).hexdigest())
    envelope = dict(unsigned, signature=_sign(unsigned, _key()))
    _canonical(envelope)
    return envelope


def _ledger_stat():
    from gateway.gus_worker_authority import protected
    value = protected(ledger_path(), ROOT)
    if stat.S_IMODE(value.st_mode) != 0o600:
        raise PermissionError('Controller replay ledger must be private')
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode, value.st_nlink)


def initialize_ledger(path):
    """Installer-only initialization; refuses any pre-existing file or link."""
    path = Path(path)
    if path != ledger_path():
        raise PermissionError('Unexpected controller replay ledger location')
    # Checking the installed key also validates the protected parent path.
    _key()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE nonces (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL, request_sha256 TEXT NOT NULL)')
    _ledger_stat()


def _consume(nonce, expires, request_sha256, now):
    before = _ledger_stat()
    # mode=rw prevents an absent ledger from being recreated by a normal request.
    conn = sqlite3.connect(ledger_path().as_uri() + '?mode=rw', uri=True, timeout=5)
    try:
        if _ledger_stat() != before:
            raise PermissionError('Controller replay ledger replaced')
        conn.execute('BEGIN IMMEDIATE')
        # Enrollment refresh or lock contention must not extend the signed TTL.
        now = int(time.time())
        if now >= expires:
            raise PermissionError('Controller request expired before admission')
        columns = conn.execute('PRAGMA table_info(nonces)').fetchall()
        if [(r[1], r[2], r[5]) for r in columns] != [
                ('nonce', 'TEXT', 1), ('expires', 'INTEGER', 0), ('request_sha256', 'TEXT', 0)]:
            raise PermissionError('Controller replay ledger schema mismatch')
        conn.execute('DELETE FROM nonces WHERE expires < ?', (now,))
        conn.execute('INSERT INTO nonces VALUES (?, ?, ?)', (nonce, expires, request_sha256))
        if _ledger_stat() != before:
            raise PermissionError('Controller replay ledger replaced')
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PermissionError('Controller request was already consumed; inspect the card before retrying') from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify(envelope):
    """Verify, freshly revalidate enrollment and consume one exact operation."""
    _deny_worker()
    if type(envelope) is not dict or set(envelope) != FIELDS:
        raise PermissionError('Exact signed controller envelope required')
    envelope = json.loads(_canonical(envelope))
    if envelope['purpose'] != PURPOSE:
        raise PermissionError('Wrong controller request purpose')
    supplied = envelope.pop('signature')
    if type(supplied) is not str or not re.fullmatch('[a-f0-9]{64}', supplied):
        raise PermissionError('Invalid controller signature')
    if not hmac.compare_digest(supplied, _sign(envelope, _key())):
        raise PermissionError('Invalid controller signature')
    if (type(envelope['issued_at']) is not int or type(envelope['expires_at']) is not int
            or not 0 < envelope['expires_at'] - envelope['issued_at'] <= TTL_SECONDS):
        raise PermissionError('Invalid controller request lifetime')
    now = int(time.time())
    if not envelope['issued_at'] <= now < envelope['expires_at']:
        raise PermissionError('Controller request expired or not yet valid')
    if type(envelope['nonce']) is not str or not re.fullmatch('[a-f0-9]{32}', envelope['nonce']):
        raise PermissionError('Invalid controller request nonce')
    session_id = envelope['session_id']
    if type(session_id) is not str or not session_id or len(session_id) > 256:
        raise PermissionError('Invalid controller request session')
    payload, grant = envelope['payload'], _grant(envelope['grant'])
    if type(payload) is not dict:
        raise PermissionError('Controller request board mismatch')
    if payload.get('board') != grant['board']:
        from hermes_cli.board_owner_manage import administrator_board
        if administrator_board(grant, payload.get('board')) is None:
            raise PermissionError('Controller request must name the enrolled board')
    payload_hash = hashlib.sha256(_canonical(payload)).hexdigest()
    if envelope['payload_sha256'] != payload_hash:
        raise PermissionError('Controller payload changed')
    from hermes_cli.board_owner_manage import refresh_member_grant
    if refresh_member_grant(grant) != grant:
        raise PermissionError('Member enrollment changed')
    _consume(envelope['nonce'], envelope['expires_at'], payload_hash, now)
    return payload, grant

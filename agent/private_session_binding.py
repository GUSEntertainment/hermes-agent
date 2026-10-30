"""Ephemeral server parent identity for transcript-free admitted workers."""
import threading
import weakref
import sqlite3
import contextlib
from pathlib import Path

_LOCK = threading.RLock()
_PARENTS = weakref.WeakKeyDictionary()


def _run_identity(contract):
    # The path and inode are authenticated by _current's durable validator.
    with contextlib.closing(sqlite3.connect(Path(contract['board_database']['path']).as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT current_run_id,worker_pid,worker_process_start,claim_lock FROM tasks WHERE id=?', (contract['task'],)).fetchone()
    if row is None or any(value is None for value in row):
        raise PermissionError('Private parent run identity unavailable')
    return tuple(row)


def _current():
    from agent.private_worker_mode import (
        get_authenticated_worker_contract, contract_digest, _default_durable_validator,
    )
    contract = get_authenticated_worker_contract()
    if contract is None:
        return None
    if not _default_durable_validator(contract, contract_digest(contract)):
        raise PermissionError('Private parent admission is no longer live')
    if contract.get('member_company_policy'):
        from gateway.member_worker_policy import verify
        verify(contract['member_company_policy'])
    return contract


def bind_parent(agent):
    """Called only after AIAgent initialization finalized its session identity."""
    contract = _current()
    if contract is None:
        return  # A privacy environment switch alone is not authority.
    if getattr(agent, '_parent_session_id', None) is not None:
        return  # A delegated child cannot become the native parent.
    session = getattr(agent, 'session_id', None)
    if not isinstance(session, str) or not session or not getattr(agent, '_persist_disabled', False):
        raise PermissionError('Invalid transcript-free parent object')
    with _LOCK:
        if any(other is not agent and value[0] == session for other, value in _PARENTS.items()):
            raise PermissionError('Ambiguous private parent session')
        prior = _PARENTS.get(agent)
        value = (session, contract, _run_identity(contract))
        if prior is not None and prior != value:
            raise PermissionError('Private parent identity changed')
        _PARENTS[agent] = value


def validate_parent(contract, session):
    """Fresh FD/PID/claim/source proof plus live object; no durable session fallback."""
    current = _current()
    if current is None:
        return False
    if current != contract:
        raise PermissionError('Private parent admission mismatch')
    with _LOCK:
        matches = [(agent, value) for agent, value in _PARENTS.items() if value[0] == session]
        if len(matches) != 1:
            raise PermissionError('Unbound private native parent session')
        agent, (_, bound, run) = matches[0]
        if (bound != contract or agent.session_id != session
                or run != _run_identity(contract)
                or getattr(agent, '_parent_session_id', None) is not None
                or not getattr(agent, '_persist_disabled', False)):
            raise PermissionError('Private parent object changed')
    return True

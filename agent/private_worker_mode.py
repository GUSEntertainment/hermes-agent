"""Authenticated, fail-closed process state for private confined workers."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

_MAX_CONTRACT_BYTES = 128 * 1024
_LOCK = threading.Lock()
_AUTHENTICATED_CONTRACT: dict[str, Any] | None = None
_CONSUMED = False
_FAILED = False
_PRIVATE_QUERY: str | None = None


class PrivateWorkerContractError(PermissionError):
    """The dispatcher admission handoff is absent, malformed, or stale."""


def _canonical_bytes(contract: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(contract), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def _validate_shape(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise PrivateWorkerContractError("worker admission contract must be an object")
    required = {
        "version", "task", "admission_nonce", "board_database", "profile",
        "profile_home", "profile_config_digest",
        "provider", "model", "fallbacks", "reasoning", "toolsets", "skills",
        "skill_dependencies", "privacy", "tenant", "workspace",
        "workspace_confinement",
        "workspace_read_only_roots", "workspace_read_only_root_identities",
    }
    if set(contract) not in (required, required | {"member_company_policy"}):
        raise PrivateWorkerContractError("worker admission contract schema is invalid")
    if contract["version"] != "hermes.worker-admission/v1":
        raise PrivateWorkerContractError("worker admission contract version is invalid")
    for key in (
        "task", "admission_nonce", "profile", "profile_home",
        "profile_config_digest", "provider", "model",
    ):
        if not isinstance(contract[key], str) or not contract[key].strip():
            raise PrivateWorkerContractError("worker admission contract identity is invalid")
    board_database = contract["board_database"]
    if (
        not isinstance(board_database, dict)
        or set(board_database) != {"path", "device", "inode"}
        or not isinstance(board_database["path"], str)
        or not Path(board_database["path"]).is_absolute()
        or not isinstance(board_database["device"], int)
        or not isinstance(board_database["inode"], int)
        or board_database["device"] < 0
        or board_database["inode"] <= 0
    ):
        raise PrivateWorkerContractError("worker admission board identity is invalid")
    if not isinstance(contract["toolsets"], list) or not all(
        isinstance(item, str) and item for item in contract["toolsets"]
    ):
        raise PrivateWorkerContractError("worker admission toolsets are invalid")
    if not isinstance(contract["skills"], list) or not all(
        isinstance(item, str) and item for item in contract["skills"]
    ):
        raise PrivateWorkerContractError("worker admission skills are invalid")
    workspace = contract["workspace"]
    if not isinstance(workspace, dict) or set(workspace) != {"kind", "path", "branch", "project"}:
        raise PrivateWorkerContractError("worker admission workspace is invalid")
    if not isinstance(workspace.get("path"), str) or not Path(workspace["path"]).is_absolute():
        raise PrivateWorkerContractError("worker admission workspace path is invalid")
    if contract["workspace_confinement"] is not True:
        raise PrivateWorkerContractError("worker admission does not require confinement")
    if not isinstance(contract["workspace_read_only_roots"], list) or not all(
        isinstance(item, str) and Path(item).is_absolute()
        for item in contract["workspace_read_only_roots"]
    ):
        raise PrivateWorkerContractError("worker admission read-only roots are invalid")
    root_identities = contract["workspace_read_only_root_identities"]
    if (
        not isinstance(root_identities, list)
        or len(root_identities) != len(contract["workspace_read_only_roots"])
    ):
        raise PrivateWorkerContractError("worker admission read-only identities are invalid")
    for raw, identity in zip(contract["workspace_read_only_roots"], root_identities):
        if (
            not isinstance(identity, dict)
            or set(identity) != {"path", "device", "inode"}
            or identity["path"] != os.path.realpath(raw)
            or not isinstance(identity["device"], int)
            or not isinstance(identity["inode"], int)
            or identity["device"] < 0
            or identity["inode"] <= 0
        ):
            raise PrivateWorkerContractError(
                "worker admission read-only identities are invalid"
            )
    return copy.deepcopy(contract)


def activate_private_worker_mode() -> None:
    """Disable logging and durable transcript paths before private intake."""
    os.environ["HERMES_PRIVATE_WORKER"] = "1"
    logging.disable(logging.CRITICAL)


def install_authenticated_worker_contract(
    contract: Mapping[str, Any],
    *,
    durable_validator: Callable[[dict[str, Any], str], bool],
) -> dict[str, Any]:
    """Install a validated immutable admission contract exactly once.

    ``durable_validator`` must bind the canonical digest to the live task/run,
    claim, profile and worker PID.  It is deliberately mandatory: environment
    variables and the pipe payload alone never constitute authority.
    """
    global _AUTHENTICATED_CONTRACT
    checked = _validate_shape(contract)
    digest = contract_digest(checked)
    if checked.get("member_company_policy"):
        from gateway.member_worker_policy import verify as verify_member_policy
        verify_member_policy(checked["member_company_policy"])
    if not durable_validator(checked, digest):
        raise PrivateWorkerContractError("worker admission contract is not live")
    with _LOCK:
        if _AUTHENTICATED_CONTRACT is not None:
            if _AUTHENTICATED_CONTRACT != checked:
                raise PrivateWorkerContractError("worker admission contract is already bound")
            return copy.deepcopy(_AUTHENTICATED_CONTRACT)
        _AUTHENTICATED_CONTRACT = copy.deepcopy(checked)
    activate_private_worker_mode()
    return copy.deepcopy(checked)


def _read_contract_fd(fd: int) -> Any:
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        raise PrivateWorkerContractError("worker admission must use an anonymous pipe")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, _MAX_CONTRACT_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_CONTRACT_BYTES:
            raise PrivateWorkerContractError("worker admission contract is oversized")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateWorkerContractError("worker admission contract is malformed") from exc
    return value


def _consume_handoff() -> tuple[dict[str, Any], str | None]:
    """Consume and authenticate the inherited handoff exactly once.

    Presence of the descriptor is an irreversible private-worker marker.  Any
    read, schema, or durable-validation failure therefore raises and remains
    failed closed for the lifetime of the process.
    """
    global _CONSUMED, _FAILED, _PRIVATE_QUERY
    with _LOCK:
        if _FAILED:
            raise PrivateWorkerContractError("worker admission previously failed")
        if _AUTHENTICATED_CONTRACT is not None:
            return copy.deepcopy(_AUTHENTICATED_CONTRACT), _PRIVATE_QUERY
        if _CONSUMED:
            raise PrivateWorkerContractError("worker admission was consumed without authority")
        _CONSUMED = True
    raw_fd = os.environ.get("HERMES_WORKER_ADMISSION_FD", "").strip()
    if not raw_fd:
        raise PrivateWorkerContractError("worker admission descriptor is missing")
    # Suppress before reading or parsing a single private byte.
    activate_private_worker_mode()
    fd: int | None = None
    try:
        fd = int(raw_fd)
        payload = _read_contract_fd(fd)
        if isinstance(payload, dict) and set(payload) == {"contract", "query"}:
            contract = _validate_shape(payload["contract"])
            query = payload["query"]
            if not isinstance(query, str) or not query.strip():
                raise PrivateWorkerContractError("private worker query is invalid")
        else:
            contract = _validate_shape(payload)
            query = None
        installed = install_authenticated_worker_contract(
            contract, durable_validator=_default_durable_validator
        )
        with _LOCK:
            _PRIVATE_QUERY = query
        return installed, query
    except (ValueError, OSError, PrivateWorkerContractError):
        with _LOCK:
            _FAILED = True
        raise
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except OSError:
            pass


def consume_private_worker_intake() -> str:
    """Authenticate the private handoff and return its non-argv query."""
    _contract, query = _consume_handoff()
    if query is None:
        raise PrivateWorkerContractError("private worker handoff has no query")
    return query


def _default_durable_validator(contract: dict[str, Any], digest: str) -> bool:
    """Bind the pipe contract to the live durable claim and exact child PID."""
    try:
        from hermes_cli import kanban_db as kb

        board = os.environ.get("HERMES_KANBAN_BOARD") or None
        with kb.contextlib.closing(kb.connect(board=board)) as conn:
            database_identity = kb._canonical_database_identity(conn)
            expected_database_identity = (
                "file",
                os.path.realpath(contract["board_database"]["path"]),
                int(contract["board_database"]["device"]),
                int(contract["board_database"]["inode"]),
            )
            if database_identity != expected_database_identity:
                return False
            deadline = time.monotonic() + 3.0
            while True:
                task = conn.execute(
                    "SELECT id, assignee, status, claim_lock, claim_expires, current_run_id, "
                    "worker_pid, worker_process_start, "
                    "worker_admission_digest, worker_admission_contract FROM tasks WHERE id=?",
                    (contract["task"],),
                ).fetchone()
                run = None
                if task is not None and task["current_run_id"] is not None:
                    run = conn.execute(
                        "SELECT task_id,profile,status,claim_lock,claim_expires,worker_pid,"
                        "worker_process_start,worker_admission_digest,"
                        "worker_admission_contract,ended_at FROM task_runs WHERE id=?",
                        (task["current_run_id"],),
                    ).fetchone()
                if task and run and task["worker_pid"] == os.getpid() and run["worker_pid"] == os.getpid():
                    encoded = _canonical_bytes(contract).decode("utf-8")
                    process_identity = kb._worker_process_identity(os.getpid())
                    claim_expires = int(task["claim_expires"] or 0)
                    return bool(
                        task["status"] == "running"
                        and run["status"] == "running"
                        and run["ended_at"] is None
                        and run["task_id"] == contract["task"] == task["id"]
                        and task["assignee"] == contract["profile"] == run["profile"]
                        and task["claim_lock"] == run["claim_lock"]
                        and int(run["claim_expires"] or 0) == claim_expires
                        and claim_expires > int(time.time())
                        and process_identity is not None
                        and float(task["worker_process_start"] or 0)
                        == float(process_identity[1])
                        and float(run["worker_process_start"] or 0)
                        == float(process_identity[1])
                        and task["worker_admission_digest"] == digest == run["worker_admission_digest"]
                        and task["worker_admission_contract"] == encoded == run["worker_admission_contract"]
                    )
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
    except Exception:
        return False


def get_authenticated_worker_contract() -> dict[str, Any] | None:
    """Return the process-bound contract, consuming its inherited pipe once."""
    if not os.environ.get("HERMES_WORKER_ADMISSION_FD", "").strip():
        with _LOCK:
            if _AUTHENTICATED_CONTRACT is not None:
                return copy.deepcopy(_AUTHENTICATED_CONTRACT)
        return None
    contract, _query = _consume_handoff()
    return contract


def _reset_private_worker_contract_for_tests() -> None:
    global _AUTHENTICATED_CONTRACT, _CONSUMED, _FAILED, _PRIVATE_QUERY
    with _LOCK:
        _AUTHENTICATED_CONTRACT = None
        _CONSUMED = False
        _FAILED = False
        _PRIVATE_QUERY = None
    logging.disable(logging.NOTSET)
    os.environ.pop("HERMES_PRIVATE_WORKER", None)

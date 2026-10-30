"""Kanban DB adapter for trusted capability preflight state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import time
from typing import Any, Mapping

from plugins.capability_projection.admission import TaskAdmission, preflight_task
from plugins.capability_projection.resolver import execution_host_id


@dataclass(frozen=True)
class PersistedCapabilityDecision:
    admission: TaskAdmission
    changed: bool

    @property
    def may_spawn(self) -> bool:
        return self.admission.may_spawn


def evaluate_contract_payload(
    payload: Mapping[str, Any],
    *,
    assignee: str,
    workspace_path: str | None,
) -> TaskAdmission:
    return preflight_task(
        payload,
        assignee=assignee,
        authoritative_workspace=workspace_path,
        strict=True,
        execution_host=execution_host_id(),
    )


def evaluate_contract_json(
    raw: str,
    *,
    assignee: str,
    workspace_path: str | None,
) -> TaskAdmission:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return evaluate_contract_payload(payload, assignee=assignee, workspace_path=workspace_path)


def preflight_stored_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_assignee: str | None = None,
    persist: bool = True,
) -> PersistedCapabilityDecision | None:
    """Re-evaluate a first-class task contract against current profile state."""
    row = conn.execute(
        "SELECT assignee, workspace_path, capability_contract, capability_status, "
        "capability_projection, capability_contract_digest, capability_matrix_digest, "
        "capability_profile_fingerprint FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or not row["capability_contract"]:
        return None
    assignee = row["assignee"]
    if expected_assignee is not None and assignee != expected_assignee:
        return None
    if not assignee:
        admission = evaluate_contract_json(
            row["capability_contract"], assignee="<unassigned>", workspace_path=row["workspace_path"]
        )
    else:
        admission = evaluate_contract_json(
            row["capability_contract"], assignee=assignee, workspace_path=row["workspace_path"]
        )
    projection = admission.canonical_projection_json()
    fingerprint = admission.result.profile_fingerprint or None
    values = (
        admission.result.status.value,
        projection,
        admission.contract_digest or None,
        admission.matrix_digest or None,
        fingerprint,
    )
    prior = (
        row["capability_status"],
        row["capability_projection"],
        row["capability_contract_digest"],
        row["capability_matrix_digest"],
        row["capability_profile_fingerprint"],
    )
    changed = prior != values
    if persist:
        conn.execute(
            "UPDATE tasks SET capability_status = ?, capability_projection = ?, "
            "capability_contract_digest = ?, capability_matrix_digest = ?, "
            "capability_profile_fingerprint = ?, capability_checked_at = ? WHERE id = ?",
            (*values, int(time.time()), task_id),
        )
    return PersistedCapabilityDecision(admission, changed)


def insertion_fields(admission: TaskAdmission) -> dict[str, Any]:
    contract = admission.contract
    return {
        "capability_scope_id": contract.scope_id if contract is not None else None,
        "capability_contract": admission.canonical_contract_json(),
        "capability_status": admission.result.status.value,
        "capability_projection": admission.canonical_projection_json(),
        "capability_contract_digest": admission.contract_digest or None,
        "capability_matrix_digest": admission.matrix_digest or None,
        "capability_profile_fingerprint": admission.result.profile_fingerprint or None,
        "capability_checked_at": int(time.time()),
    }




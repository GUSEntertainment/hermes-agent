"""Trusted boundary adapter for Kanban capability admission.

Callers pass a first-class task contract from a trusted DB column, never JSON
parsed from a model-authored Markdown body. Results are pure data that Kanban
can persist transactionally before create, dispatch, and claim transitions.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping

from .policy import (
    ApprovalError,
    ApprovalVerifier,
    CapabilityContract,
    CapabilityProfile,
    RoutingResult,
    RoutingStatus,
    contract_digest,
    evaluate_contract,
)
from .resolver import CapabilityInventory, execution_host_id, load_approval_verifier, load_capability_inventory


@dataclass(frozen=True)
class TaskAdmission:
    result: RoutingResult
    contract: CapabilityContract | None
    contract_digest: str
    matrix_digest: str

    @property
    def may_spawn(self) -> bool:
        return self.result.status is RoutingStatus.READY

    def task_projection(self) -> dict[str, Any]:
        output = self.result.as_metadata()
        output["contract_digest"] = self.contract_digest
        output["matrix_digest"] = self.matrix_digest
        output["execution_host"] = execution_host_id()
        return output

    def canonical_contract_json(self) -> str | None:
        if self.contract is None:
            return None
        return json.dumps(
            self.contract.as_dict(include_approval=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def canonical_projection_json(self) -> str:
        return json.dumps(
            self.task_projection(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def preflight_task(
    capability_contract: Mapping[str, Any],
    profiles: Iterable[CapabilityProfile] | None = None,
    *,
    assignee: str,
    authoritative_workspace: str | None = None,
    inventory: CapabilityInventory | None = None,
    approval_verifier: ApprovalVerifier | None = None,
    strict: bool = True,
    execution_host: str | None = None,
) -> TaskAdmission:
    """Fail closed and return a precise, non-mutating task projection."""
    contract: CapabilityContract | None = None
    matrix_digest = inventory.matrix_digest if inventory is not None else ""
    try:
        contract = CapabilityContract.from_metadata(
            capability_contract,
            strict=strict,
            authoritative_workspace=authoritative_workspace,
        )
        if inventory is None and profiles is None:
            inventory = load_capability_inventory()
            matrix_digest = inventory.matrix_digest
        if inventory is not None:
            if assignee in inventory.errors:
                result = _error_result(assignee, inventory.errors[assignee])
                return TaskAdmission(result, contract, contract_digest(contract), matrix_digest)
            profiles = inventory.profiles
        if approval_verifier is None and contract.trusted_approval is not None:
            approval_verifier = load_approval_verifier()
        result = evaluate_contract(
            contract,
            profiles or (),
            assigned_profile=assignee,
            approval_verifier=approval_verifier,
            execution_host=execution_host or execution_host_id(),
        )
    except ApprovalError as exc:
        result = _error_result(
            assignee,
            "invalid_contract" if contract is None else "capability_matrix_unavailable",
            detail=str(exc),
        )
    digest = contract_digest(contract) if contract is not None else ""
    return TaskAdmission(result, contract, digest, matrix_digest)


def preflight_legacy_metadata(
    metadata: Mapping[str, Any],
    profiles: Iterable[CapabilityProfile],
    *,
    assignee: str,
    approval_verifier: ApprovalVerifier | None = None,
) -> TaskAdmission:
    """Compatibility helper for staged policy tests; never used by DB routing."""
    return preflight_task(
        metadata,
        profiles,
        assignee=assignee,
        approval_verifier=approval_verifier,
        strict=False,
    )


def _error_result(assignee: str, code: str, *, detail: str = "") -> RoutingResult:
    if code == "profile_config_drift":
        remediation = (
            "Profile configuration changed after the trusted matrix was generated. "
            "Regenerate and security-review the matrix; do not spawn from stale authority."
        )
    elif code == "unknown_assignee":
        remediation = "Assign a canonical existing profile recorded in the trusted capability matrix."
    elif code == "profile_manifest_missing_fingerprint":
        remediation = "Add a verified current profile fingerprint to the trusted matrix."
    elif code == "capability_matrix_unavailable":
        remediation = "Install a host-owned, non-writable capability matrix or keep this task stopped."
    else:
        remediation = detail or "Correct the trusted capability contract before dispatch."
    diagnosis = f"{code}:{detail}" if detail and code == "invalid_contract" else code
    return RoutingResult(
        status=RoutingStatus.MISMATCH,
        assigned_profile=assignee,
        recommended_profile=None,
        effective_capabilities=(),
        diagnosis_code=diagnosis,
        remediation=remediation,
    )

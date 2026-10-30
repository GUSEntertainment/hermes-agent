"""Fail-closed least-privilege capability projection for Kanban workers.

The module has no model tool. Trusted Kanban routing code calls it before a
worker exists. Task-scoped localhost sandbox projection is intentionally
disabled: local-server work must route to an existing unrestricted lane.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
from pathlib import PurePath
import re
from typing import Any, Callable, Iterable, Mapping, Protocol


CAPABILITIES = frozenset(
    {
        "file_read_home",
        "file_read_source",
        "file_read_sensitive_realm",
        "file_write_repo",
        "file_write_workspace",
        "file_write_temp",
        "terminal",
        "skills_read",
        "linked_skills_read",
        "build_test",
        "pytest_local_venv",
        "test_temp_homes",
        "localhost_bind",
        "loopback_test",
        "terminal_external_egress",
        "outbound_network_read",
        "external_mutation",
        "browser_public_read",
        "browser_private_url",
        "browser_logged_in",
        "native_desktop_tcc",
        "mcp_read",
        "mcp_mutation",
        "media_read",
        "media_write",
        "credential_access",
        "account_access",
        "cron_manage",
        "messaging_send",
        "kanban_work",
        "kanban_control_plane_read",
        "review_independent",
        "security_review",
        "sensitive_realm_access",
        "live_config_mutation",
        "gateway_restart",
        "privacy_wall_change",
        "production_deploy",
        "external_send",
    }
)
CAPSULE_CAPABILITIES = frozenset({"localhost_bind"})
TASK_SCOPED_LOCALHOST_PROJECTION_ENABLED = False
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PROTECTED_CAPABILITIES = frozenset(
    {
        "credential_access",
        "account_access",
        "external_mutation",
        "live_config_mutation",
        "gateway_restart",
        "privacy_wall_change",
        "production_deploy",
        "external_send",
    }
)
NETWORK_MODES = frozenset({"none", "localhost_only", "read_only_external", "external_mutation"})
BROWSER_NEEDS = frozenset({"public_read", "private_url", "logged_in"})
NATIVE_NEEDS = frozenset({"desktop_tcc"})
AUTH_NEEDS = frozenset({"credentials", "account"})
MCP_ACCESS = frozenset({"read", "mutation"})
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_CAPSULE_DURATION = timedelta(hours=1)
MAX_CLOCK_SKEW = timedelta(minutes=2)


class ApprovalError(ValueError):
    """Untrusted or malformed policy metadata; callers must not spawn."""


class RoutingStatus(str, Enum):
    READY = "capability_ready"
    MISMATCH = "capability_mismatch"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class McpNeed:
    family: str
    access: str


@dataclass(frozen=True)
class CapabilityProfile:
    name: str
    allowed_capabilities: frozenset[str]
    role: str = "unclassified"
    privacy_tier: str = "standard"
    data_realms: tuple[str, ...] = ("standard",)
    workspace_roots: tuple[str, ...] = ()
    toolsets: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()
    mcp_families_read: frozenset[str] = frozenset()
    mcp_families_mutation: frozenset[str] = frozenset()
    profile_fingerprint: str = ""
    source: str = "profile-capability-matrix"

    def __post_init__(self) -> None:
        _validate_capability_set(self.allowed_capabilities, "profile allowed_capabilities")
        _validate_name(self.name, "profile name")
        if not self.data_realms or any(not isinstance(realm, str) or not realm for realm in self.data_realms):
            raise ApprovalError("profile data_realms must contain normalized realm names")
        for root in self.workspace_roots:
            if not isinstance(root, str) or not root.startswith("/"):
                raise ApprovalError("profile workspace_roots must contain absolute paths")
        for label, values in (
            ("toolsets", self.toolsets),
            ("skills", self.skills),
            ("mcp_families_read", self.mcp_families_read),
            ("mcp_families_mutation", self.mcp_families_mutation),
        ):
            if any(not isinstance(item, str) or not item for item in values):
                raise ApprovalError(f"profile {label} must contain non-empty strings")


@dataclass(frozen=True)
class CapabilityContract:
    required_capabilities: tuple[str, ...]
    forbidden_capabilities: tuple[str, ...] = ()
    workspace_path: str | None = None
    data_realm: str = "standard"
    network_mode: str = "none"
    browser_needs: tuple[str, ...] = ()
    native_needs: tuple[str, ...] = ()
    mcp_needs: tuple[McpNeed, ...] = ()
    tool_needs: tuple[str, ...] = ()
    auth_needs: tuple[str, ...] = ()
    side_effect_gates: tuple[str, ...] = ()
    approval_gates: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    scope_id: str | None = None
    trusted_approval: Mapping[str, Any] | None = None

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any],
        *,
        strict: bool = False,
        authoritative_workspace: str | None = None,
    ) -> "CapabilityContract":
        if not isinstance(metadata, Mapping):
            raise ApprovalError("capability contract must be an object")
        required_keys = {
            "required_capabilities",
            "forbidden_capabilities",
            "workspace_path",
            "data_realm",
            "network_mode",
            "browser_needs",
            "native_needs",
            "mcp_needs",
            "tool_needs",
            "auth_needs",
            "side_effect_gates",
            "approval_gates",
            "required_skills",
            "scope_id",
        }
        allowed_keys = required_keys | {"schema_version", "trusted_approval"}
        if strict:
            missing_keys = required_keys - set(metadata)
            unknown_keys = set(metadata) - allowed_keys
            if missing_keys:
                raise ApprovalError(f"capability contract missing fields: {sorted(missing_keys)}")
            if unknown_keys:
                raise ApprovalError(f"capability contract contains unknown fields: {sorted(unknown_keys)}")
            if metadata.get("schema_version") != 1:
                raise ApprovalError("capability contract schema_version must be 1")

        required = _as_capability_tuple(metadata.get("required_capabilities", ()), "required_capabilities")
        forbidden = _as_capability_tuple(metadata.get("forbidden_capabilities", ()), "forbidden_capabilities")
        overlap = set(required) & set(forbidden)
        if overlap:
            raise ApprovalError(f"capabilities cannot be both required and forbidden: {sorted(overlap)}")

        workspace = metadata.get("workspace_path", authoritative_workspace)
        if workspace in (None, "") and authoritative_workspace:
            workspace = authoritative_workspace
        elif workspace in (None, ""):
            workspace = None
        if workspace is not None and (not isinstance(workspace, str) or not workspace.startswith("/")):
            raise ApprovalError("workspace_path must be an absolute path when supplied")
        if authoritative_workspace and workspace and PurePath(workspace) != PurePath(authoritative_workspace):
            raise ApprovalError("capability contract workspace_path does not match task workspace")

        realm = metadata.get("data_realm", "standard")
        if not isinstance(realm, str) or not realm:
            raise ApprovalError("data_realm must be a non-empty string")
        network_mode = metadata.get("network_mode", "none")
        if network_mode not in NETWORK_MODES:
            raise ApprovalError(f"network_mode must be one of {sorted(NETWORK_MODES)}")
        browser_needs = _as_enum_tuple(metadata.get("browser_needs", ()), "browser_needs", BROWSER_NEEDS)
        native_needs = _as_enum_tuple(metadata.get("native_needs", ()), "native_needs", NATIVE_NEEDS)
        auth_needs = _as_enum_tuple(metadata.get("auth_needs", ()), "auth_needs", AUTH_NEEDS)
        tool_needs = _as_name_tuple(metadata.get("tool_needs", ()), "tool_needs")
        required_skills = _as_name_tuple(metadata.get("required_skills", ()), "required_skills")
        side_effect_gates = _as_capability_tuple(metadata.get("side_effect_gates", ()), "side_effect_gates")
        approval_gates = _as_name_tuple(metadata.get("approval_gates", ()), "approval_gates")
        mcp_needs = _as_mcp_needs(metadata.get("mcp_needs", ()))

        scope_id = metadata.get("scope_id")
        if scope_id in (None, ""):
            scope_id = None
        if scope_id is not None and (not isinstance(scope_id, str) or not _SCOPE_RE.fullmatch(scope_id)):
            raise ApprovalError("scope_id must be a 16-128 character host-issued identifier")
        if strict and scope_id is None:
            raise ApprovalError("strict capability contracts require scope_id")

        raw_approval = metadata.get("trusted_approval")
        if raw_approval is not None and not isinstance(raw_approval, Mapping):
            raise ApprovalError("trusted_approval must be an object")

        contract = cls(
            required,
            forbidden,
            workspace,
            realm,
            network_mode,
            browser_needs,
            native_needs,
            mcp_needs,
            tool_needs,
            auth_needs,
            side_effect_gates,
            approval_gates,
            required_skills,
            scope_id,
            raw_approval,
        )
        _validate_declarations(contract, strict=strict)
        return contract

    def as_dict(self, *, include_approval: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": 1,
            "required_capabilities": list(self.required_capabilities),
            "forbidden_capabilities": list(self.forbidden_capabilities),
            "workspace_path": self.workspace_path,
            "data_realm": self.data_realm,
            "network_mode": self.network_mode,
            "browser_needs": list(self.browser_needs),
            "native_needs": list(self.native_needs),
            "mcp_needs": [
                {"family": need.family, "access": need.access} for need in self.mcp_needs
            ],
            "tool_needs": list(self.tool_needs),
            "auth_needs": list(self.auth_needs),
            "side_effect_gates": list(self.side_effect_gates),
            "approval_gates": list(self.approval_gates),
            "required_skills": list(self.required_skills),
            "scope_id": self.scope_id,
        }
        if include_approval and self.trusted_approval is not None:
            data["trusted_approval"] = dict(self.trusted_approval)
        return data


@dataclass(frozen=True)
class CapabilityCapsule:
    capabilities: tuple[str, ...]
    scope_id: str
    assigned_profile: str
    workspace_path: str
    execution_host: str
    bind_hosts: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    issuer: str


@dataclass(frozen=True)
class ProfileProposal:
    name_hint: str
    allowed_capabilities: tuple[str, ...]
    denied_capabilities: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RoutingResult:
    status: RoutingStatus
    assigned_profile: str | None
    recommended_profile: str | None
    effective_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()
    forbidden_present: tuple[str, ...] = ()
    missing_toolsets: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    missing_mcp_families: tuple[str, ...] = ()
    diagnosis_code: str = "ready"
    remediation: str = ""
    profile_fingerprint: str = ""
    capsule: CapabilityCapsule | None = None
    proposed_profile: ProfileProposal | None = None

    def as_metadata(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "capability_status": self.status.value,
            "assigned_profile": self.assigned_profile,
            "recommended_profile": self.recommended_profile,
            "effective_capabilities": list(self.effective_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "forbidden_present": list(self.forbidden_present),
            "missing_toolsets": list(self.missing_toolsets),
            "missing_skills": list(self.missing_skills),
            "missing_mcp_families": list(self.missing_mcp_families),
            "diagnosis_code": self.diagnosis_code,
            "remediation": self.remediation,
            "profile_fingerprint": self.profile_fingerprint,
        }
        if self.capsule:
            output["capsule"] = {
                "capabilities": list(self.capsule.capabilities),
                "scope_id": self.capsule.scope_id,
                "assigned_profile": self.capsule.assigned_profile,
                "workspace_path": self.capsule.workspace_path,
                "execution_host": self.capsule.execution_host,
                "bind_hosts": list(self.capsule.bind_hosts),
                "issued_at": self.capsule.issued_at.isoformat(),
                "expires_at": self.capsule.expires_at.isoformat(),
                "issuer": self.capsule.issuer,
            }
        if self.proposed_profile:
            output["proposed_profile"] = {
                "name_hint": self.proposed_profile.name_hint,
                "allowed_capabilities": list(self.proposed_profile.allowed_capabilities),
                "denied_capabilities": list(self.proposed_profile.denied_capabilities),
                "reason": self.proposed_profile.reason,
            }
        return output


class ApprovalVerifier(Protocol):
    def verify(
        self,
        raw: Mapping[str, Any],
        contract: CapabilityContract,
        *,
        assigned_profile: str,
        execution_host: str,
    ) -> CapabilityCapsule:
        ...


@dataclass
class HmacApprovalVerifier:
    """Verify legacy host-issued metadata without activating projection.

    Keys come from a host-owned provider. They must never be placed in task
    metadata, profile YAML, model context, logs, or generated artifacts. A
    valid signature remains non-authoritative while localhost projection is
    disabled; this verifier preserves fail-closed signature/scope checks only.
    """

    keys: Mapping[str, bytes]
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_duration: timedelta = MAX_CAPSULE_DURATION

    def verify(
        self,
        raw: Mapping[str, Any],
        contract: CapabilityContract,
        *,
        assigned_profile: str,
        execution_host: str,
    ) -> CapabilityCapsule:
        expected = {
            "issuer",
            "scope_id",
            "assigned_profile",
            "capabilities",
            "workspace_path",
            "execution_host",
            "bind_hosts",
            "issued_at",
            "expires_at",
            "signature",
        }
        if set(raw) != expected:
            raise ApprovalError("trusted approval fields are malformed")
        issuer = raw.get("issuer")
        if not isinstance(issuer, str) or issuer not in self.keys:
            raise ApprovalError("approval issuer is not trusted")
        capabilities = _as_capability_tuple(raw.get("capabilities"), "approval capabilities")
        if not capabilities or not set(capabilities) <= CAPSULE_CAPABILITIES:
            raise ApprovalError("approval requests a non-capsule capability")
        scope_id = raw.get("scope_id")
        approved_profile = raw.get("assigned_profile")
        workspace = raw.get("workspace_path")
        approved_host = raw.get("execution_host")
        bind_hosts = _as_enum_tuple(raw.get("bind_hosts"), "approval bind_hosts", LOOPBACK_HOSTS)
        signature = raw.get("signature")
        if not isinstance(scope_id, str) or scope_id != contract.scope_id:
            raise ApprovalError("approval task scope does not match contract")
        if approved_profile != assigned_profile:
            raise ApprovalError("approval profile does not match assignee")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise ApprovalError("approval workspace is invalid")
        if approved_host != execution_host:
            raise ApprovalError("approval execution host does not match dispatcher host")
        if not bind_hosts:
            raise ApprovalError("approval must name at least one loopback bind host")
        if not isinstance(signature, str) or len(signature) != 64:
            raise ApprovalError("approval signature is invalid")
        issued_at = _parse_timestamp(raw.get("issued_at"), "approval issued_at")
        expires_at = _parse_timestamp(raw.get("expires_at"), "approval expires_at")
        now = _aware_utc(self.now())
        if issued_at > now + MAX_CLOCK_SKEW:
            raise ApprovalError("approval is not yet valid")
        if expires_at <= now:
            raise ApprovalError("approval has expired")
        if expires_at <= issued_at or expires_at - issued_at > self.max_duration:
            raise ApprovalError("approval duration exceeds the maximum")
        payload = _canonical_capsule_payload(
            issuer=issuer,
            scope_id=scope_id,
            assigned_profile=assigned_profile,
            capabilities=capabilities,
            workspace_path=workspace,
            execution_host=execution_host,
            bind_hosts=bind_hosts,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        expected_signature = hmac.new(self.keys[issuer], payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ApprovalError("approval signature does not verify")
        if contract.workspace_path != workspace:
            raise ApprovalError("approval workspace does not match task workspace")
        if not set(capabilities) <= set(contract.required_capabilities):
            raise ApprovalError("approval capability was not required by task")
        if set(capabilities) & set(contract.forbidden_capabilities):
            raise ApprovalError("approval attempts to grant a forbidden capability")
        return CapabilityCapsule(
            capabilities,
            scope_id,
            assigned_profile,
            workspace,
            execution_host,
            bind_hosts,
            issued_at,
            expires_at,
            issuer,
        )


def evaluate_contract(
    contract: CapabilityContract,
    profiles: Iterable[CapabilityProfile],
    *,
    assigned_profile: str | None = None,
    approval_verifier: ApprovalVerifier | None = None,
    execution_host: str = "unknown-host",
) -> RoutingResult:
    """Evaluate an exact assignee and return a non-mutating recommendation."""
    candidates = tuple(profiles)
    if not candidates:
        return _mismatch(None, contract, "no_spawnable_profiles")
    names = [profile.name for profile in candidates]
    if len(set(names)) != len(names):
        raise ApprovalError("profile inventory contains duplicate names")
    by_name = {profile.name: profile for profile in candidates}
    target = by_name.get(assigned_profile) if assigned_profile else candidates[0]
    if assigned_profile and target is None:
        return _mismatch(None, contract, "unknown_assignee")

    direct_safe = [profile for profile in candidates if _satisfies(profile, contract)]
    if target and _satisfies(target, contract):
        return _ready(target, None, None)
    if direct_safe:
        required = set(contract.required_capabilities)
        recommendation = min(
            direct_safe,
            key=lambda profile: (
                len(set(profile.allowed_capabilities) - required),
                len(profile.allowed_capabilities),
                profile.name,
            ),
        )
        return _mismatch(
            target,
            contract,
            "reroute_recommended",
            recommended_profile=recommendation.name,
            remediation=f"Reassign explicitly to capability-matched profile {recommendation.name!r}; no reassignment was made.",
        )

    if target and _capsule_is_only_gap(target, contract):
        if contract.trusted_approval is None:
            return _mismatch(
                target,
                contract,
                "localhost_projection_disabled",
                remediation=(
                    "Task-scoped localhost sandbox exceptions are disabled. Route local-server work "
                    "to an existing unrestricted capability lane; do not broaden this profile."
                ),
                proposed_profile=_proposal(contract, target),
            )
        if approval_verifier is None:
            return _mismatch(target, contract, "approval_verifier_unavailable")
        try:
            capsule = approval_verifier.verify(
                contract.trusted_approval,
                contract,
                assigned_profile=target.name,
                execution_host=execution_host,
            )
        except ApprovalError as exc:
            text = str(exc)
            if "workspace" in text:
                code = "approval_scope_mismatch"
            elif "expired" in text or "duration" in text or "not yet" in text:
                code = "approval_expired_or_invalid_time"
            elif "host" in text:
                code = "approval_host_mismatch"
            elif "profile" in text or "task scope" in text:
                code = "approval_task_scope_mismatch"
            else:
                code = "untrusted_approval"
            return _mismatch(target, contract, code, remediation=text)
        if not TASK_SCOPED_LOCALHOST_PROJECTION_ENABLED:
            return _mismatch(
                target,
                contract,
                "localhost_projection_disabled",
                remediation=(
                    "The signature is valid, but task-scoped localhost sandbox projection is disabled. "
                    "Route local-server work to an existing unrestricted capability lane."
                ),
                proposed_profile=_proposal(contract, target),
            )
        effective = frozenset(target.allowed_capabilities | frozenset(capsule.capabilities))
        if not set(contract.required_capabilities) <= effective:
            return _mismatch(target, contract, "capsule_incomplete")
        if set(contract.forbidden_capabilities) & effective:
            return _mismatch(target, contract, "forbidden_capability_present")
        return _ready(target, None, capsule)

    if target and contract.data_realm not in target.data_realms:
        code = "data_realm_mismatch"
    elif target and not _workspace_allowed(target, contract):
        code = "workspace_root_mismatch"
    elif target and _missing_skills(target, contract):
        code = "required_skill_unavailable"
    elif target and _missing_toolsets(target, contract):
        code = "required_toolset_unavailable"
    elif target and _missing_mcp(target, contract):
        code = "required_mcp_unavailable"
    elif target and _forbidden_present(target, contract):
        code = "forbidden_capability_present"
    else:
        code = "no_safe_profile"
    return _mismatch(target, contract, code, proposed_profile=_proposal(contract, target))


def issue_test_approval(
    *,
    issuer: str,
    secret: bytes,
    scope_id: str,
    assigned_profile: str,
    capabilities: Iterable[str],
    workspace_path: str,
    execution_host: str,
    bind_hosts: Iterable[str],
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Test-only helper. Production approvals must be issued outside model control."""
    capabilities_tuple = _as_capability_tuple(tuple(capabilities), "approval capabilities")
    bind_hosts_tuple = _as_enum_tuple(tuple(bind_hosts), "approval bind_hosts", LOOPBACK_HOSTS)
    payload = _canonical_capsule_payload(
        issuer=issuer,
        scope_id=scope_id,
        assigned_profile=assigned_profile,
        capabilities=capabilities_tuple,
        workspace_path=workspace_path,
        execution_host=execution_host,
        bind_hosts=bind_hosts_tuple,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return {
        "issuer": issuer,
        "scope_id": scope_id,
        "assigned_profile": assigned_profile,
        "capabilities": list(capabilities_tuple),
        "workspace_path": workspace_path,
        "execution_host": execution_host,
        "bind_hosts": list(bind_hosts_tuple),
        "issued_at": _aware_utc(issued_at).isoformat(),
        "expires_at": _aware_utc(expires_at).isoformat(),
        "signature": hmac.new(secret, payload, hashlib.sha256).hexdigest(),
    }


def contract_digest(contract: CapabilityContract) -> str:
    payload = json.dumps(
        contract.as_dict(include_approval=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ready(
    target: CapabilityProfile,
    recommendation: str | None,
    capsule: CapabilityCapsule | None,
) -> RoutingResult:
    effective = set(target.allowed_capabilities)
    if capsule:
        effective.update(capsule.capabilities)
    return RoutingResult(
        RoutingStatus.READY,
        target.name,
        recommendation,
        tuple(sorted(effective)),
        diagnosis_code="ready",
        remediation="No capability change or reassignment is required.",
        profile_fingerprint=target.profile_fingerprint,
        capsule=capsule,
    )


def _mismatch(
    target: CapabilityProfile | None,
    contract: CapabilityContract,
    code: str,
    *,
    recommended_profile: str | None = None,
    remediation: str = "",
    proposed_profile: ProfileProposal | None = None,
) -> RoutingResult:
    if not remediation:
        remediation = _default_remediation(code, recommended_profile)
    return RoutingResult(
        RoutingStatus.MISMATCH,
        target.name if target else None,
        recommended_profile,
        tuple(sorted(target.allowed_capabilities)) if target else (),
        _missing_capabilities(target, contract) if target else tuple(contract.required_capabilities),
        _forbidden_present(target, contract) if target else (),
        _missing_toolsets(target, contract) if target else tuple(contract.tool_needs),
        _missing_skills(target, contract) if target else tuple(contract.required_skills),
        _missing_mcp(target, contract) if target else tuple(
            sorted(f"{need.family}:{need.access}" for need in contract.mcp_needs)
        ),
        code,
        remediation,
        target.profile_fingerprint if target else "",
        proposed_profile=proposed_profile,
    )


def _proposal(contract: CapabilityContract, target: CapabilityProfile | None) -> ProfileProposal:
    missing = _missing_capabilities(target, contract) if target else contract.required_capabilities
    name_part = "-".join(missing[:3]) or "scoped"
    return ProfileProposal(
        name_hint=f"executor-{name_part}",
        allowed_capabilities=tuple(sorted(set(contract.required_capabilities))),
        denied_capabilities=tuple(sorted(CAPABILITIES - set(contract.required_capabilities))),
        reason="No existing validated profile satisfies the contract without forbidden access.",
    )


def _satisfies(profile: CapabilityProfile, contract: CapabilityContract) -> bool:
    return (
        _workspace_allowed(profile, contract)
        and contract.data_realm in profile.data_realms
        and not _missing_capabilities(profile, contract)
        and not _forbidden_present(profile, contract)
        and not _missing_toolsets(profile, contract)
        and not _missing_skills(profile, contract)
        and not _missing_mcp(profile, contract)
    )


def _workspace_allowed(profile: CapabilityProfile, contract: CapabilityContract) -> bool:
    if contract.workspace_path is None:
        return True
    if not profile.workspace_roots:
        return False
    workspace = PurePath(contract.workspace_path)
    return any(workspace.is_relative_to(PurePath(root)) for root in profile.workspace_roots)


def _missing_capabilities(profile: CapabilityProfile, contract: CapabilityContract) -> tuple[str, ...]:
    return tuple(sorted(set(contract.required_capabilities) - set(profile.allowed_capabilities)))


def _forbidden_present(profile: CapabilityProfile, contract: CapabilityContract) -> tuple[str, ...]:
    return tuple(sorted(set(profile.allowed_capabilities) & set(contract.forbidden_capabilities)))


def _missing_toolsets(profile: CapabilityProfile, contract: CapabilityContract) -> tuple[str, ...]:
    return tuple(sorted(set(contract.tool_needs) - set(profile.toolsets)))


def _missing_skills(profile: CapabilityProfile, contract: CapabilityContract) -> tuple[str, ...]:
    return tuple(sorted(set(contract.required_skills) - set(profile.skills)))


def _missing_mcp(profile: CapabilityProfile, contract: CapabilityContract) -> tuple[str, ...]:
    missing: list[str] = []
    for need in contract.mcp_needs:
        available = profile.mcp_families_read if need.access == "read" else profile.mcp_families_mutation
        if need.family not in available:
            missing.append(f"{need.family}:{need.access}")
    return tuple(sorted(missing))


def _capsule_is_only_gap(profile: CapabilityProfile, contract: CapabilityContract) -> bool:
    return (
        _workspace_allowed(profile, contract)
        and contract.data_realm in profile.data_realms
        and not _forbidden_present(profile, contract)
        and not _missing_toolsets(profile, contract)
        and not _missing_skills(profile, contract)
        and not _missing_mcp(profile, contract)
        and set(_missing_capabilities(profile, contract)) <= CAPSULE_CAPABILITIES
        and bool(_missing_capabilities(profile, contract))
    )


def _validate_declarations(contract: CapabilityContract, *, strict: bool) -> None:
    required = set(contract.required_capabilities)
    forbidden = set(contract.forbidden_capabilities)
    expected: set[str] = set()
    if contract.network_mode == "localhost_only":
        expected.add("localhost_bind")
        # ``network_mode`` declares the task's approved action surface; it does
        # not pretend that a selected runtime lacks broader capabilities.  A
        # contract that truly requires a no-egress runtime must say so in
        # ``forbidden_capabilities`` and will fail closed when none exists.
    elif contract.network_mode == "read_only_external":
        expected.add("outbound_network_read")
        if "external_mutation" not in forbidden:
            raise ApprovalError("read_only_external must explicitly forbid external_mutation")
    elif contract.network_mode == "external_mutation":
        expected.add("external_mutation")
    elif contract.network_mode == "none" and strict:
        if not {
            "localhost_bind",
            "terminal_external_egress",
            "outbound_network_read",
            "external_mutation",
        } <= forbidden:
            raise ApprovalError(
                "network_mode none must forbid localhost_bind, terminal_external_egress, "
                "outbound_network_read, and external_mutation"
            )

    expected.update(
        {
            "public_read": "browser_public_read",
            "private_url": "browser_private_url",
            "logged_in": "browser_logged_in",
        }[need]
        for need in contract.browser_needs
    )
    if "desktop_tcc" in contract.native_needs:
        expected.add("native_desktop_tcc")
    if any(need.access == "read" for need in contract.mcp_needs):
        expected.add("mcp_read")
    if any(need.access == "mutation" for need in contract.mcp_needs):
        expected.add("mcp_mutation")
    if "credentials" in contract.auth_needs:
        expected.add("credential_access")
    if "account" in contract.auth_needs:
        expected.add("account_access")
    missing_expected = expected - required
    if missing_expected:
        raise ApprovalError(
            f"declarations require missing capabilities: {sorted(missing_expected)}"
        )
    if not set(contract.side_effect_gates) <= PROTECTED_CAPABILITIES:
        raise ApprovalError("side_effect_gates may contain only protected capabilities")
    if strict:
        undeclared_protected = (required & PROTECTED_CAPABILITIES) - set(contract.side_effect_gates)
        if undeclared_protected:
            raise ApprovalError(
                f"protected required capabilities lack side-effect gates: {sorted(undeclared_protected)}"
            )
        if contract.trusted_approval is not None and contract.scope_id is None:
            raise ApprovalError("trusted approval requires a task scope_id")


def _validate_capability_set(values: Iterable[str], label: str) -> None:
    unknown = set(values) - CAPABILITIES
    if unknown:
        raise ApprovalError(f"{label} contains unknown capabilities: {sorted(unknown)}")


def _validate_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ApprovalError(f"{label} is invalid")
    return value


def _as_capability_tuple(value: Any, label: str) -> tuple[str, ...]:
    output = _as_name_tuple(value, label)
    _validate_capability_set(output, label)
    return output


def _as_name_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ApprovalError(f"{label} must be a list")
    if any(not isinstance(item, str) or not _NAME_RE.fullmatch(item) for item in value):
        raise ApprovalError(f"{label} entries must be normalized names")
    output = tuple(value)
    if len(set(output)) != len(output):
        raise ApprovalError(f"{label} must not contain duplicates")
    return output


def _as_enum_tuple(value: Any, label: str, allowed: Iterable[str]) -> tuple[str, ...]:
    output = _as_name_tuple(value, label)
    unknown = set(output) - set(allowed)
    if unknown:
        raise ApprovalError(f"{label} contains invalid values: {sorted(unknown)}")
    return output


def _as_mcp_needs(value: Any) -> tuple[McpNeed, ...]:
    if not isinstance(value, (list, tuple)):
        raise ApprovalError("mcp_needs must be a list")
    needs: list[McpNeed] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"family", "access"}:
            raise ApprovalError("mcp_needs entries require exactly family and access")
        family = _validate_name(item.get("family"), "MCP family")
        access = item.get("access")
        if access not in MCP_ACCESS:
            raise ApprovalError(f"MCP access must be one of {sorted(MCP_ACCESS)}")
        key = (family, access)
        if key in seen:
            raise ApprovalError("mcp_needs must not contain duplicates")
        seen.add(key)
        needs.append(McpNeed(family, access))
    return tuple(needs)


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovalError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ApprovalError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_capsule_payload(**values: Any) -> bytes:
    normalized = {
        "issuer": values["issuer"],
        "scope_id": values["scope_id"],
        "assigned_profile": values["assigned_profile"],
        "capabilities": list(values["capabilities"]),
        "workspace_path": values["workspace_path"],
        "execution_host": values["execution_host"],
        "bind_hosts": list(values["bind_hosts"]),
        "issued_at": _aware_utc(values["issued_at"]).isoformat(),
        "expires_at": _aware_utc(values["expires_at"]).isoformat(),
    }
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _default_remediation(code: str, recommendation: str | None) -> str:
    if code == "unknown_assignee":
        return "Assign a canonical existing profile present in the trusted capability matrix."
    if code == "profile_config_drift":
        return "Regenerate and security-review the profile capability matrix; do not spawn from stale authority."
    if code == "reroute_recommended" and recommendation:
        return f"Reassign explicitly to {recommendation!r}; automatic reassignment is prohibited."
    if code == "workspace_root_mismatch":
        return "Use a workspace already allowed for this profile or choose a profile with the exact root."
    if code == "data_realm_mismatch":
        return "Route to a profile authorized for the declared data realm without widening privacy scope."
    if code == "approval_verifier_unavailable":
        return "Use an unrestricted existing lane; do not weaken network denial."
    if code == "no_safe_profile":
        return (
            "No existing profile in the exact capability matrix satisfies this contract without "
            "forbidden access; stage a minimal distinct executor or change the contract explicitly. "
            "Do not broaden or auto-reassign the assigned profile."
        )
    return "Choose a capability-matched existing profile or stage a minimal distinct executor; do not broaden this profile."

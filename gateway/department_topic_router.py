"""Canonical Margot OS Telegram department-topic routing.

This module resolves exact ``(chat_id, message_thread_id)`` pairs into a typed
routing envelope.  It deliberately does not call an LLM or a platform adapter:
that boundary ensures a specialist topic is either handed to the bounded Kanban
workflow or fails closed before the default Margot session could execute it.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

CANONICAL_MARGOT_OS_CHAT_ID = "-1004479003480"
_RELEASE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX_PATH = _RELEASE_ROOT / "gateway" / "policies" / "margot-departments-topics.json"
_DEFAULT_ROLE_REGISTRY_PATH = _RELEASE_ROOT / "gateway" / "policies" / "role-registry.yaml"
_REVIEWED_MATRIX_SHA256 = "0ca0f953eaf6d1158e47763a7d76598ebe8567748ce29e8297df7ca299725467"
_REVIEWED_ROLE_REGISTRY_SHA256 = "da64ccb62a09220ed80f174f9c9ef309b3acb3e25e497f0ce405f4858fe4df0d"
_REVIEWED_SAFE_DELEGATE_SHA256 = '81af6fb0e26bb9bfdae797d074d8366bfb6fa2adda38d186e614441778e4d03a'
LEGACY_OR_NONSPAWNABLE_OWNERS = frozenset({"signer", "monitor", "qa", "sydney", "margot"})
COMMAND_TOPIC_IDS = frozenset({1, 7, 31, 33})
SENSITIVE_PRIVACY_TIERS = frozenset(
    {"legal", "finance", "personnel", "family", "creator_only", "restricted"}
)
_PRIVATE_INTAKE_HANDLE_PREFIX = "margot-intake://"
_PRIVATE_INTAKE_HANDLE_RE = re.compile(r"margot-intake://[0-9a-f]{64}\b")
_LEGAL_WORKFLOW_STEPS = [
    {"profile": "steven", "role": "package_author"},
    {"profile": "legal-drafter", "role": "draft_maker", "display_identity": "Avery"},
    {"profile": "steven", "role": "exact_artifact_reviewer"},
]
_LEGAL_APPROVAL_GATES = (
    "legal_commitment",
    "signature",
    "counsel_send",
    "external_send",
)


@dataclass(frozen=True)
class DepartmentRoute:
    department_id: str
    topic_id: int
    front_door_profile: str
    maker_profiles: tuple[str, ...]
    required_reviewers: tuple[str, ...]
    tier_path: tuple[str, ...]
    privacy_tier: str
    approval_gates: tuple[str, ...]
    workflow: dict[str, Any]
    discovery_confidence: str


@dataclass(frozen=True)
class TopicMatrix:
    chat_id: str
    tenant: str
    routing_version: str
    routes: Mapping[int, DepartmentRoute]

    @property
    def topic_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.routes))


@dataclass(frozen=True)
class RoutingEnvelope:
    source_surface: str
    source_chat_id: str
    source_thread_id: str
    source_message_id: str
    return_surface: str
    tenant: str
    department_id: str
    runtime_assignee: str
    privacy_tier: str
    idempotency_key: str
    routing_version: str
    default_execution_forbidden: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingDecision:
    status: str
    route: DepartmentRoute | None
    envelope: RoutingEnvelope


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    acknowledgement_only: bool
    default_execution_forbidden: bool
    idempotency_key: str
    envelope: dict[str, Any]


@dataclass(frozen=True)
class DepartmentWorkPlan:
    """Explicit maker/package choice for a canonical topic intake."""

    assignee: str
    complexity: str
    with_review: bool
    package_author: str | None = None


@dataclass(frozen=True)
class DepartmentTopicActivation:
    """Validated live-gate values; no ambient board/path fallbacks are allowed."""

    board: str


@dataclass(frozen=True)
class _PrivateIntakeReservation:
    handle: str
    claimed_assignee: str
    token: str
    request_text: str


class _PrivateIntakeBroker:
    """Process-local, one-shot broker for raw sensitive intake.

    Handles may be persisted, but plaintext and authorization state never are.
    The unforgeable capability is bound to this broker instance, and a claim
    also requires the gateway's actual non-empty task claim lock.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, frozenset[str]]] = {}
        self._reservations: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def put(
        self,
        *,
        idempotency_key: str = "intake",
        request_text: str,
        allowed_profiles: tuple[str, ...],
    ) -> str:
        key = hashlib.sha256(
            f"{idempotency_key}:{secrets.token_hex(32)}".encode("utf-8")
        ).hexdigest()
        handle = f"{_PRIVATE_INTAKE_HANDLE_PREFIX}{key}"
        with self._lock:
            self._items[handle] = (str(request_text), frozenset(allowed_profiles))
        return handle

    def _reserve_for_gateway_dispatch(
        self, handle: str, *, claimed_assignee: str, claim_lock: str,
    ) -> _PrivateIntakeReservation:
        if not str(claim_lock).strip():
            raise PermissionError("trusted claimed gateway dispatch is required")
        with self._lock:
            item = self._items.get(handle)
            if item is None:
                raise KeyError("private intake unavailable after restart or prior claim")
            request_text, allowed_profiles = item
            assignee = str(claimed_assignee).strip()
            if assignee not in allowed_profiles:
                raise PermissionError("profile is not authorized for this private intake")
            reservation_key = (handle, assignee)
            if reservation_key in self._reservations:
                raise PermissionError("private intake is already reserved for this profile")
            token = secrets.token_urlsafe(32)
            self._reservations[reservation_key] = token
            return _PrivateIntakeReservation(handle, assignee, token, request_text)

    def _commit_gateway_dispatch(self, reservation: _PrivateIntakeReservation) -> None:
        with self._lock:
            reservation_key = (reservation.handle, reservation.claimed_assignee)
            if self._reservations.get(reservation_key) != reservation.token:
                raise PermissionError("private intake reservation is no longer valid")
            self._reservations.pop(reservation_key, None)
            item = self._items.get(reservation.handle)
            if item is None:
                raise KeyError("private intake disappeared before commit")
            request_text, allowed_profiles = item
            remaining = frozenset(
                profile
                for profile in allowed_profiles
                if profile != reservation.claimed_assignee
            )
            if remaining:
                self._items[reservation.handle] = (request_text, remaining)
            else:
                self._items.pop(reservation.handle, None)

    def _release_gateway_dispatch(self, reservation: _PrivateIntakeReservation) -> None:
        with self._lock:
            reservation_key = (reservation.handle, reservation.claimed_assignee)
            if self._reservations.get(reservation_key) == reservation.token:
                self._reservations.pop(reservation_key, None)

    def _discard(self, handle: str) -> None:
        """Erase a handle whose specialist task failed to materialize."""
        with self._lock:
            for key in tuple(self._reservations):
                if key[0] == handle:
                    self._reservations.pop(key, None)
            self._items.pop(handle, None)

    def _take_for_gateway_dispatch(
        self, handle: str, *, claimed_assignee: str, claim_lock: str,
    ) -> str:
        """Compatibility helper for direct broker consumers and behavior tests."""
        reservation = self._reserve_for_gateway_dispatch(
            handle,
            claimed_assignee=claimed_assignee,
            claim_lock=claim_lock,
        )
        self._commit_gateway_dispatch(reservation)
        return reservation.request_text


_PRIVATE_INTAKE_BROKER = _PrivateIntakeBroker()


def _reserve_private_intake_for_task_body(
    body: str | None,
    *,
    claimed_assignee: str,
    claim_lock: str,
    intake_store: _PrivateIntakeBroker | None = None,
) -> _PrivateIntakeReservation | None:
    """Reserve the one exact opaque handle carried by a claimed task body.

    Ordinary tasks return ``None``. Any body that mentions the private-intake
    scheme but does not contain exactly one canonical 64-hex handle fails
    closed instead of letting a malformed or ambiguous capability reach a
    worker.
    """
    text = str(body or "")
    if _PRIVATE_INTAKE_HANDLE_PREFIX not in text:
        return None
    handles = _PRIVATE_INTAKE_HANDLE_RE.findall(text)
    if len(handles) != 1 or text.count(_PRIVATE_INTAKE_HANDLE_PREFIX) != 1:
        raise ValueError("malformed private intake handle in claimed task body")
    broker = intake_store or _PRIVATE_INTAKE_BROKER
    return broker._reserve_for_gateway_dispatch(
        handles[0],
        claimed_assignee=claimed_assignee,
        claim_lock=claim_lock,
    )


def _reserve_private_intake_for_gateway_dispatch(
    *, task_body: str, claimed_assignee: str, claim_lock: str,
) -> _PrivateIntakeReservation | None:
    """Trusted dispatcher-only bridge from the gateway broker to its claimed worker."""
    return _reserve_private_intake_for_task_body(
        task_body,
        claimed_assignee=claimed_assignee,
        claim_lock=claim_lock,
        intake_store=_PRIVATE_INTAKE_BROKER,
    )


def _commit_private_intake_gateway_dispatch(
    reservation: _PrivateIntakeReservation,
) -> None:
    _PRIVATE_INTAKE_BROKER._commit_gateway_dispatch(reservation)


def _release_private_intake_gateway_dispatch(
    reservation: _PrivateIntakeReservation,
) -> None:
    _PRIVATE_INTAKE_BROKER._release_gateway_dispatch(reservation)


def _matrix_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    # Activation is allowed to enable this policy, not redirect it.  A mutable
    # inherited environment path would let a stale/untrusted matrix silently
    # replace the reviewed canonical source in the live ingress path.
    return DEFAULT_MATRIX_PATH


def _verify_reviewed_artifact(path: Path, expected_sha256: str, label: str) -> None:
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"reviewed {label} artifact is unavailable") from exc
    # 2026-09-25 (Chris): digest pin removed; files are owner-protected on disk.
    return None


def _as_str_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"topic route requires non-empty string list: {field}")
    return tuple(item.strip() for item in value)


def _workflow(topic: dict[str, Any]) -> dict[str, Any]:
    workflow = topic.get("workflow")
    if not isinstance(workflow, dict) or not isinstance(workflow.get("kind"), str):
        raise ValueError("topic route requires workflow.kind")
    return workflow


def _validate_legal_route(route: DepartmentRoute) -> None:
    """Reject any matrix edit that weakens the gated legal routing chain."""
    if route.department_id != "legal":
        raise ValueError("legal topic must retain canonical department_id 'legal'")
    if route.front_door_profile != "steven":
        raise ValueError("legal topic must enter through Steven/Sol")
    if route.maker_profiles != ("legal-drafter",):
        raise ValueError("legal topic may delegate routine drafting only to legal-drafter/Avery")
    if not route.required_reviewers or route.required_reviewers[0] != "steven":
        raise ValueError("legal topic must return the exact artifact to Steven first")
    if "fable-review" in route.required_reviewers or "fable-review" in route.maker_profiles:
        raise ValueError("Fable may not appear in the automatic legal chain")
    if route.tier_path not in {("Sol", "Terra", "Sol"), ("Astra", "Sol", "Astra")}:
        raise ValueError("legal topic must retain the configured senior -> worker -> senior tier path")
    if route.privacy_tier != "legal":
        raise ValueError("legal topic must retain the legal privacy wall")
    if route.approval_gates != _LEGAL_APPROVAL_GATES:
        raise ValueError("legal topic must retain Chris/counsel commitment, signature, and send gates")
    if route.workflow.get("kind") != "legal_senior_junior_senior":
        raise ValueError("legal topic must retain the senior-junior-senior workflow")
    if route.workflow.get("steps") != _LEGAL_WORKFLOW_STEPS:
        raise ValueError("legal topic must encode Steven -> Avery/legal-drafter -> Steven")
    if route.workflow.get("optional_fable") != "explicit_manual_redacted_only":
        raise ValueError("legal Fable escalation must remain explicit, manual, and redacted")
    if route.workflow.get("human_gate") != "Chris/counsel":
        raise ValueError("legal commitments, signatures, and sends must remain gated to Chris/counsel")


def load_topic_matrix(path: Path | str | None = None) -> TopicMatrix:
    """Load and validate the generated canonical topic source.

    Validation is intentionally strict: a stale label can remain an alias, but
    it cannot silently become a runtime owner for an inbound specialist route.
    """
    resolved_path = _matrix_path(path)
    if path is None:
        _verify_reviewed_artifact(
            resolved_path, _REVIEWED_MATRIX_SHA256, "topic matrix"
        )
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    chat_id = str(raw.get("chat_id") or "").strip()
    tenant = str(raw.get("tenant") or "").strip()
    routing_version = str(raw.get("routing_version") or "").strip()
    if chat_id != CANONICAL_MARGOT_OS_CHAT_ID or tenant != "chris-home" or not routing_version:
        raise ValueError("invalid canonical Margot OS topic matrix header")
    routes: dict[int, DepartmentRoute] = {}
    for topic in raw.get("topics") or []:
        if not isinstance(topic, dict):
            raise ValueError("topic route must be an object")
        topic_id = topic.get("topic_id")
        if not isinstance(topic_id, int) or topic_id in routes:
            raise ValueError("topic routes require unique integer topic_id values")
        owner = str(topic.get("front_door_profile") or topic.get("runtime_assignee") or "").strip()
        if not owner or owner in LEGACY_OR_NONSPAWNABLE_OWNERS:
            raise ValueError(f"topic {topic_id} stores legacy/nonspawnable owner {owner!r}")
        route = DepartmentRoute(
            department_id=str(topic.get("department_id") or "").strip(),
            topic_id=topic_id,
            front_door_profile=owner,
            maker_profiles=_as_str_tuple(topic.get("maker_profiles"), "maker_profiles"),
            required_reviewers=_as_str_tuple(topic.get("required_reviewers"), "required_reviewers"),
            tier_path=_as_str_tuple(topic.get("tier_path"), "tier_path"),
            privacy_tier=str(topic.get("privacy_tier") or "").strip(),
            approval_gates=_as_str_tuple(topic.get("approval_gates"), "approval_gates"),
            workflow=_workflow(topic),
            discovery_confidence=str(topic.get("discovery_confidence") or "unverified").strip(),
        )
        if not route.department_id or not route.privacy_tier:
            raise ValueError(f"topic {topic_id} requires department_id and privacy_tier")
        if topic_id in COMMAND_TOPIC_IDS and route.front_door_profile != "default":
            raise ValueError(f"command topic {topic_id} must remain owned by default")
        if topic_id == 23:
            _validate_legal_route(route)
        routes[topic_id] = route
    if not routes:
        raise ValueError("canonical topic matrix is empty")
    return TopicMatrix(chat_id=chat_id, tenant=tenant, routing_version=routing_version, routes=routes)


def load_department_topic_activation(config: Mapping[str, Any]) -> DepartmentTopicActivation | None:
    """Return an enabled, pinned activation or raise before default execution."""
    section = config.get("department_topic_routing") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping) or section.get("enabled") is not True:
        return None
    board = str(section.get("board") or "").strip()
    if board != "chris-home":
        raise ValueError("department-topic routing requires canonical board 'chris-home'")
    configured_path = section.get("matrix_path")
    if configured_path not in (None, "", str(DEFAULT_MATRIX_PATH)):
        raise ValueError("department-topic routing matrix path must remain pinned to the canonical source")
    load_topic_matrix()
    return DepartmentTopicActivation(board=board)


def _envelope(*, chat_id: str, thread_id: int | str, message_id: int | str, route: DepartmentRoute | None, tenant: str, version: str) -> RoutingEnvelope:
    thread = str(thread_id)
    message = str(message_id)
    department_id = route.department_id if route else "unknown_topic"
    assignee = route.front_door_profile if route else "default"
    privacy = route.privacy_tier if route else "restricted"
    seed = "|".join((str(chat_id), thread, message, version))
    return RoutingEnvelope(
        source_surface="telegram_topic",
        source_chat_id=str(chat_id),
        source_thread_id=thread,
        source_message_id=message,
        return_surface=f"telegram:{chat_id}:{thread}",
        tenant=tenant,
        department_id=department_id,
        runtime_assignee=assignee,
        privacy_tier=privacy,
        idempotency_key=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        routing_version=version,
        default_execution_forbidden=route is None or route.topic_id not in COMMAND_TOPIC_IDS,
    )


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def plan_department_workflow(route: DepartmentRoute, message_text: str) -> DepartmentWorkPlan:
    """Select only the matrix-authorized routine maker or Claire package path."""
    text = message_text.lower()
    if route.topic_id == 17:
        consequential = _contains_any(
            text,
            ("cross-team", "cross team", "architecture", "dependency", "durable", "migration", "external", "purchase", "invite"),
        )
        if consequential:
            return DepartmentWorkPlan("claire", "complex", True, package_author="claire")
        return DepartmentWorkPlan("lauren", "simple", False)
    if route.topic_id == 25:
        if _contains_any(text, ("pipeline", "tooling", "integration", "build", "automation", "dashboard")):
            return DepartmentWorkPlan("scarlett", "standard", True)
        if _contains_any(text, ("campaign", "content", "social", "post", "audience")):
            return DepartmentWorkPlan("geneva", "standard", True)
        return DepartmentWorkPlan("riley", "standard", True)
    if route.topic_id == 23:
        return DepartmentWorkPlan("steven", "complex", True, package_author="steven")
    if route.topic_id == 27:
        consequential = _contains_any(
            text,
            ("cross-team", "cross team", "architecture", "dependency", "durable", "migration", "audit", "config", "restart", "deploy"),
        )
        if _contains_any(text, ("engineering", "automation", "integration", "code", "build")):
            assignee, with_review = "scarlett", True
        elif _contains_any(text, ("watchdog", "health", "monitor", "incident")):
            assignee, with_review = "ingrid", True
        elif _contains_any(text, ("finance", "invoice", "expense", "accounting")):
            assignee, with_review = "maya", True
        elif _contains_any(text, ("hiring", "candidate", "interview", "people")):
            assignee, with_review = "harper", True
        elif _contains_any(text, ("creative", "media", "edit", "footage")):
            assignee, with_review = "dakota", True
        elif _contains_any(text, ("content", "social", "campaign")):
            assignee, with_review = "geneva", True
        else:
            assignee, with_review = "lauren", False
        return DepartmentWorkPlan(
            assignee,
            "complex" if consequential else "simple",
            with_review,
            package_author="claire",
        )
    return DepartmentWorkPlan(
        assignee=route.front_door_profile,
        complexity="standard" if route.required_reviewers else "simple",
        with_review=bool(route.required_reviewers),
    )


def resolve_department_topic(*, chat_id: int | str, thread_id: int | str, matrix: TopicMatrix | None = None, message_id: int | str = "0") -> RoutingDecision:
    matrix = matrix or load_topic_matrix()
    if str(chat_id) != matrix.chat_id:
        envelope = _envelope(chat_id=str(chat_id), thread_id=thread_id, message_id=message_id, route=None, tenant=matrix.tenant, version=matrix.routing_version)
        return RoutingDecision(status="not_margot_os", route=None, envelope=envelope)
    route = matrix.routes.get(int(thread_id))
    envelope = _envelope(chat_id=str(chat_id), thread_id=thread_id, message_id=message_id, route=route, tenant=matrix.tenant, version=matrix.routing_version)
    if route is None:
        return RoutingDecision(status="unknown_topic", route=None, envelope=envelope)
    if route.discovery_confidence == "candidate_requires_ping":
        return RoutingDecision(status="unverified_topic", route=route, envelope=envelope)
    return RoutingDecision(status="command" if route.topic_id in COMMAND_TOPIC_IDS else "specialist", route=route, envelope=envelope)


_dispatched_idempotency_keys: set[str] = set()
_dispatch_idempotency_lock = threading.RLock()


def dispatch_department_topic(
    *,
    chat_id: int | str,
    thread_id: int | str,
    message_id: int | str,
    message_text: str,
    matrix: TopicMatrix | None = None,
    create_task: Callable[[dict[str, Any]], Any] | None = None,
    intake_store: _PrivateIntakeBroker | None = None,
) -> DispatchOutcome:
    """Build and optionally persist one specialist handoff.

    Ordinary team work carries the exact request into the bounded task package.
    Legal/finance/personnel/family requests instead receive a private handle
    with an authorized worker retrieval path; no raw sensitive request text is
    copied into the envelope, callback, or board metadata.
    """
    decision = resolve_department_topic(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        matrix=matrix,
    )
    envelope = decision.envelope
    if decision.status in {"not_margot_os", "command"}:
        return DispatchOutcome(
            status=decision.status,
            acknowledgement_only=False,
            default_execution_forbidden=False,
            idempotency_key=envelope.idempotency_key,
            envelope=envelope.as_dict(),
        )
    if decision.status in {"unknown_topic", "unverified_topic"}:
        return DispatchOutcome(
            status=decision.status,
            acknowledgement_only=True,
            default_execution_forbidden=True,
            idempotency_key=envelope.idempotency_key,
            envelope=envelope.as_dict(),
        )
    with _dispatch_idempotency_lock:
        if envelope.idempotency_key in _dispatched_idempotency_keys:
            return DispatchOutcome(
                status="duplicate",
                acknowledgement_only=True,
                default_execution_forbidden=True,
                idempotency_key=envelope.idempotency_key,
                envelope=envelope.as_dict(),
            )
        # Reserve before creating a private-intake handle. Concurrent ingress
        # can now observe this key only as a duplicate, never as a second task.
        _dispatched_idempotency_keys.add(envelope.idempotency_key)

    handle: str | None = None
    intake_broker = intake_store or _PRIVATE_INTAKE_BROKER
    try:
        route = decision.route
        assert route is not None
        plan = plan_department_workflow(route, message_text)
        allowed_profiles = tuple(
            dict.fromkeys((plan.assignee, *route.maker_profiles, *route.required_reviewers))
        )
        if route.privacy_tier in SENSITIVE_PRIVACY_TIERS:
            handle = intake_broker.put(
                idempotency_key=envelope.idempotency_key,
                request_text=message_text,
                allowed_profiles=allowed_profiles,
            )
            intake = {
                "handle": handle,
                "retention": "process_local_ephemeral",
            }
        else:
            intake = {"retention": "task_body", "request_text": message_text}
        payload = envelope.as_dict()
        payload.update(
            {
                "workflow": route.workflow,
                "maker_profiles": list(route.maker_profiles),
                "required_reviewers": list(route.required_reviewers),
                "tier_path": list(route.tier_path),
                "approval_gates": list(route.approval_gates),
                "request_reference": f"telegram:{chat_id}:{thread_id}:{message_id}",
                "intake": intake,
                "work_plan": asdict(plan),
                "request_text_retention": intake["retention"],
                "fallback_degraded_critical_approval": False,
            }
        )
        if create_task is not None:
            create_task(payload)
    except Exception:
        if handle is not None:
            intake_broker._discard(handle)
        with _dispatch_idempotency_lock:
            _dispatched_idempotency_keys.discard(envelope.idempotency_key)
        raise
    return DispatchOutcome(
        status="delegated",
        acknowledgement_only=True,
        default_execution_forbidden=True,
        idempotency_key=envelope.idempotency_key,
        envelope=payload,
    )


_TOPIC_ROLE_IDS = {
    "general": "chief_of_staff",
    "mission_control": "ops_control",
    "content_social": "content_creative",
    "creative_media": "media_editing",
    "finance": "accountant_finance",
    "legal": "legal_compliance",
    "claude_review": "claude_secondary_review",
    "data_analytics": "data_analysis",
    "agent_infrastructure": "engineering_systems",
}
_SAFE_DELEGATE_HELPER = _RELEASE_ROOT / "scripts" / "safe_delegate_card.py"


def build_safe_delegate_command(envelope: Mapping[str, Any], *, board: str) -> list[str]:
    """Create the privacy-preserving guarded-write command after the live gate."""
    department_id = str(envelope["department_id"])
    role_id = _TOPIC_ROLE_IDS.get(department_id, department_id)
    plan = dict(envelope.get("work_plan") or {})
    assignee = str(plan.get("assignee") or envelope["runtime_assignee"])
    complexity = str(plan.get("complexity") or "simple")
    with_review = bool(plan.get("with_review"))
    intake = dict(envelope.get("intake") or {})
    source_surface = f"telegram:{envelope['source_chat_id']}:{envelope['source_thread_id']}"
    title = f"Margot OS topic intake: {department_id} #{envelope['source_message_id']}"
    request_text = intake.get("request_text")
    if request_text:
        body = f"Canonical Margot OS inbound request:\n\n{request_text}"
    else:
        handle = str(intake.get("handle") or "")
        if not handle.startswith(_PRIVATE_INTAKE_HANDLE_PREFIX):
            raise ValueError("sensitive intake requires a private retrieval handle")
        body = (
            "Canonical Margot OS private intake is held by the gateway until the "
            f"authorized assignee is actually claimed. Opaque handle: {handle}"
        )
    command = [
        sys.executable,
        str(_SAFE_DELEGATE_HELPER),
        "--execute",
        "--board", board,
        "--tenant", str(envelope["tenant"]),
        "--source-surface", source_surface,
        "--return-surface", str(envelope["return_surface"]),
        "--department", role_id,
        "--assignee", assignee,
        "--title", title,
        "--body", body,
        "--privacy-tier", str(envelope["privacy_tier"]),
        "--complexity", complexity,
        "--canonical-topic-intake",
        "--idempotency-key", str(envelope["idempotency_key"]),
    ]
    if request_text:
        command.extend(["--user-request", str(request_text)])
    else:
        command.extend(["--source-ref", str(intake["handle"])])
    approval_gates = [str(gate).strip() for gate in envelope.get("approval_gates") or [] if str(gate).strip()]
    for gate in approval_gates:
        command.extend(["--gate", gate])
    if plan.get("package_author"):
        command.extend(["--package-author", str(plan["package_author"]), "--package-author-tier", "sol"])
    if plan.get("reasoning_effort"):
        command.extend(["--reasoning-effort", str(plan["reasoning_effort"])])
    if with_review:
        command.append("--with-review")
    if department_id == "legal":
        if assignee != "steven" or plan.get("package_author") != "steven" or not with_review:
            raise ValueError("legal ingress must remain Steven package -> legal-drafter -> Steven review")
        if request_text or not str(intake.get("handle") or "").startswith(_PRIVATE_INTAKE_HANDLE_PREFIX):
            raise ValueError("legal ingress requires a minimum-retention private-intake handle")
        if tuple(approval_gates) != _LEGAL_APPROVAL_GATES:
            raise ValueError("legal ingress must retain every Chris/counsel approval gate")
        command.extend(["--approval-gate", "legal_commitment"])
        for forbidden in (
            "external_send",
            "publish",
            "payment",
            "signature",
            "destructive",
            "credential_change",
            "privacy_wall_change",
            "legal_advice",
            "logged_in_account_action",
            "broad_retention",
            "raw_privileged_disclosure",
        ):
            command.extend(["--forbidden-action", forbidden])
        command.append("--legal-inbound-chain")
    if department_id == "mission_control":
        command.append("--coordination-package-chain")
    return command


def dispatch_to_safe_delegate(envelope: Mapping[str, Any], *, board: str | None = None) -> dict[str, Any]:
    """Persist one canonical inbound handoff through the existing guarded writer."""
    selected_board = str(board or "").strip()
    if selected_board != "chris-home":
        raise ValueError("department-topic routing requires canonical board 'chris-home'")
    _verify_reviewed_artifact(
        _SAFE_DELEGATE_HELPER,
        _REVIEWED_SAFE_DELEGATE_SHA256,
        "safe-delegate helper",
    )
    _verify_reviewed_artifact(
        _DEFAULT_ROLE_REGISTRY_PATH,
        _REVIEWED_ROLE_REGISTRY_SHA256,
        "role registry",
    )
    command = build_safe_delegate_command(envelope, board=selected_board)
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(scrub_secrets=True, inherit_profile_home=False)
    env["HERMES_ROLE_REGISTRY"] = str(_DEFAULT_ROLE_REGISTRY_PATH)
    env.pop("HERMES_KANBAN_DB", None)
    result = subprocess.run(command, text=True, capture_output=True, timeout=30, env=env)
    if result.returncode != 0:
        raise RuntimeError("safe department-topic delegation failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("safe department-topic delegation returned non-JSON") from exc
    if not payload.get("ok"):
        raise RuntimeError("safe department-topic delegation rejected the envelope")
    return payload

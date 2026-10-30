#!/usr/bin/env python3
"""Create Hermes Kanban cards with a guaranteed executable owner.

This is the write-side counterpart to board_hygiene_audit.py. It keeps the
human-facing department/pod labels in card metadata while ensuring the actual
Kanban assignee is a spawnable Hermes profile.

Default mode is dry-run. Pass --execute to create the card.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # canonical role registry is JSON-compatible YAML
    yaml = None

HOME = Path.home()
_configured_hermes_home = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes"))).expanduser()
# Dispatched workers receive a profile-local HERMES_HOME (for example
# ~/.hermes/profiles/scarlett). Board discovery and profile routing are shared,
# so resolve that injected profile path back to the canonical Hermes root.
HERMES_HOME = (
    _configured_hermes_home.parent.parent
    if _configured_hermes_home.parent.name == "profiles"
    else _configured_hermes_home
)
BOARDS_ROOT = HERMES_HOME / "kanban" / "boards"
DEFAULT_DB = HERMES_HOME / "kanban.db"
ROLE_REGISTRY = Path(
    os.environ.get(
        "HERMES_ROLE_REGISTRY",
        str(HOME / ".openclaw" / "workspace" / "Agent-Shared" / "roles" / "role-registry.yaml"),
    )
).expanduser()

BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.I)
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.I)
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8,}$")
TELEGRAM_SURFACE_RE = re.compile(r"^telegram:(?P<chat>-?\d+)(?::(?P<thread>\d+))?(?::.*)?$", re.I)
COCKPIT_VOICE_SOURCE_SURFACE = "agent-os:cockpit:voice"
COCKPIT_VOICE_CALLBACK_SURFACE = "agent-os:cockpit:voice-callback"
COCKPIT_USER_RE = re.compile(r"^[a-z0-9._-]{1,40}$")
COCKPIT_SESSION_RE = re.compile(r"^agent-os:cockpit:(?P<user>[a-z0-9._-]{1,40})$")
COCKPIT_USER_BOARDS = {
    "chris": "chris-home",
    "connor": "connor-cockpit-lanes",
}
VALID_PACKAGE_CLASSES = {"deliverable", "decision", "status"}
APPROVAL_SENSITIVITIES = {
    "payment",
    "external_send",
    "destructive",
    "legal",
    "finance",
    "secrets",
    "config",
    "production",
    "personnel",
    "privacy",
}

SENSITIVE_PRIVACY_TIERS = {"finance", "legal", "personnel", "family", "creator_only"}
CLAIRE_SOL_GATE_REASONS = {
    "ambiguous",
    "audit_or_qa_chain",
    "audit_required",
    "callback_required",
    "config",
    "conflicting_evidence",
    "crash_resistant",
    "cross_department",
    "destructive",
    "durable",
    "external_action",
    "external_facing",
    "external_facing_final",
    "final_verdict",
    "high_cost",
    "legal_financial_personnel_privacy_security",
    "migration",
    "multi_stage",
    "privacy_wall_change",
    "production",
    "production_or_architecture",
    "repeated_failed_qa",
    "sensitive",
}
IRREVERSIBLE_GATES = {
    "banking",
    "config",
    "credential_change",
    "counsel_send",
    "client_send",
    "deploy",
    "destructive",
    "external_calendar_invite",
    "external_send",
    "filing",
    "full_financial_sharing",
    "legal_commitment",
    "payment",
    "privacy_wall_change",
    "public_post",
    "publish",
    "restart",
    "signature",
    "tax_claim",
}
HUMAN_DECISION_GATES = IRREVERSIBLE_GATES | {
    # Exact action gates used by the reviewed department-topic matrix. Keep
    # their specificity in metadata instead of dropping or weakening them.
    "logged_in_private_pull",
    "package_update",
    "brand_sensitive_claim",
    "external_delivery",
    "delete_or_move_originals",
    "paid_render",
    "account_change",
    "sensitive_personal_data",
    "offer",
    "rejection",
    "compensation",
    "spend",
    "external_outreach_send",
    "share_full_financials",
    "private_data_exposure",
    "financial_data_exposure",
    "personnel_data_exposure",
    "system_update",
    "ad_spend",
    "api_spend",
    "bulk_archive",
    "cutover",
    "delete",
    "external_action",
    "external_outreach",
    "finance",
    "hard_to_undo",
    "high_cost",
    "identity_change",
    "irreversible_data_loss",
    "legal",
    "overwrite",
    "paid_boost",
    "paid_generation",
    "personnel",
    "privacy",
    "production",
    "purchase",
    "reset",
    "revoke",
    "secrets",
    "security",
    "subscription",
}
SOL_PACKAGE_AUTHORS = {"claire", "claire/sol", "sol", "steven", "steven/sol"}
SOL_COMMAND_AUTHORS = {"default", "default/sol", "margot", "margot/sol", "sol"}
OWNER_ACCEPTANCE_ROUTING_CONTRACT = "sol_owner_terra_maker_single_root"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
SOL_OWNER_TERRA_MAKER_DEPARTMENTS = {
    "lauren": ("assistant_ops", "ops-rowan"),
    "maya": ("accountant_finance", "finance-ellis"),
    "geneva": ("content_creative", "content-celia"),
    "riley": ("research", "research-quinn"),
    "scarlett": ("engineering_systems", "engineering-wren"),
    "dakota": ("media_editing", "media-luca"),
    "harper": ("hiring_people", "hiring-naomi"),
    "julia": ("personal_family", "family-eliana"),
    "sloane": ("revenue_partnerships", "revenue-reid"),
    "parker": ("production_operations", "production-tessa"),
    "steven": ("legal_compliance", "legal-drafter"),
}
FABLE_SECONDARY_REVIEW_DEPARTMENTS = {"accountant_finance", "legal_compliance"}
SOL_OWNER_TERRA_MAKER_PAIRS = {
    owner: maker
    for owner, (_, maker) in SOL_OWNER_TERRA_MAKER_DEPARTMENTS.items()
}
SOL_OWNER_PROFILES = set(SOL_OWNER_TERRA_MAKER_PAIRS)
TERRA_MAKER_PROFILES = set(SOL_OWNER_TERRA_MAKER_PAIRS.values())
CRITICAL_APPROVED_STATUSES = {"approved", "approved_final", "critical_verdict_approved", "final_approved"}
OWNER_CELL_V1_FLAGS = {
    "enabled": False,
    "shadow_router": True,
    "enforce_worker_budget": False,
    "enforce_target_leases": False,
    "enforce_state_versions": False,
    "enforce_verdict_gates": False,
    "suppress_internal_notifications": False,
}


def select_owner_cell_shadow(signals: dict[str, Any]) -> dict[str, str]:
    """Return a deterministic, non-enforcing Owner Cell shadow decision.

    This helper is deliberately side-effect free during the staging window: the
    caller records its decision for canary comparison but continues legacy card
    creation until an approved runtime activation enables enforcement.
    """
    if signals.get("extra_workers"):
        return {"mode": "fanout", "risk_class": "material", "rationale": "independent deliverables requested"}
    review_keys = ("review_required", "material_code", "consequential", "privacy_risk", "security_risk", "explicit_verdict")
    if any(bool(signals.get(key)) for key in review_keys):
        return {"mode": "reviewed", "risk_class": "material", "rationale": "independent review trigger"}
    if bool(signals.get("deterministic")) and bool(signals.get("read_only") or signals.get("preapproved_reversible")):
        return {"mode": "script", "risk_class": "low", "rationale": "deterministic bounded operation"}
    return {"mode": "single", "risk_class": "low", "rationale": "one bounded owner is sufficient"}


DEFAULT_ALLOWED_TOOLSETS = ["terminal", "file", "kanban", "skills", "session_search", "todo"]
DEFAULT_FORBIDDEN_ACTIONS = [
    "external_send",
    "publish",
    "payment",
    "signature",
    "destructive",
    "credential_change",
    "privacy_wall_change",
]
# Chris-requested work may edit config (with backup), deploy, and request
# restarts through existing mechanisms without re-approval. Real gates above
# remain. Own processes never self-restart: post the ready restart for Ava.
DEFAULT_ALLOWED_SIDE_EFFECTS = [
    "read_only",
    "local_artifact",
    "internal_task_update",
    "config_edit_with_backup",
    "deploy_via_existing_mechanism",
    "restart_via_external_activation",
    "scoped_tool_install",
]
# Baseline for anyone other than Chris, and for private lanes (family,
# legal, talent): the old conservative defaults stay.
BASELINE_ALLOWED_SIDE_EFFECTS = ["read_only", "local_artifact", "internal_task_update"]
BASELINE_EXTRA_FORBIDDEN = ["deploy", "restart", "config"]
BROAD_DEFAULT_REQUESTERS = frozenset({"chris"})
BROAD_DEFAULT_PRIVACY_TIERS = frozenset({"team", "normal", ""})
PRIVATE_DEPARTMENTS = frozenset({"personal_family", "legal_compliance"})
_HOLD_TO_SIDE_EFFECT = {
    "deploy": "deploy_via_existing_mechanism",
    "restart": "restart_via_external_activation",
    "config": "config_edit_with_backup",
    "install": "scoped_tool_install",
}


def broad_defaults_apply(route) -> bool:
    """Chris's standing authorization (t_9add3e3d) covers his own team-lane work only."""
    return (
        (route.requesting_user or "") in BROAD_DEFAULT_REQUESTERS
        and (route.privacy_tier or "") in BROAD_DEFAULT_PRIVACY_TIERS
        and route.department_id not in PRIVATE_DEPARTMENTS
    )


def default_side_effect_policy(route) -> tuple[list[str], list[str]]:
    if broad_defaults_apply(route):
        return list(DEFAULT_ALLOWED_SIDE_EFFECTS), list(DEFAULT_FORBIDDEN_ACTIONS)
    return list(BASELINE_ALLOWED_SIDE_EFFECTS), list(DEFAULT_FORBIDDEN_ACTIONS) + BASELINE_EXTRA_FORBIDDEN


REVIEWER_FALLBACK_ORDER = ("claire", "default", "claude-review")
PRODUCER_KINDS = {
    "model",
    "deterministic_script",
    "watchdog",
    "status_read",
    "mechanical_reversible_update",
}
REVIEW_EXEMPT_PRODUCER_KINDS = {
    "deterministic_script",
    "watchdog",
    "status_read",
    "mechanical_reversible_update",
}
FABLE_MAX_ADVISORY_CONTEXT_ITEMS = 4
FABLE_MAX_ADVISORY_CONTEXT_CHARS = 4_000

ENGINEERING_QA_WORDS = ("qa", "review", "smoke", "test", "verify", "verification")
ENGINEERING_STANDALONE_QA_TITLE_RE = re.compile(
    r"^\s*(?:qa|review|verification|verify|smoke(?:\s+test)?|acceptance(?:\s+test)?)\b",
    re.I,
)
ENGINEERING_MONITOR_WORDS = ("cron", "watchdog", "monitor", "health", "alert")
ENGINEERING_CODEX_WORDS = ("codex", "autonomous coding", "agent cli")
FINANCE_ROUTE_WORDS = (
    "accounting",
    "bank",
    "banking",
    "books",
    "bookkeeping",
    "budget",
    "expense",
    "finance",
    "financial",
    "invoice",
    "payable",
    "payment",
    "payroll",
    "reconciliation",
    "tax",
)
LEGAL_ROUTE_WORDS = (
    "agreement",
    "compliance",
    "contract",
    "counsel",
    "legal",
    "nda",
    "obligation",
    "release",
    "rights",
    "signature",
    "signing",
    "terms",
)
# These route directly to the required Steven/Sol senior-review lane. Generic
# first-pass legal drafting stays with legal-drafter and gets Steven as reviewer.
LEGAL_SENIOR_REVIEW_WORDS = (
    "counsel gate",
    "counsel-gate",
    "counsel sign-off",
    "final legal review",
    "legal gate",
    "legal review",
    "returned for revision",
    "senior review",
    "signature packet",
    "unresolved legal decision",
)
COMMAND_ENGINEERING_WORDS = (
    "automate",
    "build",
    "callback",
    "code",
    "config",
    "fix",
    "implement",
    "integration",
    "repair",
    "system",
)
COMMAND_RESEARCH_WORDS = ("compare", "due diligence", "research", "source-backed", "sources")


def has_route_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) for word in words)


def command_specialist_for(text: str) -> str | None:
    """Keep the private command lane from becoming a substantive worker pool."""
    lowered = text.lower()
    if has_route_word(lowered, ENGINEERING_MONITOR_WORDS):
        return "ingrid"
    if has_route_word(lowered, COMMAND_ENGINEERING_WORDS):
        return "scarlett"
    if has_route_word(lowered, ENGINEERING_QA_WORDS) or "audit" in lowered:
        return "grace"
    if has_route_word(lowered, COMMAND_RESEARCH_WORDS):
        return "riley"
    return None


def is_routine_reversible_ops_request(args: argparse.Namespace) -> bool:
    """Recognize the narrow routine path that must not consume Claire/Sol execution."""
    if str(getattr(args, "complexity", "simple") or "simple").lower() != "simple":
        return False
    text = f"{getattr(args, 'title', '')}\n{getattr(args, 'body', '')}".lower()
    routine_words = ("mechanical", "reversible", "routine status", "status update", "task update")
    return producer_kind_for(args) in REVIEW_EXEMPT_PRODUCER_KINDS or any(word in text for word in routine_words)


def is_explicit_standalone_engineering_qa(
    title: str,
    producer_references: list[str] | None,
) -> bool:
    """Return whether this is a review-only task with a declared producer reference.

    Generic engineering work often says "test", "verify", or "review" as
    acceptance criteria. Those words must not replace the Engineering owner
    with QA. The QA lane is selected only for an explicitly named standalone
    review task that points to an existing producer artifact or task.
    """
    return bool(ENGINEERING_STANDALONE_QA_TITLE_RE.match(title or "")) and any(
        str(reference).strip() for reference in (producer_references or [])
    )


def default_return_surface(source_surface: str) -> str:
    """Return the callback surface implied by a source surface.

    Command Center voice turns arrive with ``source_surface=agent-os:cockpit:voice``.
    If the caller omits ``--return-surface``, the safe delegate helper must not
    echo that source as the return target; loop-back work is consumed by the
    cockpit's voice-callback pathway instead.
    """
    if str(source_surface or "").strip() == COCKPIT_VOICE_SOURCE_SURFACE:
        return COCKPIT_VOICE_CALLBACK_SURFACE
    return source_surface

# Canonical pod metadata for role-registry entries that do not yet carry a
# first-class `pod` block. This keeps cockpit cards readable while still using
# the registry's spawnable `kanban_assignee` for execution.
CANONICAL_PODS: dict[str, dict[str, str]] = {
    "chief_of_staff": {
        "display_name": "Margot Chief of Staff Pod",
        "slug": "margot-chief-of-staff",
        "named_owner": "Margot",
        "default_owner_role": "Command Owner",
        "verifier_role": "Margot / Source Thread",
    },
    "assistant_ops": {
        "display_name": "Lauren Assistant Ops Pod",
        "slug": "lauren-assistant-ops",
        "named_owner": "Lauren",
        "default_owner_role": "Assistant Ops Lead",
        "verifier_role": "Margot / Source Thread",
    },
    "engineering_systems": {
        "display_name": "Scarlett Engineering Pod",
        "slug": "scarlett-engineering",
        "named_owner": "Scarlett",
        "default_owner_role": "Engineering Lead",
        "verifier_role": "Grace QA Reviewer",
    },
    "research": {
        "display_name": "Riley Research Pod",
        "slug": "riley-research",
        "named_owner": "Riley",
        "default_owner_role": "Research Lead",
        "verifier_role": "Citation Verifier",
    },
    "data_analysis": {
        "display_name": "Riley Content Analytics Pod",
        "slug": "riley-content-analytics",
        "named_owner": "Riley",
        "default_owner_role": "Content Analytics Owner",
        "verifier_role": "Grace / Evidence Verifier",
    },
    "watchdog_health": {
        "display_name": "Ingrid Watchdog Pod",
        "slug": "ingrid-watchdog",
        "named_owner": "Ingrid",
        "default_owner_role": "Health Monitor",
        "verifier_role": "Margot / Source Thread",
    },
    "content_creative": {
        "display_name": "Geneva Content Lane",
        "slug": "geneva-content-lane",
        "named_owner": "Geneva",
        "default_owner_role": "Content Lane Owner",
        "verifier_role": "Grace / Editorial Gate",
    },
    "media_editing": {
        "display_name": "Dakota Media/Post Pod",
        "slug": "dakota-media-post",
        "named_owner": "Dakota",
        "default_owner_role": "Media/Post Owner",
        "verifier_role": "Grace / Editorial Gate",
    },
    "legal_compliance": {
        "display_name": "Steven Legal / Counsel Ops Pod",
        "slug": "steven-legal-counsel",
        "named_owner": "Steven",
        "default_owner_role": "Junior Legal Drafter",
        "verifier_role": "Steven/Sol required senior review + Chris/Counsel approval gate",
    },
    "accountant_finance": {
        "display_name": "Maya Finance / Accounting Pod",
        "slug": "maya-finance-accounting",
        "named_owner": "Maya",
        "default_owner_role": "Finance / Accounting Ops Owner",
        "verifier_role": "Chris / Connor Approval Gate + Claude Review",
    },
    "claude_secondary_review": {
        "display_name": "Claude Review Pod",
        "slug": "claude-review",
        "named_owner": "Claude Review",
        "default_owner_role": "Secondary Review Router",
        "verifier_role": "Primary Owner + Chris Approval Gate",
    },
    "hiring_people": {
        "display_name": "Harper Hiring/Outreach Pod",
        "slug": "harper-hiring-outreach",
        "named_owner": "Harper",
        "default_owner_role": "Hiring/Outreach Owner",
        "verifier_role": "Chris / Claire Approval Gate",
    },
    "personal_family": {
        "display_name": "Julia Private Family Pod",
        "slug": "julia-private-family",
        "named_owner": "Julia",
        "default_owner_role": "Private Family Agent",
        "verifier_role": "Chris Approval Gate",
    },
}


def slugify(value: str, fallback: str = "new-department") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def load_registry(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        hooks = _NATIVE_EXECUTION.get()
        path = hooks["registry"] if hooks is not None else ROLE_REGISTRY
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CockpitRoutingError(f"Cannot read role registry: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if yaml is None:
            raise CockpitRoutingError(
                "Cannot read role registry: non-JSON YAML requires PyYAML"
            )
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise CockpitRoutingError(f"Cannot read role registry: {exc}") from exc
    if not isinstance(data, dict):
        raise CockpitRoutingError("Cannot read role registry: top level must be a mapping")
    return data


def spawnable_profiles(
    hermes_home: Path = HERMES_HOME,
    registry_path: Path | None = None,
) -> set[str]:
    installed = {"default"} if (hermes_home / "config.yaml").exists() else set()
    profiles_root = hermes_home / "profiles"
    if profiles_root.exists():
        for child in profiles_root.iterdir():
            if child.is_dir() and (child / "config.yaml").exists():
                installed.add(child.name)
    registry = load_registry(registry_path)
    allowed = {
        str(profile).strip()
        for profile in ((registry.get("kanban_assignment_policy") or {}).get("spawnable_profiles") or [])
        if str(profile).strip()
    }
    if not allowed:
        raise CockpitRoutingError("role registry has no ordinary spawnable profile allowlist")
    return installed & allowed


def known_boards() -> set[str]:
    boards = {"default"} if DEFAULT_DB.exists() else set()
    if BOARDS_ROOT.exists():
        boards.update(p.parent.name for p in BOARDS_ROOT.glob("*/kanban.db"))
    return boards


def find_role(registry: dict[str, Any], query: str | None) -> dict[str, Any] | None:
    if not query:
        return None
    q = query.strip().lower()
    q_slug = slugify(q)
    role_aliases = {
        "claire": "ops_control",
        "claire-ops": "ops_control",
        "ops": "ops_control",
        "ops-control": "ops_control",
        "operations": "ops_control",
        "operations-control": "ops_control",
        "opslane": "ops_control",
        "audrey": "research",
        "riley": "research",
        "riley-research": "research",
        "research-lane": "research",
        "researcher": "research",
        "researcher-a": "research",
        "researcher-b": "research",
        "scarlett": "engineering_systems",
        "scarlett-engineering": "engineering_systems",
        "builder": "engineering_systems",
        "engineer": "engineering_systems",
        "engineering": "engineering_systems",
        "engineering-systems": "engineering_systems",
        "sydney": "data_analysis",
        "geneva": "content_creative",
        "content-lane": "content_creative",
        "content-analytics": "data_analysis",
        "content_analytics": "data_analysis",
        "performance-insights": "data_analysis",
        "metrical": "content_creative",
        "metrical-records": "content_creative",
        "metrical-content": "content_creative",
        "metricool": "content_creative",
        "maya": "accountant_finance",
        "finance": "accountant_finance",
        "finance-ops": "accountant_finance",
        "accounting": "accountant_finance",
        "accounting-ops": "accountant_finance",
        "legal-drafter": "legal_compliance",
        "junior-legal-drafter": "legal_compliance",
        "steven": "legal_compliance",
        "legal": "legal_compliance",
        "legal-ops": "legal_compliance",
        "legal-compliance": "legal_compliance",
        "counsel": "legal_compliance",
        "counsel-ops": "legal_compliance",
        "compliance": "legal_compliance",
        "compliance-ops": "legal_compliance",
        "maya-finance": "accountant_finance",
        "maya-finance-accounting": "accountant_finance",
        "finance-accounting": "accountant_finance",
        "steven-legal": "legal_compliance",
        "steven-legal-counsel": "legal_compliance",
        "legal-counsel": "legal_compliance",
        "claude-review": "claude_secondary_review",
        "claude-secondary-review": "claude_secondary_review",
        "secondary-review": "claude_secondary_review",
        "vivienne": "accountant_finance",
        "finlegal": "accountant_finance",
        "harper": "hiring_people",
        "hiring-outreach": "hiring_people",
        "people-pipeline": "hiring_people",
        "julia": "personal_family",
        "family": "personal_family",
        "personal-family": "personal_family",
        "sienna": "personal_family",
        "personal-logistics": "personal_family",
        "dakota": "media_editing",
        "media-post": "media_editing",
        "content-media": "content_creative",
        "metricool-records": "content_creative",
        "publishing-prep": "content_creative",
        "sloane": "revenue_partnerships",
        "revenue": "revenue_partnerships",
        "sales": "revenue_partnerships",
        "partnerships": "revenue_partnerships",
        "client-success": "revenue_partnerships",
        "parker": "production_operations",
        "production": "production_operations",
        "production-operations": "production_operations",
        "line-producer": "production_operations",
        "fable": "premium_strategy_review",
        "fable-5": "premium_strategy_review",
        "fable-review": "premium_strategy_review",
        "premium-review": "premium_strategy_review",
    }
    target_id = role_aliases.get(q) or role_aliases.get(q_slug)
    if target_id:
        for role in registry.get("roles", []):
            if role.get("id") == target_id:
                return role

    # Prefer canonical role ids/names/pods before matching runtime/profile aliases.
    # Otherwise a query like "research" can accidentally match a finance/legal role
    # merely because that role uses the legacy `research` profile as a helper.
    for role in registry.get("roles", []):
        role_id = str(role.get("id", ""))
        # Match both role-registry pod blocks and fallback canonical pod labels.
        # Some installed departments intentionally rely on CANONICAL_PODS only,
        # so cockpit labels such as "Scarlett Engineering Pod" must resolve to
        # the underlying role instead of falling through as a new department.
        pod = {**CANONICAL_PODS.get(role_id, {}), **(role.get("pod") or {})}
        primary_candidates = {
            role_id.lower(),
            slugify(role_id),
            str(role.get("name", "")).lower(),
            slugify(str(role.get("name", ""))),
            str(pod.get("display_name", "")).lower(),
            str(pod.get("slug", "")).lower(),
            slugify(str(pod.get("display_name", ""))),
        }
        if q in primary_candidates or q_slug in primary_candidates:
            return role

    for role in registry.get("roles", []):
        runtime_candidates: set[str] = set()
        for rt in role.get("default_runtimes", []):
            runtime_candidates.add(str(rt.get("agent", "")).lower())
            runtime_candidates.add(slugify(str(rt.get("agent", ""))))
            runtime_candidates.add(str(rt.get("profile", "")).lower())
            runtime_candidates.add(str(rt.get("kanban_assignee", "")).lower())
        if q in runtime_candidates or q_slug in runtime_candidates:
            return role
    return None


def is_legal_senior_review_request(title: str, body: str) -> bool:
    """Recognize explicit senior-review intent without treating drafting as review."""
    text = f"{title}\n{body}".lower()
    title_requests_senior_review = bool(
        re.match(r"^\s*(review|revise|revision)\b", title.lower())
    )
    return title_requests_senior_review or has_route_word(text, LEGAL_SENIOR_REVIEW_WORDS)


def choose_assignee(
    role: dict[str, Any] | None,
    spawnable: set[str],
    title: str,
    body: str,
    explicit_assignee: str | None = None,
    producer_references: list[str] | None = None,
) -> tuple[str, str]:
    text = f"{title}\n{body}".lower()
    if explicit_assignee:
        explicit_assignee = explicit_assignee.strip().lower()
        if explicit_assignee not in spawnable:
            raise ValueError(
                f"assignee '{explicit_assignee}' is not spawnable; choose one of {', '.join(sorted(spawnable))}"
            )
        return explicit_assignee, "explicit canonical spawnable assignee"

    role_id = str((role or {}).get("id", ""))
    if role_id == "engineering_systems":
        if (
            has_route_word(text, ENGINEERING_QA_WORDS)
            and is_explicit_standalone_engineering_qa(title, producer_references)
            and "grace" in spawnable
        ):
            return "grace", "explicit standalone engineering QA/review route with producer reference"
        if any(w in text for w in ENGINEERING_MONITOR_WORDS) and "ingrid" in spawnable:
            return "ingrid", "engineering health/monitor keyword route"
        if any(w in text for w in ENGINEERING_CODEX_WORDS) and "scarlett" in spawnable:
            return "scarlett", "engineering autonomous-coding keyword route"
    if role_id == "legal_compliance":
        if is_legal_senior_review_request(title, body) and "steven" in spawnable:
            return "steven", "required Steven/Sol legal review/revision/gate keyword route"
        if "legal-drafter" in spawnable:
            return "legal-drafter", "first-pass legal drafting route with required Steven/Sol review"

    for rt in (role or {}).get("default_runtimes", []):
        candidate = str(rt.get("kanban_assignee") or rt.get("profile") or "").strip().lower()
        if candidate in spawnable:
            return str(candidate), f"role registry route: {role_id or 'unknown'}"

    if "default" in spawnable:
        return "default", "fallback command-layer owner"
    if not spawnable:
        raise ValueError("no spawnable Hermes profiles found on disk")
    return sorted(spawnable)[0], "fallback first spawnable owner"


@dataclass
class ResolvedRoute:
    board: str
    requesting_user: str | None
    audience: str
    cockpit_board: str | None
    source_session_key: str | None
    tenant: str
    title: str
    assignee: str
    owner_name: str
    department_id: str
    department_name: str
    pod: str
    pod_slug: str
    member_role: str
    member_role_slug: str
    handoff_label: str
    source_surface: str
    return_surface: str
    privacy_tier: str
    approval_gate: str | None
    verifier: str
    reviewer_profiles: list[str]
    route_reason: str
    proposed_new_department: bool


@dataclass
class ComplexityGate:
    complexity: str
    complexity_reasons: list[str]
    requires_claire_sol: bool
    required_reason: str | None
    metadata_satisfied_by: str | None


class CockpitRoutingError(ValueError):
    """Raised before mutation when trusted cockpit lane fields disagree."""


def normalize_cockpit_user(value: Any) -> str | None:
    if value is None:
        return None
    user = str(value).strip().lower()
    if not user or not COCKPIT_USER_RE.fullmatch(user):
        return None
    return user


def resolve_cockpit_board(value: Any) -> str:
    user = normalize_cockpit_user(value)
    if user is None or user not in COCKPIT_USER_BOARDS:
        raise CockpitRoutingError(f"unknown or malformed cockpit user: {value!r}")
    return COCKPIT_USER_BOARDS[user]


def cockpit_user_from_session_key(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    match = COCKPIT_SESSION_RE.fullmatch(raw)
    if match:
        return match.group("user")
    if raw.startswith("agent-os:cockpit"):
        raise CockpitRoutingError(f"malformed cockpit session key: {value!r}")
    return None


def _is_cockpit_request(args: argparse.Namespace) -> bool:
    surfaces = (
        getattr(args, "source_surface", None),
        getattr(args, "return_surface", None),
        getattr(args, "tenant", None),
    )
    return bool(
        getattr(args, "requesting_user", None)
        or getattr(args, "source_session_key", None)
        or getattr(args, "handoff_to_user", None)
        or any(str(value or "").strip().lower().startswith("agent-os:cockpit") for value in surfaces)
    )


def _resolve_lane_fields(args: argparse.Namespace) -> tuple[str, str | None, str, str | None, str | None]:
    """Return board, authenticated source user, audience, cockpit board, session key."""
    explicit_user_raw = getattr(args, "requesting_user", None)
    explicit_user = normalize_cockpit_user(explicit_user_raw)
    if explicit_user_raw and explicit_user is None:
        raise CockpitRoutingError(f"malformed cockpit user: {explicit_user_raw!r}")
    session_key = str(getattr(args, "source_session_key", None) or "").strip().lower() or None
    session_user = cockpit_user_from_session_key(session_key)
    if explicit_user and session_user and explicit_user != session_user:
        raise CockpitRoutingError("requesting user and cockpit session user disagree")
    source_user = explicit_user or session_user
    is_cockpit = _is_cockpit_request(args)
    if is_cockpit and source_user is None:
        raise CockpitRoutingError("cockpit routing requires --requesting-user or a valid --source-session-key")
    if source_user is not None:
        resolve_cockpit_board(source_user)
        session_key = session_key or f"agent-os:cockpit:{source_user}"

    handoff_target_raw = getattr(args, "handoff_to_user", None)
    handoff_target = normalize_cockpit_user(handoff_target_raw)
    if handoff_target_raw and (handoff_target is None or handoff_target not in COCKPIT_USER_BOARDS):
        raise CockpitRoutingError(f"unknown or malformed handoff target: {handoff_target_raw!r}")
    if handoff_target:
        if source_user is None:
            raise CockpitRoutingError("cross-user handoff requires an authenticated source user")
        declared_from_raw = getattr(args, "handoff_from_user", None)
        declared_from = normalize_cockpit_user(declared_from_raw) if declared_from_raw else source_user
        if declared_from != source_user:
            raise CockpitRoutingError("handoff source does not match authenticated requesting user")
        if handoff_target == source_user:
            raise CockpitRoutingError("cross-user handoff target must differ from source user")
        if getattr(args, "prior_context", None):
            raise CockpitRoutingError("cross-user handoff cannot copy raw prior context or transcripts")
        audience = handoff_target
    else:
        audience = source_user or "chris"

    cockpit_board = resolve_cockpit_board(audience) if is_cockpit and audience in COCKPIT_USER_BOARDS else None
    explicit_board = str(getattr(args, "board", None) or "").strip() or None
    if is_cockpit:
        if cockpit_board is None:
            raise CockpitRoutingError("cockpit audience does not resolve to a configured board")
        if explicit_board and explicit_board != cockpit_board:
            raise CockpitRoutingError(
                f"cockpit user/audience resolves to board {cockpit_board!r}, not {explicit_board!r}"
            )
        board = cockpit_board
    else:
        if not explicit_board:
            raise CockpitRoutingError("non-cockpit delegation requires an explicit --board")
        board = explicit_board
    return board, source_user, audience, cockpit_board, session_key


def csv_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    cleaned: list[str] = []
    for value in values:
        for item in str(value).split(","):
            normalized = slugify(item.strip().replace("/", "_"), "").replace("-", "_")
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
    return cleaned


def first_lines(value: str, limit: int = 4) -> list[str]:
    lines = [line.strip(" -\t") for line in value.splitlines() if line.strip()]
    return lines[:limit]


def parse_source_surface(source_surface: str, tenant: str) -> dict[str, Any]:
    surface = source_surface or tenant or "manual"
    platform = "other"
    chat_id = None
    topic_id = None
    if surface == COCKPIT_VOICE_SOURCE_SURFACE:
        platform = "agent_os"
    elif surface.startswith("mission_control"):
        platform = "mission_control"
    elif surface.startswith("cron"):
        platform = "cron"
    elif surface.startswith("manual"):
        platform = "manual"
    else:
        match = TELEGRAM_SURFACE_RE.match(str(surface).strip()) or TELEGRAM_SURFACE_RE.match(str(tenant or "").strip())
        if match:
            platform = "telegram"
            chat_id = match.group("chat")
            topic_id = match.group("thread")
    return {
        "platform": platform,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "source_surface": surface,
        "source_ref": None,
        "tenant": tenant,
    }


def package_id_for(args: argparse.Namespace, route: ResolvedRoute) -> str:
    explicit = args.package_id or args.idempotency_key
    if explicit:
        seed = "|".join([str(explicit), route.audience, route.board])
        return f"{explicit}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
    seed = "|".join(
        [route.audience, route.board, route.tenant, route.title, route.department_id, route.source_surface]
    )
    return "delegate:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def infer_complexity_gate(args: argparse.Namespace, route: ResolvedRoute) -> ComplexityGate:
    complexity = slugify((args.complexity or "simple").strip().lower(), "simple")
    if complexity not in {"simple", "standard", "complex", "critical"}:
        raise ValueError("--complexity must be one of simple, standard, complex, critical")

    reasons = csv_values(args.complexity_reason)
    privacy = slugify(route.privacy_tier or "normal")
    if privacy in SENSITIVE_PRIVACY_TIERS and "sensitive" not in reasons and not getattr(args, "canonical_topic_intake", False):
        reasons.append("sensitive")
    if route.approval_gate and slugify(route.approval_gate).replace("-", "_") in HUMAN_DECISION_GATES and "external_action" not in reasons:
        reasons.append("external_action")
    if route.proposed_new_department and "ambiguous" not in reasons:
        reasons.append("ambiguous")

    required_reason = None
    if complexity in {"complex", "critical"}:
        required_reason = complexity
    else:
        matched = sorted(set(reasons) & CLAIRE_SOL_GATE_REASONS)
        if matched:
            required_reason = matched[0]

    metadata_satisfied_by = None
    author = slugify((args.package_author or "").strip().lower(), "")
    author_tier = slugify((args.package_author_tier or "").strip().lower(), "")
    reviewed_by = slugify((args.package_reviewed_by or "").strip().lower(), "")
    reviewer_tier = slugify((args.package_reviewer_tier or "").strip().lower(), "")
    if author in SOL_PACKAGE_AUTHORS and author_tier == "sol":
        metadata_satisfied_by = f"package_author:{args.package_author}/sol"
    elif reviewed_by in SOL_PACKAGE_AUTHORS and reviewer_tier == "sol":
        metadata_satisfied_by = f"package_review:{args.package_reviewed_by}/sol"
    elif route.assignee == "default" and route.department_id == "chief_of_staff":
        if author in SOL_COMMAND_AUTHORS and author_tier == "sol":
            metadata_satisfied_by = f"private_command_author:{args.package_author}/sol"
        elif reviewed_by in SOL_COMMAND_AUTHORS and reviewer_tier == "sol":
            metadata_satisfied_by = f"private_command_review:{args.package_reviewed_by}/sol"

    # A named department head can package its own work. Claire is not a
    # mandatory intermediate planner; cross-department identity stays guarded.
    if not metadata_satisfied_by:
        policy = load_registry().get("model_policy") or {}
        for candidate, tier in ((author, author_tier), (reviewed_by, reviewer_tier)):
            pair = SOL_OWNER_TERRA_MAKER_DEPARTMENTS.get(candidate)
            if (
                tier in {"astra", "sol"}
                and (policy.get("profiles") or {}).get(candidate) == "department_owner"
                and pair is not None
                and route.department_id == pair[0]
                and route.assignee in {candidate, pair[1]}
            ):
                metadata_satisfied_by = f"department_owner:{candidate}"
                break

    requires = required_reason is not None
    # 2026-09-25 (Chris): package-author provenance is informational only.
    # Sensitive/complex work is protected by the privacy lane, the department
    # owner, and the existing human-action approvals, not by a Claire/Sol stamp.
    if requires and not metadata_satisfied_by:
        metadata_satisfied_by = f"department_direct:{route.assignee}"
    if False:
        raise ValueError(
            "Claire/Sol package author or review metadata is required before dispatch "
            f"for {required_reason}; pass --package-author claire --package-author-tier sol "
            "or --package-reviewed-by claire --package-reviewer-tier sol"
        )

    return ComplexityGate(
        complexity=complexity,
        complexity_reasons=reasons,
        requires_claire_sol=requires,
        required_reason=required_reason,
        metadata_satisfied_by=metadata_satisfied_by,
    )


def producer_kind_for(args: argparse.Namespace) -> str:
    kind = slugify(str(getattr(args, "producer_kind", "model") or "model"), "model").replace("-", "_")
    if kind not in PRODUCER_KINDS:
        raise ValueError(f"invalid producer kind: {kind!r}")
    return kind


def owner_acceptance_mode(args: argparse.Namespace) -> bool:
    return getattr(args, "routing_contract", None) == OWNER_ACCEPTANCE_ROUTING_CONTRACT


def validate_owner_acceptance_topology_args(args: argparse.Namespace) -> None:
    if not owner_acceptance_mode(args):
        return
    incompatible = []
    if getattr(args, "with_review", False):
        incompatible.append("--with-review")
    if getattr(args, "parent", None):
        incompatible.append("--parent")
    if getattr(args, "legal_inbound_chain", False):
        incompatible.append("--legal-inbound-chain")
    if getattr(args, "coordination_package_chain", False):
        incompatible.append("--coordination-package-chain")
    if incompatible:
        raise ValueError(
            "single-root owner-acceptance routing forbids child/parent topology flags: "
            + ", ".join(incompatible)
        )


def owner_acceptance_contract_for(args: argparse.Namespace, route: ResolvedRoute) -> dict[str, Any] | None:
    if not owner_acceptance_mode(args):
        return None
    owner = str(getattr(args, "accountable_owner_profile", None) or "").strip().lower()
    maker = str(route.assignee or "").strip().lower()
    spawnable = spawnable_profiles()
    if not owner:
        raise ValueError("single-root owner-acceptance routing requires --accountable-owner-profile")
    if owner not in spawnable:
        raise ValueError(f"accountable owner profile is not spawnable: {owner!r}")
    if owner not in SOL_OWNER_PROFILES:
        raise ValueError(f"accountable owner profile must be a canonical Sol owner: {owner!r}")
    if maker not in spawnable:
        raise ValueError(f"Terra maker profile is not spawnable: {maker!r}")
    expected_department, expected_maker = SOL_OWNER_TERRA_MAKER_DEPARTMENTS[owner]
    # 2026-09-25 (Chris): a department head may be its own maker. Heads have
    # Margot-level tools and farm grunt work to Sol sub-agents themselves.
    if maker == owner:
        expected_maker = owner
    if maker != expected_maker:
        raise ValueError(
            "single-root routing requires the approved Sol-owner/Terra-maker pair: "
            f"{owner!r} -> {expected_maker!r}; got {maker!r}"
        )
    if route.department_id != expected_department:
        raise ValueError(
            "single-root routing requires the canonical department for its owner/maker pair: "
            f"{owner!r} / {maker!r} -> {expected_department!r}; got {route.department_id!r}"
        )

    registry = load_registry()
    contract = deepcopy(registry.get("routing_contract") or {})
    owner_cell = deepcopy(registry.get("owner_cell_v1") or {})
    owner_cell.update({
        "enforce_verdict_gates": True,
        "suppress_internal_notifications": True,
        "worker_budget": {"producers": 1, "reviewers": 0, "extras": 0},
    })
    contract.update({
        "mode": OWNER_ACCEPTANCE_ROUTING_CONTRACT,
        "department_id": expected_department,
        "accountable_owner_profile": owner,
        "maker_profile": maker,
        "required_acceptor_profile": owner,
        "owner_tier": "sol",
        "maker_tier": "terra",
        "topology": "single_root",
        "max_child_depth": 0,
        "peer_review_child_cards": False,
        "sol_review_child_cards": False,
        "subscription_scope": "root_only",
        "report_back_scope": "root_only",
        "owner_cell_v1": owner_cell,
        **(
            {"required_secondary_reviewer_profile": "fable-review"}
            if expected_department in FABLE_SECONDARY_REVIEW_DEPARTMENTS
            else {}
        ),
    })
    return contract


def canonical_owner_acceptance_metadata(
    routing_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project the canonical single-root contract into durable task metadata."""
    contract = routing_contract if isinstance(routing_contract, Mapping) else {}
    if contract.get("mode") != OWNER_ACCEPTANCE_ROUTING_CONTRACT:
        return {}
    owner = str(contract.get("accountable_owner_profile") or "").strip().lower()
    maker = str(contract.get("maker_profile") or "").strip().lower()
    reviewer = str(contract.get("required_acceptor_profile") or "").strip().lower()
    secondary_reviewer = str(contract.get("required_secondary_reviewer_profile") or "").strip().lower()
    return {
        "owner_acceptance": {"mode": "accountable_owner"},
        "accountable_owner_profile": owner,
        "maker_profile": maker,
        "runtime_profile": maker,
        "required_acceptor_profile": reviewer,
        "required_reviewer_profile": reviewer,
        "reviewer_profiles": [reviewer],
        "routing_contract": dict(contract),
        "department_id": contract.get("department_id"),
        "topology": contract.get("topology"),
        "max_child_depth": contract.get("max_child_depth"),
        "peer_review_child_cards": contract.get("peer_review_child_cards"),
        "sol_review_child_cards": contract.get("sol_review_child_cards"),
        "subscription_scope": contract.get("subscription_scope"),
        "report_back_scope": contract.get("report_back_scope"),
        **(
            {
                "required_secondary_reviewer_profile": secondary_reviewer,
                "secondary_reviewer_profiles": [secondary_reviewer],
            }
            if secondary_reviewer
            else {}
        ),
    }


def review_contract_for(
    args: argparse.Namespace,
    route: ResolvedRoute,
    gate: ComplexityGate,
    package_class: str,
) -> dict[str, Any]:
    """Describe the required maker-to-Sol review edge without creating cards."""
    producer_kind = producer_kind_for(args)
    if route.department_id == "legal_compliance" and route.assignee == "steven":
        return {
            "required": False,
            "dependency_required": False,
            "producer_kind": producer_kind,
            "maker_profile": "steven",
            "sol_reviewer_profile": None,
            "sol_reviewer_tier": "none",
            "peer_reviewer_profiles": [],
            "exemption": "direct_steven_senior_review",
            "trigger": "direct_steven_senior_review",
            "owner_acceptance_required": True,
            "required_acceptor_profile": "steven",
            "exact_artifact_attestation_required": True,
            "attestation_state": "pending",
            "secondary_review_required": True,
            "secondary_reviewer_profile": "fable-review",
            "secondary_review_stage": "after_steven_exact_artifact_review_before_final_report",
            "conditional_review_required": "predicate_driven_fail_closed",
            "conditional_reviewer_profile": "claude-review",
        }
    if owner_acceptance_mode(args):
        owner = str(getattr(args, "accountable_owner_profile", None) or "").strip().lower()
        secondary_reviewer = (
            "fable-review"
            if route.department_id in FABLE_SECONDARY_REVIEW_DEPARTMENTS
            else None
        )
        legal_first_pass = route.department_id == "legal_compliance" and route.assignee == "legal-drafter"
        return {
            "required": legal_first_pass,
            "dependency_required": legal_first_pass,
            "producer_kind": producer_kind,
            "maker_profile": route.assignee,
            "sol_reviewer_profile": "steven" if legal_first_pass else None,
            "sol_reviewer_tier": "sol" if legal_first_pass else "none",
            "peer_reviewer_profiles": [],
            "exemption": None if legal_first_pass else "owner_acceptance_on_root",
            "trigger": "legal_avery_to_steven_exact_artifact" if legal_first_pass else OWNER_ACCEPTANCE_ROUTING_CONTRACT,
            "owner_acceptance_required": True,
            "required_acceptor_profile": owner,
            "exact_artifact_attestation_required": legal_first_pass,
            "attestation_state": "pending" if legal_first_pass else None,
            "secondary_review_required": bool(secondary_reviewer),
            "secondary_reviewer_profile": secondary_reviewer,
            "secondary_review_stage": (
                "after_steven_exact_artifact_review_before_final_report"
                if legal_first_pass and secondary_reviewer
                else "after_owner_exact_artifact_review_before_final_report"
                if secondary_reviewer
                else None
            ),
            "conditional_review_required": "predicate_driven_fail_closed" if legal_first_pass else None,
            "conditional_reviewer_profile": "claude-review" if legal_first_pass else None,
        }
    exemption = producer_kind if producer_kind in REVIEW_EXEMPT_PRODUCER_KINDS else None
    consequential = bool(
        package_class == "decision"
        or gate.requires_claire_sol
        or gate.complexity in {"standard", "complex", "critical"}
    )
    durable = package_class == "deliverable" or "durable" in gate.complexity_reasons
    required = producer_kind == "model" and (durable or consequential)

    if getattr(args, "legal_inbound_chain", False):
        maker_profile = "legal-drafter"
        sol_reviewer_profile = "steven"
        required = True
        exemption = None
    else:
        maker_profile = route.assignee
        if route.department_id == "legal_compliance":
            sol_reviewer_profile = "steven"
        elif route.assignee == "claire":
            sol_reviewer_profile = "default"
        else:
            sol_reviewer_profile = "claire"

    return {
        "required": required,
        "dependency_required": required,
        "producer_kind": producer_kind,
        "maker_profile": maker_profile,
        "sol_reviewer_profile": sol_reviewer_profile if required else None,
        "sol_reviewer_tier": "sol" if required else "none",
        "peer_reviewer_profiles": route.reviewer_profiles,
        "exemption": exemption,
        "trigger": "legal_junior_to_steven" if getattr(args, "legal_inbound_chain", False) else (
            "durable_deliverable" if durable else "consequential_decision" if consequential else "none"
        ),
        **(
            {
                "required_acceptor_profile": "steven",
                "exact_artifact_attestation_required": True,
                "attestation_state": "pending",
                "secondary_review_required": True,
                "secondary_reviewer_profile": "fable-review",
                "secondary_review_stage": "after_steven_exact_artifact_review_before_final_report",
                "conditional_review_required": "predicate_driven_fail_closed",
                "conditional_reviewer_profile": "claude-review",
            }
            if route.department_id == "legal_compliance"
            else {}
        ),
    }


def legal_finality_contract_for(
    route: ResolvedRoute,
    review_contract: Mapping[str, Any],
    gates: list[str],
) -> dict[str, Any] | None:
    """Emit a fail-closed contract for the exact artifact Steven must attest."""
    if route.department_id != "legal_compliance":
        return None
    return {
        "state": "pending_steven_exact_artifact_attestation",
        "required_reviewer_profile": "steven",
        "required_reviewer_tier": "sol",
        "artifact_sha256_required": True,
        "artifact_version_required": True,
        "review_after_last_substantive_edit_required": True,
        "substantive_edit_invalidates_reviews": True,
        "maker_self_attestation_allowed": False,
        "fable_review": {
            "required": True,
            "profile": "fable-review",
            "stage": "after_steven_exact_artifact_review_before_final_report",
            "compact_redacted_packet_required": True,
            "recommendations_only": True,
        },
        "conditional_claude_review": {
            "profile": "claude-review",
            "policy": "predicate_driven_fail_closed",
            "distinct_from_fable": True,
        },
        "human_action_gate": {
            "required": True,
            "required_gate_codes": list(gates),
            "review_authorizes_action": False,
            "reviews_authorize_action": False,
            "required_after_reviews": True,
            "separate_explicit_approval_required": True,
        },
        "review_trigger": review_contract.get("trigger"),
    }


def fable_advisory_review_for(
    args: argparse.Namespace,
    route: ResolvedRoute,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Return an explicit-only, non-authoritative Fable advisory envelope."""
    enabled = route.assignee == "fable-review"
    if not enabled:
        return {
            "enabled": False,
            "manual_approved": False,
            "profile": "fable-review",
            "redacted_compact_packet_required": True,
            "recommendations_only": True,
        }

    source_ref = str(source.get("source_ref") or "")
    provenance = str(source.get("raw_provenance") or "")
    context_items = list(getattr(args, "prior_context", None) or [])
    if not source_ref.startswith("redacted://") or not provenance.startswith("redacted:"):
        raise ValueError("Fable advisory requires redacted source-ref and raw-provenance stubs")
    if len(context_items) > FABLE_MAX_ADVISORY_CONTEXT_ITEMS or sum(len(item) for item in context_items) > FABLE_MAX_ADVISORY_CONTEXT_CHARS:
        raise ValueError("Fable advisory packet exceeds compact redacted context bounds")
    return {
        "enabled": True,
        "manual_approved": bool(getattr(args, "premium_approved", False)),
        "profile": "fable-review",
        "redacted_compact_packet_required": True,
        "max_context_items": FABLE_MAX_ADVISORY_CONTEXT_ITEMS,
        "max_context_chars": FABLE_MAX_ADVISORY_CONTEXT_CHARS,
        "return_to_profile": "default" if route.reviewer_profiles == ["default"] else "claire",
        "recommendations_only": True,
        "forbidden_actions": ["execute", "sign", "send", "approve_legal_commitment"],
        "approval_status": "advisory_only_not_approved",
    }


def model_tier_for(args: argparse.Namespace, route: ResolvedRoute, gate: ComplexityGate) -> dict[str, str]:
    if gate.complexity == "critical":
        reasoning = "critical"
    elif gate.complexity == "complex" or route.assignee == "default" or gate.requires_claire_sol:
        reasoning = "high"
    elif gate.complexity == "standard" or route.reviewer_profiles:
        reasoning = "medium"
    else:
        reasoning = "low"

    explicit_sol_author = bool(args.package_author) and slugify(args.package_author_tier or "") == "sol"
    planner = "sol" if gate.requires_claire_sol or route.assignee == "default" or explicit_sol_author else "luna"
    if route.assignee in {"default", "claire"} or (
        route.department_id == "legal_compliance" and route.assignee == "steven"
    ):
        executor = "sol"
    elif route.assignee == "claude-review":
        executor = "claude"
    else:
        executor = "terra"

    # Keep the named reviewer in ``assignee.reviewer_profile(s)``. This field
    # carries only a tier/provider class so downstream consumers never need to
    # infer a model class from a profile name.
    reviewer_profile = route.reviewer_profiles[0] if route.reviewer_profiles else "none"
    if reviewer_profile in {"claude-review", "fable-review"}:
        reviewer = "claude"
    elif reviewer_profile in {"default", "claire", "steven", "sol"}:
        reviewer = "sol"
    elif reviewer_profile in {"", "none", "null"}:
        reviewer = "none"
    else:
        reviewer = "terra"

    return {
        "planner": planner,
        "executor": executor,
        "reviewer": reviewer,
        "reasoning_required": reasoning,
        "model_notes": (
            "Luna may only classify simple reversible work; Terra executes specialist tasks; "
            "Sol/Claude review remains required for critical or consequential approval."
        ),
    }


def validate_fallback_gate(args: argparse.Namespace, gate: ComplexityGate, model_tier: dict[str, str]) -> dict[str, Any]:
    active = bool(args.fallback_degraded_mode)
    approval_status = slugify((args.approval_status or "not_applicable").strip().lower(), "not_applicable")
    if active and (gate.complexity == "critical" or model_tier.get("reasoning_required") == "critical"):
        if approval_status in CRITICAL_APPROVED_STATUSES:
            raise ValueError("fallback-degraded lower-tier mode cannot approve a critical Sol/Claude verdict")
        approval_status = "not_approved_degraded_mode"
    return {
        "active": active,
        "fallback_reason": args.fallback_reason if active else None,
        "allowed_output": args.fallback_allowed_output if active else None,
        "cannot_approve": [
            "critical_verdict",
            "legal_finance_personnel_security",
            "external_facing_final",
            "production_change",
        ] if active else [],
        "approval_status": approval_status,
    }


def configured_model_routing(package: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve current models separately from persisted legacy role aliases.

    Older packages remain valid. New policy-backed packages name the actual
    configured profile/model and fail closed on profile-policy drift. This is
    configuration evidence, never a claim about an inference-time fallback.
    """
    policy = load_registry().get("model_policy") or {}
    if not policy:
        return {}
    profiles = policy.get("profiles") or {}
    models = policy.get("models") or {}
    assignee = package.get("assignee") or {}
    contract = package.get("routing_contract") or {}
    planner = contract.get("accountable_owner_profile") or package.get("created_by")
    reviewer = contract.get("required_acceptor_profile") or (package.get("review_contract") or {}).get("sol_reviewer_profile") or assignee.get("reviewer_profile")
    bindings = {"planner": planner, "executor": assignee.get("canonical_spawnable_assignee"), "reviewer": reviewer}
    result = {"policy_version": policy["version"], "semantics": "configured_models_not_runtime_receipts", "assignments": {}}
    for phase, profile in bindings.items():
        profile = str(profile or "").split("/", 1)[0]
        role = profiles.get(profile)
        if role is None:
            continue  # Preserve explicitly separate utility/premium lanes.
        expected = models.get(role)
        if not expected or not policy.get("provider"):
            raise ValueError("model policy has an incomplete role assignment")
        home = HERMES_HOME if profile == "default" else HERMES_HOME / "profiles" / profile
        try:
            text = (home / "config.yaml").read_text()
            config = yaml.safe_load(text) if yaml is not None else json.loads(text)
            configured = config["model"]
            actual_model = configured["default"]
            actual_provider = configured["provider"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"model policy cannot verify profile {profile!r}") from exc
        if actual_model != expected or actual_provider != policy["provider"]:
            raise ValueError(f"model policy drift for profile {profile!r}")
        result["assignments"][phase] = {"profile": profile, "role": role, "model": expected, "provider": actual_provider}
    return result


def build_delegation_package(args: argparse.Namespace, route: ResolvedRoute) -> dict[str, Any]:
    gate = infer_complexity_gate(args, route)
    model_tier = model_tier_for(args, route, gate)
    fallback = validate_fallback_gate(args, gate, model_tier)
    source = parse_source_surface(route.source_surface, route.tenant)
    source["source_ref"] = args.source_ref
    source["privacy_lane"] = route.privacy_tier
    source["raw_provenance"] = args.raw_provenance or "provenance_stub:minimal-retention"
    source["requesting_user"] = route.requesting_user
    source["audience"] = route.audience
    source["cockpit_board"] = route.cockpit_board
    source["source_session_key"] = route.source_session_key
    source["return_surface"] = route.return_surface

    acceptance = args.acceptance or first_lines(args.body or route.title) or [f"Complete: {route.title}"]
    evidence = args.evidence or ["Kanban completion metadata includes concrete files, commands, artifacts, or readbacks."]
    reviewer_profile = route.reviewer_profiles[0] if route.reviewer_profiles else "none"
    gates = csv_values(args.gate) or ([route.approval_gate] if route.approval_gate else [])
    gates = [slugify(g).replace("-", "_") for g in gates if g]
    if route.department_id == "legal_compliance":
        legal_text = f"{args.title}\n{args.body or ''}".lower()
        required_legal_gates = [
            "legal_commitment",
            "signature",
            "counsel_send",
            "client_send",
            "external_send",
        ]
        if has_route_word(legal_text, ("filing", "file with")):
            required_legal_gates.append("filing")
        if has_route_word(legal_text, ("payment", "pay", "fee")):
            required_legal_gates.append("payment")
        for required_gate in required_legal_gates:
            if required_gate not in gates:
                gates.append(required_gate)
    unknown_gates = sorted(set(gates) - HUMAN_DECISION_GATES)
    if unknown_gates:
        raise ValueError(
            "approval gates must identify destructive, irreversible, external, costly, privacy/security, "
            f"legal, finance, production, or personnel actions; unrecognized={unknown_gates}"
        )
    allowed_toolsets = csv_values(args.allowed_toolset) or DEFAULT_ALLOWED_TOOLSETS
    default_allowed, default_forbidden = default_side_effect_policy(route)
    # Explicit holds ADD restrictions; they never erase the real gates.
    forbidden_actions = list(default_forbidden)
    for hold in csv_values(args.forbidden_action) or []:
        if hold not in forbidden_actions:
            forbidden_actions.append(hold)
    if route.department_id == "legal_compliance":
        for forbidden in (
            "external_send",
            "payment",
            "signature",
            "legal_advice",
            "logged_in_account_action",
            "broad_retention",
            "raw_privileged_disclosure",
        ):
            if forbidden not in forbidden_actions:
                forbidden_actions.append(forbidden)
    package_class = str(getattr(args, "package_class", "status") or "status").strip().lower()
    if package_class not in VALID_PACKAGE_CLASSES:
        raise ValueError(f"invalid package class: {package_class!r}")
    owner_acceptance_contract = owner_acceptance_contract_for(args, route)
    review_contract = review_contract_for(args, route, gate, package_class)
    legal_finality_contract = legal_finality_contract_for(route, review_contract, gates)
    if owner_acceptance_contract:
        model_tier.update({"planner": "sol", "executor": "terra", "reviewer": "sol"})
        reviewer_profile = owner_acceptance_contract["required_acceptor_profile"]
    elif review_contract["required"]:
        model_tier["reviewer"] = "sol"
    fable_advisory_review = fable_advisory_review_for(args, route, source)
    sensitivities = csv_values(getattr(args, "sensitivity", None))
    invalid_sensitivities = sorted(set(sensitivities) - APPROVAL_SENSITIVITIES)
    if invalid_sensitivities:
        raise ValueError(f"invalid decision sensitivities: {invalid_sensitivities}")
    decision_approval_packet_required = package_class == "decision"
    if decision_approval_packet_required:
        if not gates:
            raise ValueError("decision class requires at least one genuine human-action gate")
        if not sensitivities:
            raise ValueError("decision class requires --sensitivity")
        for field, value in (
            ("exact-action", getattr(args, "exact_action", None)),
            ("risk-cost-summary", getattr(args, "risk_cost_summary", None)),
            ("resume-target", getattr(args, "resume_target", None)),
        ):
            if not str(value or "").strip():
                raise ValueError(f"decision class requires --{field}")
    elif sensitivities or any(
        str(getattr(args, field, None) or "").strip()
        for field in ("exact_action", "risk_cost_summary", "resume_target")
    ):
        raise ValueError("approval-shaped fields are valid only for decision-class packages")
    allowed_side_effects = csv_values(args.allowed_side_effect) or default_allowed
    # Explicit holds always win over the broad defaults.
    allowed_side_effects = [e for e in allowed_side_effects
                            if not any(_HOLD_TO_SIDE_EFFECT.get(h) == e for h in forbidden_actions)]
    package_id = package_id_for(args, route)
    duplicate_check = args.duplicate_check or f"kanban board={route.board} idempotency_key={args.idempotency_key or package_id}"

    handoff = None
    if getattr(args, "handoff_to_user", None):
        if not str(getattr(args, "handoff_reason", None) or "").strip():
            raise ValueError("cross-user handoff requires --handoff-reason")
        if not str(getattr(args, "suggested_ask", None) or "").strip():
            raise ValueError("cross-user handoff requires --suggested-ask")
        handoff = {
            "kind": "cross_user",
            "fromUser": route.requesting_user,
            "toUser": route.audience,
            "sourcePackageId": getattr(args, "handoff_source_package_id", None),
            "sourceArtifactId": getattr(args, "handoff_source_artifact_id", None),
            "sourceTaskId": getattr(args, "handoff_source_task_id", None),
            "sourceBoard": resolve_cockpit_board(route.requesting_user),
            "transferredAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "transferReason": args.handoff_reason,
            "suggestedAsk": args.suggested_ask,
        }
        handoff = {key: value for key, value in handoff.items() if value not in (None, "")}
        if not any(handoff.get(key) for key in ("sourcePackageId", "sourceArtifactId", "sourceTaskId")):
            raise ValueError("cross-user handoff requires source package, artifact, or task provenance")

    result = {
        "delegation_package_version": 1,
        "package_id": package_id,
        "requesting_user": route.requesting_user,
        "audience": route.audience,
        "package_class": package_class,
        "class": package_class,
        "cockpit_board": route.cockpit_board,
        "source_session_key": route.source_session_key,
        "source_surface": route.source_surface,
        "return_surface": route.return_surface,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        # Preserve real provenance. A missing package author must not be
        # silently relabeled as Claire or Luna just because routing inferred a
        # complexity tier; author/review metadata stays separate below.
        "created_by": args.package_author or os.environ.get("HERMES_PROFILE") or "manual",
        "package_author_tier": args.package_author_tier if args.package_author else None,
        "package_reviewed_by": args.package_reviewed_by,
        "package_reviewer_tier": args.package_reviewer_tier,
        "chain_contract": (
            "legal_steven_drafter_steven"
            if getattr(args, "legal_inbound_chain", False)
            else "claire_package_then_worker"
            if getattr(args, "coordination_package_chain", False)
            else None
        ),
        "chain_steps": (
            [
                {"profile": "steven", "tier": "sol", "role": "package_author"},
                {"profile": "legal-drafter", "tier": "terra", "role": "draft_maker", "display_identity": "Avery"},
                {"profile": "steven", "tier": "sol", "role": "exact_artifact_reviewer"},
            ]
            if getattr(args, "legal_inbound_chain", False)
            else []
        ),
        "complexity_gate": asdict(gate),
        "source": source,
        "user_goal": {
            "user_facing_request": args.user_request or route.title,
            "operational_goal": args.operational_goal or route.title,
            "non_goals": args.non_goal or [],
        },
        "classification": {
            "department_owner": route.department_id,
            "department_name": route.department_name,
            "role_classification_reason": route.route_reason,
            "complexity": gate.complexity,
            "complexity_reasons": gate.complexity_reasons,
        },
        "assignee": {
            "canonical_spawnable_assignee": route.assignee,
            "human_facing_owner": route.owner_name,
            "reviewer_profile": reviewer_profile,
            "reviewer_profiles": [] if owner_acceptance_contract else route.reviewer_profiles,
            **({"required_acceptor_profile": owner_acceptance_contract["required_acceptor_profile"]} if owner_acceptance_contract else {}),
        },
        # Small top-level projections keep callback/Desk consumers from having
        # to infer the nested schema. Canonical values remain in their contract
        # sections below.
        "approval_gate": route.approval_gate,
        "reviewer_profile": reviewer_profile,
        "review_contract": review_contract,
        "fable_advisory_review": fable_advisory_review,
        "model_tier": model_tier,
        "workspace_and_tools": {
            "workspace_kind": args.workspace_kind,
            "workspace_path": args.workspace_path,
            "allowed_toolsets": allowed_toolsets,
            "forbidden_actions": forbidden_actions,
            "allowed_side_effects": allowed_side_effects,
        },
        "source_materials": {
            "files": args.source_file or [],
            "urls": args.source_url or [],
            "task_refs": args.task_ref or [],
            "prior_context": args.prior_context or [],
        },
        "acceptance_criteria": acceptance,
        "verification": {
            "required_evidence": evidence,
            "evidence_location": args.evidence_location or "kanban completion metadata/comment",
            "reviewer_instructions": args.reviewer_instructions or "Inspect the actual artifact/evidence before passing review.",
        },
        "irreversible_external_action_gates": {
            "required": bool(gates),
            "gates": gates,
            "approval_owner": (args.approval_owner or route.audience) if gates else None,
            "approval_packet_required": decision_approval_packet_required,
            "review_authorizes_action": False,
            "sensitivity": sensitivities,
            "exactAction": getattr(args, "exact_action", None) if decision_approval_packet_required else None,
            "riskCostSummary": getattr(args, "risk_cost_summary", None) if decision_approval_packet_required else None,
            "resumeTarget": getattr(args, "resume_target", None) if decision_approval_packet_required else None,
        },
        "return_callback": {
            "return_surface": route.return_surface,
            "callback_destination": args.callback_destination or route.return_surface,
            "report_style": args.report_style,
            "include_metadata": True,
            **({
                "subscription_scope": "root_only",
                "report_back_scope": "root_only",
                "report_back_task": "root",
            } if owner_acceptance_contract else {}),
        },
        "idempotency_and_dedupe": {
            "idempotency_key": package_id,
            "duplicate_check": duplicate_check,
            "supersedes": args.supersedes or [],
        },
        "bounds": {
            "timebox_minutes": args.timebox_minutes,
            "token_budget": args.token_budget,
            "runtime_cap_seconds": args.runtime_cap_seconds,
            "max_review_loops": args.max_review_loops,
        },
        "escalation": {
            "escalate_to": args.escalate_to or ("Claire" if gate.requires_claire_sol else "Margot"),
            "triggers": csv_values(args.escalation_trigger) or [
                "legal_financial_personnel_privacy_security",
                "final_verdict",
                "fallback_degraded_mode",
            ],
        },
        "fallback_degraded_mode": fallback,
        **(
            {
                "legal_finality_contract": legal_finality_contract,
                "accountable_owner_profile": "steven",
                "required_acceptor_profile": "steven",
                "required_reviewer_profile": "steven",
                "required_secondary_reviewer_profile": "fable-review",
                "secondary_reviewer_profiles": ["fable-review"],
                "required_human_approval_gates": list(gates),
                **({"maker_profile": "legal-drafter"} if route.assignee == "legal-drafter" else {}),
            }
            if legal_finality_contract
            else {}
        ),
        **canonical_owner_acceptance_metadata(owner_acceptance_contract),
    }
    if handoff:
        result["handoff"] = handoff
    model_routing = configured_model_routing(result)
    if model_routing:
        result["model_tier_semantics"] = "legacy_role_aliases_v1"
        result["model_routing"] = model_routing
    return result


def delegation_callback_payload(package: dict[str, Any]) -> dict[str, Any]:
    """Project routing/approval metadata that must survive a return handoff.

    The projection intentionally excludes full source material and user-goal
    content. It can be copied into a Desk record, callback fixture, or reviewer
    task without widening sensitive-source retention.
    """
    assignee = package.get("assignee") or {}
    gates = package.get("irreversible_external_action_gates") or {}
    return {
        "package_id": package.get("package_id"),
        "requesting_user": package.get("requesting_user"),
        "audience": package.get("audience"),
        "package_class": package.get("package_class"),
        "class": package.get("class"),
        "cockpit_board": package.get("cockpit_board"),
        "source_session_key": package.get("source_session_key"),
        "source_surface": package.get("source_surface"),
        "return_surface": package.get("return_surface"),
        "handoff": package.get("handoff"),
        "source": package.get("source") or {},
        "return_callback": package.get("return_callback") or {},
        "approval_gate": package.get("approval_gate"),
        "approval_gates": gates,
        "reviewer_profile": package.get("reviewer_profile") or assignee.get("reviewer_profile"),
        "review_contract": package.get("review_contract") or {},
        "legal_finality_contract": package.get("legal_finality_contract") or {},
        "accountable_owner_profile": package.get("accountable_owner_profile"),
        "required_acceptor_profile": package.get("required_acceptor_profile"),
        "required_secondary_reviewer_profile": package.get("required_secondary_reviewer_profile"),
        "routing_contract": package.get("routing_contract") or {},
        "model_routing": package.get("model_routing") or {},
        "model_tier_semantics": package.get("model_tier_semantics"),
        "fable_advisory_review": package.get("fable_advisory_review") or {},
        "fallback_degraded_mode": package.get("fallback_degraded_mode") or {},
    }


def validate_delegation_package(package: dict[str, Any], route: ResolvedRoute) -> list[str]:
    """Fail closed before dry-run/execute output if the delegation package is unsafe."""
    errors: list[str] = []
    if package.get("model_routing"):
        try:
            if package["model_routing"] != configured_model_routing(package):
                errors.append("configured model routing does not match the canonical profile policy")
        except ValueError as exc:
            errors.append(str(exc))
    required_fields = [
        "source", "user_goal", "classification", "assignee", "model_tier",
        "workspace_and_tools", "source_materials", "acceptance_criteria", "verification",
        "irreversible_external_action_gates", "return_callback", "idempotency_and_dedupe",
        "review_contract", "fable_advisory_review", "bounds", "escalation", "fallback_degraded_mode",
    ]
    for field in required_fields:
        if package.get(field) in (None, {}, []):
            errors.append(f"missing delegation package field: {field}")
    if not str(package.get("package_id") or "").strip():
        errors.append("missing delegation package field: package_id")
    if package.get("audience") != route.audience:
        errors.append("delegation package audience does not match resolved route")
    if package.get("requesting_user") != route.requesting_user:
        errors.append("delegation package requesting_user does not match resolved route")
    if package.get("cockpit_board") != route.cockpit_board:
        errors.append("delegation package cockpit_board does not match resolved route")
    if package.get("source_session_key") != route.source_session_key:
        errors.append("delegation package source_session_key does not match resolved route")
    if package.get("package_class") not in VALID_PACKAGE_CLASSES or package.get("class") != package.get("package_class"):
        errors.append("delegation package requires a valid, consistent class")
    if route.cockpit_board and route.board != route.cockpit_board:
        errors.append("resolved cockpit board does not match task board")
    source = package.get("source") or {}
    for field in ("platform", "chat_id", "topic_id", "tenant", "privacy_lane", "raw_provenance"):
        if field not in source:
            errors.append(f"missing delegation package field: source.{field}")
    for field in ("platform", "source_surface", "tenant", "privacy_lane", "raw_provenance"):
        if not str(source.get(field) or "").strip():
            errors.append(f"missing delegation package value: source.{field}")
    for field in ("requesting_user", "audience", "cockpit_board", "source_session_key", "return_surface"):
        if route.requesting_user is not None and source.get(field) in (None, ""):
            errors.append(f"missing cockpit source metadata: source.{field}")
    if source.get("audience") != route.audience or source.get("cockpit_board") != route.cockpit_board:
        errors.append("source audience/board does not match resolved route")
    callback = package.get("return_callback") or {}
    for field in ("return_surface", "callback_destination"):
        if not str(callback.get(field) or "").strip():
            errors.append(f"missing return/callback surface: return_callback.{field}")
    if not route.return_surface:
        errors.append("missing return/callback surface")
    spawnable = spawnable_profiles()
    assignee = (package.get("assignee") or {}).get("canonical_spawnable_assignee")
    if assignee not in spawnable:
        errors.append(f"nonspawnable assignee: {assignee}")
    normalized_assignee = str(assignee or "").strip().lower()
    assignee_meta = package.get("assignee") or {}
    reviewer = str(assignee_meta.get("reviewer_profile") or "none").strip().lower()
    reviewer_profiles = [
        str(profile).strip().lower()
        for profile in (assignee_meta.get("reviewer_profiles") or [])
        if str(profile).strip()
    ]
    routing_contract = package.get("routing_contract") or {}
    if routing_contract.get("mode") == OWNER_ACCEPTANCE_ROUTING_CONTRACT:
        owner = str(package.get("accountable_owner_profile") or "").strip().lower()
        expected_owner = str(routing_contract.get("required_acceptor_profile") or "").strip().lower()
        if owner != expected_owner or assignee_meta.get("required_acceptor_profile") != owner:
            errors.append("single-root owner acceptance requires one consistent accountable owner")
        if owner not in SOL_OWNER_PROFILES or owner not in spawnable:
            errors.append("single-root accountable owner must be a spawnable canonical Sol profile")
        expected_triple = SOL_OWNER_TERRA_MAKER_DEPARTMENTS.get(owner)
        expected_department, expected_maker = expected_triple or (None, None)
        contract_maker = str(routing_contract.get("maker_profile") or "").strip().lower()
        if normalized_assignee == owner and contract_maker == owner:
            expected_maker = owner
        if (
            not expected_maker
            or normalized_assignee != expected_maker
            or contract_maker != expected_maker
        ):
            errors.append("single-root routing requires the approved Sol-owner/Terra-maker pair")
        contract_department = str(routing_contract.get("department_id") or "").strip()
        package_department = str((package.get("classification") or {}).get("department_owner") or "").strip()
        if (
            not expected_department
            or route.department_id != expected_department
            or contract_department != expected_department
            or package_department != expected_department
        ):
            errors.append("single-root routing requires one consistent canonical department")
        if reviewer != owner or reviewer_profiles:
            errors.append("single-root owner acceptance requires owner as sole acceptor and no peer reviewers")
        expected = {
            "topology": "single_root",
            "max_child_depth": 0,
            "peer_review_child_cards": False,
            "sol_review_child_cards": False,
            "subscription_scope": "root_only",
            "report_back_scope": "root_only",
        }
        if any(routing_contract.get(key) != value for key, value in expected.items()):
            errors.append("single-root routing topology/report-back contract is inconsistent")
        callback_contract = package.get("return_callback") or {}
        if callback_contract.get("subscription_scope") != "root_only" or callback_contract.get("report_back_scope") != "root_only":
            errors.append("single-root owner acceptance requires root-only subscription and report-back")
    review_contract = package.get("review_contract") or {}
    if not isinstance(review_contract, dict):
        errors.append("review_contract must be an object")
        review_contract = {}
    review_required = bool(review_contract.get("required"))
    if review_required:
        sol_reviewer = str(review_contract.get("sol_reviewer_profile") or "").strip().lower()
        maker = str(review_contract.get("maker_profile") or "").strip().lower()
        if not review_contract.get("dependency_required"):
            errors.append("required maker-to-Sol review must declare a dependency edge")
        if review_contract.get("sol_reviewer_tier") != "sol":
            errors.append("required maker-to-Sol review must retain Sol reviewer tier")
        if sol_reviewer not in spawnable:
            errors.append(f"nonspawnable Sol reviewer profile: {sol_reviewer}")
        if maker == sol_reviewer and package.get("chain_contract") != "legal_steven_drafter_steven":
            errors.append("Sol reviewer must be independent from maker profile")
        if (package.get("model_tier") or {}).get("reviewer") != "sol":
            errors.append("model_tier.reviewer must be sol when maker-to-Sol review is required")
    elif review_contract.get("producer_kind") in REVIEW_EXEMPT_PRODUCER_KINDS:
        if review_contract.get("exemption") != review_contract.get("producer_kind"):
            errors.append("explicit review exemption must identify its deterministic producer kind")
    fable_advisory = package.get("fable_advisory_review") or {}
    if fable_advisory.get("enabled"):
        expected_forbidden = {"execute", "sign", "send", "approve_legal_commitment"}
        if not fable_advisory.get("manual_approved"):
            errors.append("Fable advisory must remain manual-approved")
        if fable_advisory.get("profile") != "fable-review":
            errors.append("Fable advisory must use the canonical fable-review profile")
        if not fable_advisory.get("redacted_compact_packet_required") or not fable_advisory.get("recommendations_only"):
            errors.append("Fable advisory must be compact/redacted and recommendations-only")
        if not expected_forbidden.issubset(set(fable_advisory.get("forbidden_actions") or [])):
            errors.append("Fable advisory must not execute, sign, send, or approve legal commitments")
        if not str(fable_advisory.get("return_to_profile") or "").strip():
            errors.append("Fable advisory requires an accountable Sol return profile")
    gate = package.get("complexity_gate") or {}
    gates = package.get("irreversible_external_action_gates") or {}
    gate_names = set(gates.get("gates") or [])
    if gate_names - HUMAN_DECISION_GATES:
        errors.append("delegation package contains an unrecognized human-action gate")
    if bool(gate_names) != bool(gates.get("required")):
        errors.append("human-action gate required flag must match whether gates are present")
    if gates.get("review_authorizes_action") is not False:
        errors.append("review completion must never authorize an irreversible external action")
    if package.get("package_class") == "decision":
        if not gates.get("required") or not gate_names:
            errors.append("decision package requires a genuine human-action gate")
        if not gates.get("sensitivity") or not gates.get("exactAction") or not gates.get("riskCostSummary") or not gates.get("resumeTarget"):
            errors.append("decision package requires sensitivity, exactAction, riskCostSummary, and resumeTarget")
    elif gates.get("approval_packet_required"):
        errors.append("routine deliverable/status package must not claim a decision approval packet")
    consequential = bool(
        gate.get("requires_claire_sol")
        or package.get("classification", {}).get("complexity") in {"standard", "complex", "critical"}
        or gates.get("required")
    )
    direct_steven_legal_review = route.department_id == "legal_compliance" and normalized_assignee == "steven"
    if consequential and reviewer in {"", "none", "null"} and not direct_steven_legal_review:
        errors.append("consequential output requires an independent reviewer_profile")
    if reviewer not in {"", "none", "null"} and reviewer not in spawnable and reviewer not in {"sol"}:
        errors.append(f"nonspawnable reviewer_profile: {reviewer}")
    legal_inbound_chain = package.get("chain_contract") == "legal_steven_drafter_steven"
    if legal_inbound_chain:
        expected_steps = [
            {"profile": "steven", "tier": "sol", "role": "package_author"},
            {"profile": "legal-drafter", "tier": "terra", "role": "draft_maker", "display_identity": "Avery"},
            {"profile": "steven", "tier": "sol", "role": "exact_artifact_reviewer"},
        ]
        if str(package.get("created_by") or "").strip().lower() != "steven":
            errors.append("legal inbound chain requires Steven as package author")
        if str(package.get("package_author_tier") or "").strip().lower() != "sol":
            errors.append("legal inbound chain requires the Steven/Sol package-author tier")
        if package.get("chain_steps") != expected_steps:
            errors.append("legal inbound chain must retain Sol -> Terra -> Sol execution steps")
        if not str(source.get("source_ref") or "").startswith("margot-intake://"):
            errors.append("legal inbound chain requires a private minimum-retention source handle")
        if route.privacy_tier != "legal":
            errors.append("legal inbound chain must retain the legal privacy wall")
        legal_gates = {"legal_commitment", "signature", "counsel_send", "external_send"}
        if not legal_gates.issubset(set(gates.get("gates") or [])):
            errors.append("legal inbound chain must retain every Chris/counsel approval gate")
        legal_forbidden = {
            "external_send",
            "payment",
            "signature",
            "legal_advice",
            "logged_in_account_action",
            "broad_retention",
            "raw_privileged_disclosure",
        }
        actual_forbidden = set((package.get("workspace_and_tools") or {}).get("forbidden_actions") or [])
        if not legal_forbidden.issubset(actual_forbidden):
            errors.append("legal inbound chain is missing mandatory junior forbidden actions")
        if (package.get("fable_advisory_review") or {}).get("enabled"):
            errors.append("Fable may not appear in the automatic legal inbound chain")
    if normalized_assignee and reviewer == normalized_assignee and not legal_inbound_chain:
        errors.append("reviewer_profile must be independent from canonical_spawnable_assignee")
    if normalized_assignee and normalized_assignee in reviewer_profiles and not legal_inbound_chain:
        errors.append("reviewer_profiles must not include canonical_spawnable_assignee")
    if route.department_id == "legal_compliance":
        if legal_inbound_chain and normalized_assignee == "steven" and (
            reviewer != "steven" or reviewer_profiles != ["steven"]
        ):
            errors.append("legal inbound chain requires Steven as package author and final senior reviewer")
        elif normalized_assignee == "legal-drafter":
            expected_legal_reviewers = [] if routing_contract.get("mode") == OWNER_ACCEPTANCE_ROUTING_CONTRACT else ["steven"]
            if reviewer != "steven" or reviewer_profiles != expected_legal_reviewers:
                errors.append("legal-drafter packets require Steven as the sole senior reviewer_profile")
            if (
                review_contract.get("required") is not True
                or review_contract.get("dependency_required") is not True
                or review_contract.get("maker_profile") != "legal-drafter"
                or review_contract.get("sol_reviewer_profile") != "steven"
                or review_contract.get("sol_reviewer_tier") != "sol"
            ):
                errors.append("Avery first-pass packets require a machine-enforced Steven/Sol review edge")
        elif normalized_assignee == "steven":
            if review_contract.get("trigger") != "direct_steven_senior_review":
                errors.append("direct senior legal review must retain the Steven-only review trigger")
            if package.get("maker_profile") == "legal-drafter":
                errors.append("direct senior legal review must not be relabeled as Avery first-pass drafting")
        else:
            errors.append("legal work must route to legal-drafter or direct Steven senior review")

        if route.privacy_tier != "legal" or source.get("privacy_lane") != "legal":
            errors.append("legal work must retain the sensitive legal privacy lane")
        expected_legal_gates = {
            "legal_commitment", "signature", "counsel_send", "client_send", "external_send"
        }
        if not expected_legal_gates.issubset(gate_names) or gates.get("required") is not True:
            errors.append("legal work must retain explicit required human action gates")
        if package.get("accountable_owner_profile") != "steven" or package.get("required_acceptor_profile") != "steven":
            errors.append("legal work requires Steven/Sol as the accountable exact-artifact acceptor")
        if package.get("required_secondary_reviewer_profile") != "fable-review":
            errors.append("legal work must retain the required compact redacted Fable review metadata")
        if review_contract.get("secondary_reviewer_profile") != "fable-review":
            errors.append("legal review contract must retain Fable after Steven review")
        if review_contract.get("conditional_reviewer_profile") != "claude-review":
            errors.append("legal review contract must retain distinct predicate-driven Claude review")
        if review_contract.get("exact_artifact_attestation_required") is not True or review_contract.get("attestation_state") != "pending":
            errors.append("legal finality must remain pending until exact-artifact Steven attestation")

        finality = package.get("legal_finality_contract") or {}
        finality_expected = {
            "state": "pending_steven_exact_artifact_attestation",
            "required_reviewer_profile": "steven",
            "required_reviewer_tier": "sol",
            "artifact_sha256_required": True,
            "artifact_version_required": True,
            "review_after_last_substantive_edit_required": True,
            "substantive_edit_invalidates_reviews": True,
            "maker_self_attestation_allowed": False,
        }
        if any(finality.get(key) != value for key, value in finality_expected.items()):
            errors.append("legal finality contract must fail closed on reviewer, tier, digest, version, and edits")
        finality_fable = finality.get("fable_review") or {}
        if (
            finality_fable.get("required") is not True
            or finality_fable.get("profile") != "fable-review"
            or finality_fable.get("compact_redacted_packet_required") is not True
        ):
            errors.append("legal finality contract must retain compact redacted Fable review")
        finality_claude = finality.get("conditional_claude_review") or {}
        if (
            finality_claude.get("profile") != "claude-review"
            or finality_claude.get("policy") != "predicate_driven_fail_closed"
            or finality_claude.get("distinct_from_fable") is not True
        ):
            errors.append("legal finality contract must keep conditional Claude separate from Fable")
        if (finality.get("human_action_gate") or {}).get("reviews_authorize_action") is not False:
            errors.append("Steven/Fable/Claude reviews must not authorize human-gated action")
    fallback = package.get("fallback_degraded_mode") or {}
    if route.department_id == "legal_compliance" and fallback.get("active"):
        if str(fallback.get("approval_status") or "").strip().lower() in CRITICAL_APPROVED_STATUSES:
            errors.append("degraded/lower-tier legal fallback cannot approve; Steven review remains required")
    complexity_reasons = set(package.get("classification", {}).get("complexity_reasons") or [])
    if fallback.get("active") and (
        package.get("classification", {}).get("complexity") == "critical" or "final_verdict" in complexity_reasons
    ):
        if str(fallback.get("approval_status") or "").strip().lower() in CRITICAL_APPROVED_STATUSES:
            errors.append("degraded-mode critical verdict approvals are not allowed")
    callback_payload = delegation_callback_payload(package)
    for field in ("package_id", "source", "return_callback", "reviewer_profile", "fallback_degraded_mode"):
        if callback_payload.get(field) in (None, "", {}):
            errors.append(f"missing callback metadata field: {field}")
    if route.requesting_user is not None:
        for field in ("audience", "cockpit_board", "source_session_key"):
            if callback_payload.get(field) in (None, ""):
                errors.append(f"missing audience-safe callback metadata field: {field}")
    handoff = package.get("handoff")
    if handoff is not None:
        allowed_handoff_fields = {
            "kind", "fromUser", "toUser", "sourcePackageId", "sourceArtifactId",
            "sourceTaskId", "sourceBoard", "transferredAt", "transferReason", "suggestedAsk",
        }
        if set(handoff) - allowed_handoff_fields:
            errors.append("cross-user handoff contains non-minimal provenance fields")
        if handoff.get("kind") != "cross_user" or handoff.get("fromUser") != route.requesting_user:
            errors.append("cross-user handoff source metadata is contradictory")
        if handoff.get("toUser") != route.audience or route.audience == route.requesting_user:
            errors.append("cross-user handoff recipient metadata is contradictory")
        if handoff.get("sourceBoard") != resolve_cockpit_board(route.requesting_user):
            errors.append("cross-user handoff source board is contradictory")
        if not handoff.get("suggestedAsk") or not handoff.get("transferReason"):
            errors.append("cross-user handoff requires a reason and suggestedAsk")
        if not any(handoff.get(field) for field in ("sourcePackageId", "sourceArtifactId", "sourceTaskId")):
            errors.append("cross-user handoff lacks minimum source provenance")
    return errors


def resolve_route(args: argparse.Namespace) -> ResolvedRoute:
    board, requesting_user, audience, cockpit_board, source_session_key = _resolve_lane_fields(args)
    if not BOARD_RE.match(board):
        raise ValueError(f"invalid board slug: {board!r}")
    boards = known_boards()
    if board not in boards:
        raise ValueError(f"unknown board '{board}'. Known boards: {', '.join(sorted(boards))}")

    registry = load_registry()
    spawnable = spawnable_profiles()
    role = find_role(registry, args.department)
    proposed_new = False

    dept_slug = slugify(args.department or "")
    text_for_route = f"{args.title}\n{args.body or ''}".lower()
    # `vivienne`/`finlegal` are legacy combined finance+legal labels. Keep them
    # accepted for compatibility, but split new/legal-looking work to Steven
    # instead of always falling through to Maya via the finance registry entry.
    if dept_slug in {"vivienne", "finlegal"} and has_route_word(text_for_route, FINANCE_ROUTE_WORDS):
        role = find_role(registry, "accountant_finance") or role
    elif dept_slug in {"vivienne", "finlegal"} and has_route_word(text_for_route, LEGAL_ROUTE_WORDS):
        role = find_role(registry, "legal_compliance") or role

    if role is None:
        # Unknown cockpit departments are never copied into task.assignee and
        # never fall through to a blank/default label. Route them through the
        # real Ops Control owner while preserving the requested department as
        # proposed-new metadata for later roster cleanup.
        role = find_role(registry, "ops_control") or find_role(registry, "chief_of_staff")
        proposed_new = True

    implicit_assignee = args.assignee

    role_id = str((role or {}).get("id") or slugify(args.department or "new-department"))
    legal_senior_review = role_id == "legal_compliance" and is_legal_senior_review_request(
        args.title, args.body or ""
    )
    if legal_senior_review and implicit_assignee and implicit_assignee.strip().lower() != "steven":
        raise ValueError("senior legal review must route directly to Steven/Sol, not a draft maker")
    canonical_owner_route = next(
        (
            (owner_profile, maker_profile)
            for owner_profile, (department_id, maker_profile) in SOL_OWNER_TERRA_MAKER_DEPARTMENTS.items()
            if department_id == role_id
        ),
        None,
    )
    # Installation-selected native execution keeps new tasks on their head.
    # Legacy task contracts remain immutable; this only selects new intake.
    from hermes_cli.owner_workflow import settings as owner_workflow_settings
    native_rule = (owner_workflow_settings().get("departments") or {}).get(role_id) or {}
    normalized_assignee = implicit_assignee.strip().lower() if implicit_assignee else None
    native_owner = (
        canonical_owner_route[0]
        if canonical_owner_route
        and native_rule.get("execution") == "native_owner"
        and native_rule.get("owner") == canonical_owner_route[0]
        and normalized_assignee in (None, *canonical_owner_route)
        else None
    )
    if legal_senior_review:
        # A review/revision/counsel-gate request is itself Steven's bounded
        # senior-review work. It must not be overwritten by the ordinary
        # Steven-owner -> legal-drafter first-pass maker contract.
        args.routing_contract = None
        args.accountable_owner_profile = "steven"
        implicit_assignee = "steven"
    elif native_owner:
        args.routing_contract = None
        args.accountable_owner_profile = native_owner
        implicit_assignee = native_owner
    elif (
        canonical_owner_route
        and getattr(args, "canonical_topic_intake", False)
        and implicit_assignee == canonical_owner_route[0]
    ):
        # Canonical topic ingress is an intake for the named head. Let that
        # head scope/consult/produce a guarded worker package; do not bind the
        # head as its own maker or silently skip the senior intake step.
        args.routing_contract = None
        args.accountable_owner_profile = canonical_owner_route[0]
    elif canonical_owner_route and not getattr(args, "legal_inbound_chain", False):
        expected_owner, expected_maker = canonical_owner_route
        args.routing_contract = OWNER_ACCEPTANCE_ROUTING_CONTRACT
        args.accountable_owner_profile = getattr(args, "accountable_owner_profile", None) or expected_owner
        implicit_assignee = implicit_assignee or expected_maker
    if role_id == "premium_strategy_review" and not getattr(args, "premium_approved", False):
        raise ValueError(
            "Fable 5 is an explicit premium escalation. Pass --premium-approved only after Chris explicitly requests Fable or approves the cost."
        )
    role_name = str((role or {}).get("name") or args.department or "New Department Intake")
    pod_info = {**CANONICAL_PODS.get(role_id, {}), **((role or {}).get("pod") or {})}
    owner = args.owner_name or str(pod_info.get("named_owner") or role_name.split("·", 1)[0].split("/", 1)[0].strip() or "Margot")
    producer_references = [
        *[str(value) for value in (getattr(args, "task_ref", None) or [])],
        *[str(value) for value in (getattr(args, "source_file", None) or [])],
    ]
    assignee, reason = choose_assignee(
        role,
        spawnable,
        args.title,
        args.body or "",
        implicit_assignee,
        producer_references,
    )
    if role_id == "chief_of_staff" and assignee == "default":
        specialist = command_specialist_for(text_for_route)
        if specialist and specialist in spawnable:
            assignee = specialist
            reason = f"private command intake routed substantive work to canonical specialist {specialist}"
    if role_id == "ops_control" and assignee == "claire" and is_routine_reversible_ops_request(args):
        if "lauren" not in spawnable:
            raise ValueError("routine Ops Control work requires canonical Terra executor lauren")
        assignee = "lauren"
        reason = "routine reversible Ops Control work routed to Terra admin executor; Claire remains package/review owner"
    pod = args.pod or str(pod_info.get("display_name") or f"{owner} {role_name.split('·', 1)[0].strip()} Pod")
    pod_slug = args.pod_slug or str(pod_info.get("slug") or slugify(pod))
    dept_slug = slugify(args.department or "")
    metrical_route = dept_slug in {"metrical", "metrical-records", "metrical-content", "metricool", "metricool-records", "metricool-content"}
    if metrical_route and not args.member_role:
        member_role = "Metrical Record/Content Updates"
        member_role_slug = args.member_role_slug or "metrical-record-content-updates"
    else:
        member_role = args.member_role or str(pod_info.get("default_owner_role") or "Route Owner")
        member_role_slug = args.member_role_slug or slugify(member_role, "route-owner")
    handoff_label = args.handoff_label or f"{pod_slug}/{member_role_slug}: own task"
    source_surface = args.source_surface or args.tenant
    return_surface = args.return_surface or default_return_surface(source_surface)
    verifier = args.verifier or str(pod_info.get("verifier_role") or f"{pod_slug}/verifier")
    review_matrix = ((registry.get("review_policy") or {}).get("matrix") or {})
    matrix_reviewers = review_matrix.get(role_id, [])
    if native_owner and role_id == 'personal_family' and not matrix_reviewers:
        # Match the existing private-family topic reviewer; do not fall back
        # to a company reviewer merely because its producer is now the head.
        matrix_reviewers = ['default']
    reviewer_profiles: list[str] = []
    for raw_profile in matrix_reviewers:
        profile = str(raw_profile).strip().lower()
        if profile in spawnable and profile != assignee and profile not in reviewer_profiles:
            reviewer_profiles.append(profile)
    if not reviewer_profiles and matrix_reviewers:
        fallback = next(
            (profile for profile in REVIEWER_FALLBACK_ORDER if profile in spawnable and profile != assignee),
            None,
        )
        if fallback:
            reviewer_profiles.append(fallback)
    # Steven is already the required senior legal reviewer when directly routed a
    # review/revision/counsel-gate task. Do not add a lower-tier substitute.
    if role_id == "legal_compliance" and assignee == "steven":
        reviewer_profiles = ["steven"] if getattr(args, "legal_inbound_chain", False) else []
    if owner_acceptance_mode(args):
        reviewer_profiles = []
    approval_gate = None if args.approval_gate in (None, "", "null", "none") else args.approval_gate

    return ResolvedRoute(
        board=board,
        requesting_user=requesting_user,
        audience=audience,
        cockpit_board=cockpit_board,
        source_session_key=source_session_key,
        tenant=args.tenant,
        title=args.title,
        assignee=assignee,
        owner_name=owner,
        department_id=role_id,
        department_name=role_name,
        pod=pod,
        pod_slug=pod_slug,
        member_role=member_role,
        member_role_slug=member_role_slug,
        handoff_label=handoff_label,
        source_surface=source_surface,
        return_surface=return_surface,
        privacy_tier="legal" if role_id == "legal_compliance" else args.privacy_tier,
        approval_gate=approval_gate,
        verifier=verifier,
        reviewer_profiles=reviewer_profiles,
        route_reason=reason,
        proposed_new_department=proposed_new,
    )


def body_with_metadata(body: str, route: ResolvedRoute, delegation_package: dict[str, Any]) -> str:
    meta = {
        "requesting_user": route.requesting_user,
        "audience": route.audience,
        "class": delegation_package.get("package_class"),
        "cockpit_board": route.cockpit_board,
        "source_session_key": route.source_session_key,
        "department_id": route.department_id,
        "department_name": route.department_name,
        "owner_name": route.owner_name,
        "pod": route.pod,
        "pod_slug": route.pod_slug,
        "member_role": route.member_role,
        "member_role_slug": route.member_role_slug,
        "handoff_label": route.handoff_label,
        "source_surface": route.source_surface,
        "return_surface": route.return_surface,
        "privacy_tier": route.privacy_tier,
        "approval_gate": route.approval_gate,
        "verifier": route.verifier,
        "reviewer_profiles": route.reviewer_profiles,
        "executable_assignee": route.assignee,
        "route_reason": route.route_reason,
        "proposed_new_department": route.proposed_new_department,
        **canonical_owner_acceptance_metadata(delegation_package.get("routing_contract")),
        "callback_payload": delegation_callback_payload(delegation_package),
        "delegation_package": delegation_package,
    }
    lines = [body.rstrip(), "", "## Routing metadata", "", "```json", json.dumps(meta, indent=2, sort_keys=True), "```", ""]
    return "\n".join(lines).lstrip()


def hermes_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    # A dispatched worker may have HERMES_KANBAN_DB pinned to its own board DB;
    # unset it so explicit `--board` arguments are honored.
    env.pop("HERMES_KANBAN_DB", None)
    return env


def clean_parent_ids(parent_ids: list[str], allow_parent_links: bool) -> list[str]:
    if not parent_ids:
        return []
    if not allow_parent_links:
        raise ValueError(
            "parent links are disabled by default for cockpit routing; pass "
            "--allow-parent-links with explicit --parent task ids only when this "
            "card should be dependency-gated"
        )
    cleaned: list[str] = []
    for parent_id in parent_ids:
        parent = parent_id.strip()
        if not TASK_ID_RE.match(parent):
            raise ValueError(f"invalid parent task id: {parent_id!r}")
        if parent not in cleaned:
            cleaned.append(parent)
    return cleaned


def _workspace_cli_spec(kind: str, path: str | None) -> str | None:
    """Translate delegation-package workspace metadata to Hermes CLI syntax."""
    if kind == "none":
        return None
    if kind == "scratch":
        return "scratch"
    if kind in {"dir", "worktree", "remote"}:
        if not path:
            raise ValueError(f"workspace path is required for kind {kind!r}")
        return f"{kind}:{path}"
    raise ValueError(f"unsupported workspace kind: {kind!r}")


from contextvars import ContextVar
_NATIVE_EXECUTION = ContextVar("canonical_topic_native_execution", default=None)

def _create_command(cmd):
    hooks = _NATIVE_EXECUTION.get()
    if hooks is not None:
        return hooks["create"](cmd)
    return subprocess.run(cmd, text=True, capture_output=True, timeout=20, env=hermes_cli_env())


def create_card(
    route: ResolvedRoute, body: str, priority: int, idempotency_key: str | None,
    parent_ids: list[str], edge_role: str = "dependency",
    workspace_kind: str = "scratch", workspace_path: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    cmd = [
        "hermes",
        "kanban",
        "--board",
        route.board,
        "create",
        route.title,
        "--body",
        body,
        "--assignee",
        route.assignee,
        "--tenant",
        route.tenant,
        "--priority",
        str(priority),
        "--created-by",
        "margot-safe-delegate-card",
        "--json",
    ]
    workspace_spec = _workspace_cli_spec(workspace_kind, workspace_path)
    if workspace_spec is not None:
        cmd.extend(["--workspace", workspace_spec])
    if idempotency_key:
        cmd.extend(["--idempotency-key", idempotency_key])
    if reasoning_effort:
        cmd.extend(["--reasoning-effort", reasoning_effort])
    for parent_id in parent_ids:
        cmd.extend(["--parent", parent_id])
    if parent_ids:
        cmd.extend(["--edge-role", edge_role])
    result = _create_command(cmd)
    if result.returncode != 0:
        raise RuntimeError("Hermes Kanban create failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout.strip()}
    return {"command": cmd[:4] + ["create", "<title>", "..."], "result": payload}


def create_review_card(
    route: ResolvedRoute,
    producer_task_id: str,
    priority: int,
    delegation_package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one dependency-gated lead-review card for a completed producer task."""
    if not route.reviewer_profiles:
        return {"ok": False, "reason": "no canonical reviewer profiles for this route"}
    lead = route.reviewer_profiles[0]
    secondary = route.reviewer_profiles[1:]
    body = f"""Review producer task `{producer_task_id}` before its artifact reaches Desk.

## Review contract
- Producer: `{route.assignee}`
- Lead reviewer: `{lead}`
- Secondary reviewers when domain-specific input is needed: {', '.join(f'`{x}`' for x in secondary) or 'none'}
- Inspect the actual artifact and parent handoff. Check formatting, completeness, factual support, acceptance criteria, usability/readability, and verification evidence.
- If it passes, complete this review card with exact evidence and the final artifact reference.
- If changes are needed, do not label ordinary quality defects as blocked. Comment a bounded defect list on this same review card; return the same producer task to its owner for repair, then re-review the same card. Do not create revision or re-review cards.
- Use blocked only for a genuine dependency, capability wall, missing human input, or transient failure.
- Do not send, publish, purchase, sign, delete, deploy, restart, or make another irreversible external change. Package any such final action for Desk approval after peer review passes.
"""
    if delegation_package:
        body += "\n## Callback metadata\n\n```json\n"
        body += json.dumps(delegation_callback_payload(delegation_package), indent=2, sort_keys=True)
        body += "\n```\n"
    if route.department_id == "legal_compliance" and lead == "steven":
        body += """

## Mandatory senior legal review
- The junior producer completion only unlocks this review; it is not a final legal completion or callback.
- Bind the verdict to the exact current artifact path, SHA-256 digest, and version. A substantive edit invalidates the attestation and requires a fresh Steven review.
- Confirm the packet remains an internal reviewed draft, not legal advice, execution-ready, signable, or send-ready.
- Return only a minimum-context/redacted status to the originating private source/return surface.
- Do not make a legal commitment, sign, pay, send externally, use a logged-in account, widen retention, or disclose raw privileged material.
- Chris or counsel remains the gate for commitments, signatures, counsel/client/third-party sends, and external distribution.
- Fable is not part of this chain. Any Fable escalation requires a separate explicit Chris-approved compact redacted packet.
"""
    cmd = [
        "hermes", "kanban", "--board", route.board, "create", f"Review: {route.title}",
        "--body", body, "--assignee", lead, "--tenant", route.tenant,
        "--priority", str(priority), "--created-by", "margot-safe-review-chain",
        "--parent", producer_task_id, "--json",
        "--edge-role", "delegation_reviewer",
    ]
    result = _create_command(cmd)
    if result.returncode != 0:
        raise RuntimeError("Hermes Kanban review-card create failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout.strip()}
    return {"ok": True, "lead_reviewer": lead, "secondary_reviewers": secondary, "result": payload}


def create_sol_review_card(
    route: ResolvedRoute,
    producer_task_id: str,
    priority: int,
    delegation_package: dict[str, Any],
) -> dict[str, Any]:
    """Create the automatic dependency-gated Sol review required by the package."""
    contract = delegation_package.get("review_contract") or {}
    if not contract.get("required") or not contract.get("dependency_required"):
        return {"ok": False, "reason": "maker-to-Sol review is not required"}
    reviewer = str(contract.get("sol_reviewer_profile") or "").strip().lower()
    if reviewer not in spawnable_profiles():
        raise ValueError(f"required Sol reviewer is not spawnable: {reviewer!r}")
    if reviewer == route.assignee and delegation_package.get("chain_contract") != "legal_steven_drafter_steven":
        raise ValueError("required Sol reviewer must be independent from the producer")
    body = f"""Perform the required Sol review for producer task `{producer_task_id}`.

## Maker-to-Sol review contract
- Maker: `{contract.get('maker_profile')}`
- Sol reviewer: `{reviewer}`
- Review trigger: `{contract.get('trigger')}`
- This card is dependency-gated and is the authoritative review callback for the producer.
- Inspect the exact artifact and evidence. Record a PASS or bounded REQUEST CHANGES tied to the current artifact/version.
- Do not send, publish, sign, pay, deploy, restart, change credentials, or approve a legal commitment. Preserve any existing human gate.

## Callback metadata

```json
{json.dumps(delegation_callback_payload(delegation_package), indent=2, sort_keys=True)}
```
"""
    cmd = [
        "hermes", "kanban", "--board", route.board, "create", f"Sol review: {route.title}",
        "--body", body, "--assignee", reviewer, "--tenant", route.tenant,
        "--priority", str(priority), "--created-by", "margot-maker-sol-review",
        "--parent", producer_task_id,
        "--edge-role", "delegation_reviewer",
        "--idempotency-key", f"{delegation_package['idempotency_and_dedupe']['idempotency_key']}:sol-review",
        "--json",
    ]
    result = _create_command(cmd)
    if result.returncode != 0:
        raise RuntimeError("Hermes Kanban Sol review-card create failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout.strip()}
    return {"ok": True, "reviewer": reviewer, "result": payload}


def task_id_from_create_result(create_result: dict[str, Any]) -> str | None:
    """Extract a task id from the JSON shape returned by `hermes kanban create`."""
    payload = create_result.get("result") if isinstance(create_result, dict) else None

    def walk(value: Any) -> str | None:
        if isinstance(value, str) and TASK_ID_RE.match(value):
            return value
        if isinstance(value, dict):
            for key in ("id", "task_id"):
                found = walk(value.get(key))
                if found:
                    return found
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def create_legal_inbound_chain(
    *,
    route: ResolvedRoute,
    body: str,
    priority: int,
    idempotency_key: str,
    delegation_package: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Create the mandatory Steven -> Legal Drafter -> Steven topic chain."""
    if route.department_id != "legal_compliance" or route.assignee != "steven":
        raise ValueError("legal inbound chain requires canonical Steven legal route")
    if delegation_package is None:
        raise ValueError("legal inbound chain requires a validated Steven/Sol delegation package")
    if delegation_package.get("chain_contract") != "legal_steven_drafter_steven":
        raise ValueError("legal inbound chain contract is missing")
    if str(delegation_package.get("created_by") or "").strip().lower() != "steven":
        raise ValueError("legal inbound package must be authored by Steven")
    if str(delegation_package.get("package_author_tier") or "").strip().lower() != "sol":
        raise ValueError("legal inbound package must retain the Steven/Sol author tier")
    if (delegation_package.get("assignee") or {}).get("reviewer_profiles") != ["steven"]:
        raise ValueError("legal inbound package must return to Steven as sole required senior reviewer")
    if (delegation_package.get("fable_advisory_review") or {}).get("enabled"):
        raise ValueError("Fable may not be implicit in the legal inbound chain")

    parent_body = body_with_metadata(
        "Steven/Sol legal intake and package-author step. Frame the objective, provenance "
        "stub, parties/placeholders, jurisdiction and unresolved terms, permitted tools, "
        "forbidden actions, expected artifact, exact digest/version rule, privacy/retention "
        "policy, and Chris/counsel gates. Do not perform the junior drafting task, provide "
        "legal advice, sign, commit, pay, send externally, use a logged-in account, widen "
        "retention, or disclose raw legal material.\n\n"
        f"{body}",
        route,
        delegation_package,
    )
    parent = create_card(
        route, parent_body, priority, idempotency_key, [],
        reasoning_effort=reasoning_effort,
    )
    parent_task_id = task_id_from_create_result(parent)
    if not parent_task_id:
        raise RuntimeError("could not resolve Steven legal package task id")

    draft_route = replace(
        route,
        title=f"Draft: {route.title}",
        assignee="legal-drafter",
        member_role="Junior Legal Drafter",
        member_role_slug="junior-legal-drafter",
        handoff_label=f"{route.pod_slug}/junior-legal-drafter: draft only",
        reviewer_profiles=["steven"],
        route_reason="canonical legal topic chain: Steven package -> Legal Drafter -> Steven review",
    )
    draft_package = deepcopy(delegation_package)
    draft_package["assignee"]["canonical_spawnable_assignee"] = "legal-drafter"
    draft_package["assignee"]["human_facing_owner"] = "Avery"
    draft_package["model_tier"] = {
        **draft_package["model_tier"],
        "planner": "sol",
        "executor": "terra",
        "reviewer": "sol",
    }
    if draft_package.get("model_routing"):
        draft_package["model_routing"] = configured_model_routing(draft_package)
    draft_instructions = (
        f"Draft-only child of Steven legal package `{parent_task_id}`.\n\n"
        "The result is not final and may only unlock the dependency-gated Steven review. "
        "Do not provide legal advice, sign, commit, pay, send externally, use a logged-in "
        "account, widen retention, or disclose raw legal material. "
        "Return an exact-artifact digest, change log, issue list, and minimum-context "
        "Steven-ready packet for the dependency-gated senior review.\n\n"
        f"{body}"
    )
    draft_body = body_with_metadata(draft_instructions, draft_route, draft_package)
    draft = create_card(
        draft_route,
        draft_body,
        priority,
        f"{idempotency_key}:draft",
        [parent_task_id],
        "delegation_maker",
    )
    draft_task_id = task_id_from_create_result(draft)
    if not draft_task_id:
        raise RuntimeError("could not resolve Legal Drafter task id")
    review = create_review_card(draft_route, draft_task_id, priority, draft_package)
    review_task_id = task_id_from_create_result(review)
    if not review_task_id:
        raise RuntimeError("could not resolve Steven legal review task id")
    return {
        "parent": parent,
        "draft": draft,
        "review": review,
        "parent_task_id": parent_task_id,
        "draft_task_id": draft_task_id,
        "review_task_id": review_task_id,
        "callback_task_id": review_task_id,
        "draft_finality_status": "steven_required_pending",
    }


def create_coordination_package_chain(
    *,
    route: ResolvedRoute,
    request_body: str,
    worker_body: str,
    priority: int,
    idempotency_key: str,
    delegation_package: dict[str, Any],
    with_review: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Create Claire/Sol package -> routine worker -> optional reviewer."""
    if route.department_id != "ops_control":
        raise ValueError("coordination package chain requires canonical Mission Control route")
    if delegation_package.get("created_by") != "claire" or delegation_package.get("package_author_tier") != "sol":
        raise ValueError("Mission Control coordination requires Claire/Sol package metadata")
    if delegation_package.get("chain_contract") != "claire_package_then_worker":
        raise ValueError("Mission Control coordination chain contract is missing")

    package_route = replace(
        route,
        title=f"Package: {route.title}",
        assignee="claire",
        owner_name="Claire",
        member_role="Coordination Package Author",
        member_role_slug="coordination-package-author",
        handoff_label="claire-ops/coordination-package-author: package and route",
        reviewer_profiles=[],
        route_reason="canonical Mission Control chain: Claire/Sol package before routine worker",
    )
    package_body = body_with_metadata(
        "Author and verify the bounded coordination package for the dependency-gated "
        f"routine worker `{route.assignee}`. Preserve the source/return callback and do "
        "not perform the worker's substantive task in this parent card.\n\n"
        f"{request_body}",
        package_route,
        delegation_package,
    )
    package_create = create_card(
        package_route,
        package_body,
        priority,
        f"{idempotency_key}:package",
        [],
        reasoning_effort=reasoning_effort,
    )
    package_task_id = task_id_from_create_result(package_create)
    if not package_task_id:
        raise RuntimeError("could not resolve Claire coordination package task id")

    dependent_worker_body = (
        f"Execute only after Claire package `{package_task_id}` completes. Follow that "
        "package as the controlling directive and preserve its callback metadata.\n\n"
        f"{worker_body}"
    )
    worker_create = create_card(
        route,
        dependent_worker_body,
        priority,
        idempotency_key,
        [package_task_id],
        "delegation_maker",
    )
    worker_task_id = task_id_from_create_result(worker_create)
    if not worker_task_id:
        raise RuntimeError("could not resolve Mission Control routine-worker task id")

    review_create = None
    review_task_id = None
    if with_review and route.reviewer_profiles:
        review_create = create_review_card(route, worker_task_id, priority, delegation_package)
        review_task_id = task_id_from_create_result(review_create)
        if not review_task_id:
            raise RuntimeError("could not resolve Mission Control review task id")
    sol_review_create = None
    sol_review_task_id = None
    if (delegation_package.get("review_contract") or {}).get("required"):
        sol_parent = review_task_id or worker_task_id
        sol_review_create = create_sol_review_card(route, sol_parent, priority, delegation_package)
        sol_review_task_id = task_id_from_create_result(sol_review_create)
        if not sol_review_task_id:
            raise RuntimeError("could not resolve required Mission Control Sol review task id")
    return {
        "package": package_create,
        "worker": worker_create,
        "review": review_create,
        "sol_review": sol_review_create,
        "package_task_id": package_task_id,
        "worker_task_id": worker_task_id,
        "review_task_id": review_task_id,
        "sol_review_task_id": sol_review_task_id,
    }


def telegram_notify_target(route: ResolvedRoute) -> dict[str, str] | None:
    """Resolve the originating Telegram chat/topic encoded by cockpit surfaces."""
    for surface in (route.return_surface, route.source_surface, route.tenant):
        if not surface:
            continue
        match = TELEGRAM_SURFACE_RE.match(str(surface).strip())
        if not match:
            continue
        target = {"platform": "telegram", "chat_id": match.group("chat")}
        thread = match.group("thread")
        if thread:
            target["thread_id"] = thread
        return target
    return None


def subscribe_return_target(route: ResolvedRoute, task_id: str, notifier_profile: str | None = None) -> dict[str, Any]:
    hooks = _NATIVE_EXECUTION.get()
    if hooks is not None:
        return hooks["subscribe"](route, task_id, notifier_profile)
    target = telegram_notify_target(route)
    if not target:
        return {"ok": False, "reason": "no supported return target"}
    cmd = [
        "hermes",
        "kanban",
        "--board",
        route.board,
        "notify-subscribe",
        task_id,
        "--platform",
        target["platform"],
        "--chat-id",
        target["chat_id"],
        "--notifier-profile",
        notifier_profile or os.environ.get("HERMES_PROFILE") or "default",
    ]
    if target.get("thread_id"):
        cmd.extend(["--thread-id", target["thread_id"]])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=20, env=hermes_cli_env())
    return {
        "ok": result.returncode == 0,
        "target": target,
        "command": cmd[:4] + ["notify-subscribe", task_id, "..."],
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def dispatch_dry_run(board: str) -> dict[str, Any]:
    if _NATIVE_EXECUTION.get() is not None:
        return {"deferred": True, "reason": "existing gateway dispatcher owns committed tasks"}
    result = subprocess.run(
        ["hermes", "kanban", "--board", board, "dispatch", "--dry-run", "--json"],
        text=True,
        capture_output=True,
        timeout=20,
        env=hermes_cli_env(),
    )
    if result.returncode != 0:
        return {"ok": False, "error": "dispatch dry-run failed"}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "dispatch dry-run returned non-json"}
    data["ok"] = not data.get("skipped_unassigned") and not data.get("skipped_nonspawnable")
    return data


def self_test() -> int:
    assert _workspace_cli_spec("scratch", None) == "scratch"
    assert _workspace_cli_spec("dir", "/tmp/project") == "dir:/tmp/project"
    assert _workspace_cli_spec("worktree", "/tmp/repo") == "worktree:/tmp/repo"
    assert _workspace_cli_spec("remote", "host:/srv/project") == "remote:host:/srv/project"
    assert _workspace_cli_spec("none", None) is None
    spawnable = spawnable_profiles()
    boards = known_boards()
    test_board = "chris-home" if "chris-home" in boards else sorted(boards)[0]
    expected_pairs = {
        department: (owner, maker)
        for owner, (department, maker) in SOL_OWNER_TERRA_MAKER_DEPARTMENTS.items()
    }
    assert len(expected_pairs) == 11, expected_pairs
    assert all(owner in spawnable and maker in spawnable for owner, maker in expected_pairs.values())
    assert not ({"connor", "josh", "grok-4-5", "kimi-k3", "inkling"} & spawnable)

    routes = []
    for department, (owner, maker) in expected_pairs.items():
        args = parse_args([
            "--board", test_board,
            "--tenant", "routing-v3-self-test",
            "--department", department,
            "--title", f"Routing self-test {department}",
            "--body", "Internal dry-run only; no external action.",
            "--package-class", "status",
            "--package-author", "claire",
            "--package-author-tier", "sol",
        ])
        route = resolve_route(args)
        package = build_delegation_package(args, route)
        errors = validate_delegation_package(package, route)
        assert not errors, (department, errors)
        contract = package.get("routing_contract") or {}
        assert route.assignee == maker, (department, route.assignee, maker)
        assert contract.get("accountable_owner_profile") == owner
        assert contract.get("maker_profile") == maker
        if department in FABLE_SECONDARY_REVIEW_DEPARTMENTS:
            assert package.get("required_secondary_reviewer_profile") == "fable-review"
            assert package.get("secondary_reviewer_profiles") == ["fable-review"]
        if department == "legal_compliance":
            review = package.get("review_contract") or {}
            finality = package.get("legal_finality_contract") or {}
            human_gates = package.get("irreversible_external_action_gates") or {}
            assert review.get("required") is True
            assert review.get("dependency_required") is True
            assert review.get("sol_reviewer_profile") == "steven"
            assert review.get("secondary_reviewer_profile") == "fable-review"
            assert review.get("conditional_reviewer_profile") == "claude-review"
            assert finality.get("state") == "pending_steven_exact_artifact_attestation"
            assert finality.get("substantive_edit_invalidates_reviews") is True
            assert human_gates.get("required") is True
            assert human_gates.get("review_authorizes_action") is False
        routes.append({"department": department, "owner": owner, "maker": maker})

    for department, blocked in (
        ("ops_control", "connor"),
        ("ops_control", "josh"),
        ("ops_control", "grok-4-5"),
        ("ops_control", "kimi-k3"),
        ("ops_control", "inkling"),
        ("ops_control", "fake-profile"),
        ("legal_compliance", "junior-legal-drafter"),
    ):
        args = parse_args([
            "--board", test_board,
            "--tenant", "routing-v3-self-test",
            "--department", department,
            "--assignee", blocked,
            "--title", "Blocked profile self-test",
        ])
        try:
            resolve_route(args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"blocked ordinary assignee was accepted: {blocked}")

    print(json.dumps({"ok": True, "routes": routes}, indent=2))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true", help="run built-in resolver smoke tests")
    p.add_argument("--execute", action="store_true", help="actually create the card; default is dry-run")
    p.add_argument("--board", help="explicit board for non-cockpit work; cockpit board is resolved from the trusted user")
    p.add_argument("--requesting-user", help="trusted authenticated cockpit username")
    p.add_argument("--source-session-key", help="trusted source session key (agent-os:cockpit:<user>)")
    p.add_argument("--tenant", required=False, default="telegram:-1003734569758:16344")
    p.add_argument("--title", required=False, default="Untitled delegated task")
    p.add_argument("--body", default="")
    p.add_argument("--department", default="ops_control", help="role id/name/pod/agent; unknown routes through ops_control/claire with proposed_new_department=true")
    p.add_argument("--assignee", help="optional explicit spawnable profile override")
    p.add_argument("--allow-new-department", action="store_true", help="compatibility flag; unknown departments are always marked proposed_new_department and routed through a real Ops Control owner")
    p.add_argument("--owner-name")
    p.add_argument("--pod")
    p.add_argument("--pod-slug")
    p.add_argument("--member-role")
    p.add_argument("--member-role-slug")
    p.add_argument("--handoff-label")
    p.add_argument("--source-surface")
    p.add_argument("--return-surface")
    p.add_argument("--privacy-tier", default="team")
    p.add_argument("--approval-gate")
    p.add_argument("--package-class", choices=sorted(VALID_PACKAGE_CLASSES), default="status")
    p.add_argument(
        "--routing-contract",
        choices=[OWNER_ACCEPTANCE_ROUTING_CONTRACT],
        help="explicit canonical topology contract; absent preserves legacy routing",
    )
    p.add_argument("--accountable-owner-profile", help="spawnable canonical Sol owner required to accept the single root")
    p.add_argument(
        "--producer-kind",
        choices=sorted(PRODUCER_KINDS),
        default="model",
        help="producer class; only explicit deterministic/status/mechanical classes may bypass maker-to-Sol review",
    )
    p.add_argument("--sensitivity", action="append", default=[], help="decision sensitivity; repeat or comma-separate")
    p.add_argument("--exact-action", help="verb-first gated action for a decision package")
    p.add_argument("--risk-cost-summary", help="honest risk/cost summary for a decision package")
    p.add_argument("--resume-target", help="task/workflow target resumed after approval")
    p.add_argument("--handoff-from-user", help="optional explicit source assertion; must match requesting user")
    p.add_argument("--handoff-to-user", help="explicit cross-user recipient; routes only to that user's board/audience")
    p.add_argument("--handoff-reason")
    p.add_argument("--handoff-source-package-id")
    p.add_argument("--handoff-source-artifact-id")
    p.add_argument("--handoff-source-task-id")
    p.add_argument("--suggested-ask")
    p.add_argument("--verifier")
    p.add_argument("--package-id", help="stable delegation package id; defaults to --idempotency-key or a deterministic route hash")
    p.add_argument("--package-author", help="package author metadata, e.g. claire for complex/durable/sensitive work")
    p.add_argument("--package-author-tier", default="luna", help="author role/model hint: astra (senior), sol (legacy senior alias), luna, human; configured models come from registry model_policy")
    p.add_argument("--package-reviewed-by", help="Claire/Sol reviewer metadata for complex packages not authored by Claire")
    p.add_argument("--package-reviewer-tier", help="model tier of package reviewer")
    p.add_argument("--complexity", choices=["simple", "standard", "complex", "critical"], default="simple")
    p.add_argument("--complexity-reason", action="append", default=[], help="repeat or comma-separate: durable,sensitive,cross_department,audit_required,high_cost,production,final_verdict,...")
    p.add_argument("--source-ref")
    p.add_argument("--raw-provenance")
    p.add_argument("--user-request")
    p.add_argument("--operational-goal")
    p.add_argument("--non-goal", action="append", default=[])
    p.add_argument("--workspace-kind", default="scratch", choices=["scratch", "dir", "worktree", "remote", "none"])
    p.add_argument("--workspace-path")
    p.add_argument("--allowed-toolset", action="append", default=[], help="repeat or comma-separate allowed toolsets for the worker")
    p.add_argument("--forbidden-action", action="append", default=[], help="repeat or comma-separate forbidden side effects")
    p.add_argument("--allowed-side-effect", action="append", default=[])
    p.add_argument("--source-file", action="append", default=[])
    p.add_argument("--source-url", action="append", default=[])
    p.add_argument("--task-ref", action="append", default=[])
    p.add_argument("--prior-context", action="append", default=[])
    p.add_argument("--acceptance", action="append", default=[])
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--evidence-location")
    p.add_argument("--reviewer-instructions")
    p.add_argument("--gate", action="append", default=[], help="approval gates; repeat or comma-separate")
    p.add_argument("--approval-owner", help="decision recipient; defaults to the resolved audience")
    p.add_argument("--callback-destination")
    p.add_argument("--report-style", default="concise_status", choices=["concise_status", "artifact_review", "approval_packet", "blocked_packet"])
    p.add_argument("--duplicate-check")
    p.add_argument("--supersedes", action="append", default=[])
    p.add_argument("--timebox-minutes", type=int, default=45)
    p.add_argument("--token-budget")
    p.add_argument("--runtime-cap-seconds", type=int)
    p.add_argument("--max-review-loops", type=int, default=2)
    p.add_argument("--escalate-to")
    p.add_argument("--escalation-trigger", action="append", default=[])
    p.add_argument("--fallback-degraded-mode", action="store_true", help="mark package as draft/evidence-only degraded fallback")
    p.add_argument("--fallback-reason")
    p.add_argument("--fallback-allowed-output", choices=["draft_only", "evidence_only", "partial_status"], default="draft_only")
    p.add_argument("--approval-status", default="not_applicable")
    p.add_argument("--premium-approved", action="store_true", help="allow explicit Fable 5 escalation after Chris requests or approves the premium lane")
    p.add_argument("--with-review", action="store_true", help="create a dependency-gated peer-review card using the canonical review matrix")
    p.add_argument("--legal-inbound-chain", action="store_true", help="create Steven package -> Legal Drafter -> Steven review for canonical legal-topic ingress")
    p.add_argument("--coordination-package-chain", action="store_true", help="create Claire/Sol package -> routine worker -> optional review for Mission Control ingress")
    p.add_argument("--canonical-topic-intake", action="store_true", help="bounded source-reference-only inbound topic handoff; preserves privacy while the canonical owner classifies scope")
    p.add_argument("--priority", type=int, default=50)
    p.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        help=(
            "explicit effort for the primary task, or for the Astra package-author task "
            "in chain mode; child maker and review cards keep their own profile defaults"
        ),
    )
    p.add_argument("--idempotency-key")
    p.add_argument("--parent", action="append", default=[], help="explicit dependency parent task id; ignored unless --allow-parent-links is also set")
    p.add_argument("--allow-parent-links", action="store_true", help="allow dependency gating by explicit --parent ids; default creates standalone tasks")
    p.add_argument("--notify-profile", default=None, help="profile gateway that should deliver Telegram completion follow-ups; default is HERMES_PROFILE or default")
    args = p.parse_args(argv)
    for field in ("package_author_tier", "package_reviewer_tier"):
        if getattr(args, field, None) == "astra":
            setattr(args, field, "sol")  # compatibility role alias, not model selection
    return args


def main(argv: list[str], *, native_hooks=None, result_callback=None) -> int:
    token = _NATIVE_EXECUTION.set(native_hooks)
    try:
        return _main(argv, result_callback=result_callback)
    finally:
        _NATIVE_EXECUTION.reset(token)


def _main(argv: list[str], *, result_callback=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()

    try:
        validate_owner_acceptance_topology_args(args)
        if args.legal_inbound_chain:
            if not args.canonical_topic_intake or not args.with_review:
                raise ValueError("legal inbound chain requires canonical topic intake and mandatory Steven review")
            if args.premium_approved:
                raise ValueError("Fable escalation must be a separate explicit redacted packet, not part of legal ingress")
            args.approval_gate = args.approval_gate or "legal_commitment"
        route = resolve_route(args)
        validate_owner_acceptance_topology_args(args)
        parent_ids = clean_parent_ids(args.parent, args.allow_parent_links)
        delegation_package = build_delegation_package(args, route)
        validation_errors = validate_delegation_package(delegation_package, route)
        if validation_errors:
            raise ValueError("delegation package validation failed: " + "; ".join(validation_errors))
        full_body = body_with_metadata(args.body or "", route, delegation_package)
        effective_idempotency_key = args.idempotency_key or delegation_package["idempotency_and_dedupe"]["idempotency_key"]
        output: dict[str, Any] = {
            "ok": True,
            "mode": "execute" if args.execute else "dry-run",
            "route": asdict(route),
            "delegation_package": delegation_package,
            "callback_payload": delegation_callback_payload(delegation_package),
            "parents": parent_ids,
            "body_preview": full_body,
            "notify_target": telegram_notify_target(route),
            "review_plan": {
                "enabled": bool(args.with_review and route.reviewer_profiles),
                "lead_reviewer": route.reviewer_profiles[0] if route.reviewer_profiles else None,
                "secondary_reviewers": route.reviewer_profiles[1:],
                "automatic_sol_review_required": bool((delegation_package.get("review_contract") or {}).get("required")),
                "sol_reviewer": (delegation_package.get("review_contract") or {}).get("sol_reviewer_profile"),
                "dependency_edge_required": bool((delegation_package.get("review_contract") or {}).get("dependency_required")),
            },
            "reasoning_plan": {
                "primary_or_package_task": args.reasoning_effort or "profile_default",
                "maker_tasks": "profile_default",
                "review_tasks": "profile_default",
            },
        }
        if args.execute:
            notify_task_id = None
            try:
                hooks = _NATIVE_EXECUTION.get()
                if hooks is not None and hooks.get("command_board_root") and not args.legal_inbound_chain:
                    # Command boards already have a native owner-cell review
                    # lifecycle. Legacy reviewer cards would add a prohibited
                    # second delegation level and duplicate review/returns.
                    output["create"] = create_card(route, full_body, args.priority,
                        effective_idempotency_key, parent_ids,
                        workspace_kind=args.workspace_kind, workspace_path=args.workspace_path,
                        reasoning_effort=args.reasoning_effort)
                    notify_task_id = task_id_from_create_result(output["create"])
                    output["execution_plan"] = "native_owner_cell_root"
                elif args.legal_inbound_chain:
                    output["legal_chain"] = create_legal_inbound_chain(
                        route=route,
                        body=args.body or "",
                        priority=args.priority,
                        idempotency_key=effective_idempotency_key,
                        delegation_package=delegation_package,
                        reasoning_effort=args.reasoning_effort,
                    )
                    notify_task_id = output["legal_chain"]["review_task_id"]
                elif args.coordination_package_chain:
                    output["coordination_chain"] = create_coordination_package_chain(
                        route=route,
                        request_body=args.body or "",
                        worker_body=full_body,
                        priority=args.priority,
                        idempotency_key=effective_idempotency_key,
                        delegation_package=delegation_package,
                        with_review=args.with_review,
                        reasoning_effort=args.reasoning_effort,
                    )
                    notify_task_id = (
                        output["coordination_chain"]["sol_review_task_id"]
                        or output["coordination_chain"]["review_task_id"]
                        or output["coordination_chain"]["worker_task_id"]
                    )
                else:
                    if route.assignee == "fable-review" and not parent_ids:
                        raise ValueError("Fable advisory execution requires an explicit parent Sol review task dependency")
                    output["create"] = create_card(
                        route, full_body, args.priority, effective_idempotency_key,
                        parent_ids, workspace_kind=args.workspace_kind,
                        workspace_path=args.workspace_path,
                        reasoning_effort=args.reasoning_effort,
                    )
                    task_id = task_id_from_create_result(output["create"])
                    notify_task_id = task_id
                    review_task_id = None
                    if task_id and args.with_review and route.reviewer_profiles:
                        output["review_create"] = create_review_card(
                            route, task_id, args.priority, delegation_package
                        )
                        review_task_id = task_id_from_create_result(output["review_create"])
                        output["review_task_id"] = review_task_id
                        notify_task_id = review_task_id or task_id
                    if task_id and (delegation_package.get("review_contract") or {}).get("required"):
                        sol_parent = review_task_id or task_id
                        output["sol_review_create"] = create_sol_review_card(
                            route, sol_parent, args.priority, delegation_package
                        )
                        sol_review_task_id = task_id_from_create_result(output["sol_review_create"])
                        if not sol_review_task_id:
                            raise RuntimeError("could not resolve required Sol review task id")
                        output["sol_review_task_id"] = sol_review_task_id
                        notify_task_id = sol_review_task_id
            finally:
                if notify_task_id:
                    output["notify_subscription"] = subscribe_return_target(route, notify_task_id, args.notify_profile)
                else:
                    output["notify_subscription"] = {"ok": False, "reason": "could not resolve created task id"}
            output["dispatch_dry_run"] = dispatch_dry_run(route.board)
        if result_callback is not None:
            result_callback(output)
        else:
            print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

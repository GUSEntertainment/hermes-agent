"""Opt-in Owner Cell v1 primitives.

This module is deliberately additive.  It stores canonical goal state beside
legacy Kanban rows and exposes fail-closed guard helpers.  Callers must opt in;
legacy task creation and dispatch remain unchanged while the feature flag is off.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

OWNER_CELL_SCHEMA_VERSION = 1
VALID_MODES = frozenset({"script", "single", "reviewed", "fanout"})
VALID_VERDICTS = frozenset({"PASS", "REQUEST_CHANGES", "FAIL_CLOSED"})
VALID_ACCESS_MODES = frozenset({"read", "mutate", "read_exclusive"})
PROTECTED_GATES = frozenset({
    "purchase", "spend", "external_send", "publish", "deletion",
    "irreversible_migration", "credential_change", "account_change",
    "security_change", "deploy", "restart", "legal_commitment",
    "financial_commitment", "personnel_decision", "privacy_wall_change",
})
FRONTSTAGE_EVENTS = frozenset({"goal_blocked", "decision_required", "goal_completed", "long_running_milestone"})


class OwnerCellGuardError(ValueError):
    """Raised before an Owner Cell side effect would violate a guard."""


@dataclass(frozen=True)
class RouterDecision:
    mode: str
    risk_class: str
    rationale: str


@dataclass(frozen=True)
class ReworkResult:
    phase: str
    owner_profile: str
    runtime_profile: str
    review_loop_count: int


@dataclass(frozen=True)
class ChurnDecision:
    """Deterministic, per-goal orchestration circuit-breaker result."""

    action: str
    stop_spawn: bool
    second_owner_warning: bool
    notification_key: str | None
    rationale: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def _regular_file_sha256(path: str | Path) -> str:
    """Hash a regular file without following a final-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(Path(path), flags)
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OwnerCellGuardError("bound review artifact must be a regular file")
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(fd)



def normalize_digest(value: object) -> str:
    """Canonical bare lowercase sha256 hex; accepts an optional 'sha256:' prefix."""
    text = str(value or "").strip()
    if text.lower().startswith("sha256:"):
        text = text[7:]
    return text.lower()


def digest_matches(path: str | Path, bound_digest: object) -> bool:
    return bool(bound_digest) and _regular_file_sha256(path) == normalize_digest(bound_digest)


_COMMAND_BOARD = "chris-home"


def configured_command_boards() -> frozenset[str]:
    """Return the shared, fail-closed command-board allowlist."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        config = {}
    from hermes_cli.command_boards import resolve_command_boards
    return frozenset(resolve_command_boards(config, default=_COMMAND_BOARD) or ())


def is_command_board(board: str | None) -> bool:
    # 2026-09-25 (Chris, "take it all out"): no board is a command board.
    # Cards are plain: assignee owns and executes.
    return False


_ROUTING_METADATA_RE = re.compile(
    r"## Routing metadata\s*```json\s*(\{.*?\})\s*```", re.DOTALL
)
_DEFAULT_TOPIC_MATRIX = (
    Path(__file__).resolve().parents[1]
    / "gateway"
    / "policies"
    / "margot-departments-topics.json"
)


def _routing_metadata(body: Any) -> dict[str, Any]:
    if not isinstance(body, str):
        return {}
    match = _ROUTING_METADATA_RE.search(body)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _topic_route(department_id: str) -> dict[str, Any]:
    """Best-effort read of the reviewed routing source; never invent an owner."""
    try:
        raw = json.loads(_DEFAULT_TOPIC_MATRIX.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if str(raw.get("tenant") or "").strip() != _COMMAND_BOARD:
        return {}
    for topic in raw.get("topics") or []:
        if isinstance(topic, dict) and str(topic.get("department_id") or "").strip() == department_id:
            return topic
    return {}


def _installed_profile(value: Any) -> str | None:
    from hermes_cli.profiles import normalize_profile_name, profile_exists
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        profile = normalize_profile_name(raw)
    except ValueError:
        return None
    return profile if profile_exists(profile) else None


def _canonical_owner_acceptance_binding(
    metadata: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Validate and return the immutable owner/maker task-metadata binding."""
    owner_acceptance = metadata.get("owner_acceptance")
    routing = metadata.get("routing_contract")
    marker_enabled = (
        isinstance(owner_acceptance, Mapping)
        and owner_acceptance.get("mode") == "accountable_owner"
    )
    routing_enabled = (
        isinstance(routing, Mapping)
        and routing.get("mode") == "sol_owner_terra_maker_single_root"
    )
    if not marker_enabled and not routing_enabled:
        return None
    if not marker_enabled or not routing_enabled:
        raise OwnerCellGuardError(
            "owner-acceptance metadata requires matching canonical marker and routing contract"
        )

    owner = str(routing.get("accountable_owner_profile") or "").strip().lower()
    maker = str(routing.get("maker_profile") or "").strip().lower()
    required = str(routing.get("required_acceptor_profile") or "").strip().lower()
    top_reviewers = metadata.get("reviewer_profiles")
    identity_matches = (
        bool(owner)
        and bool(maker)
        and owner == required
        and owner == str(metadata.get("accountable_owner_profile") or "").strip().lower()
        and owner == str(metadata.get("required_acceptor_profile") or "").strip().lower()
        and owner == str(metadata.get("required_reviewer_profile") or "").strip().lower()
        and top_reviewers == [owner]
        and maker == str(metadata.get("maker_profile") or "").strip().lower()
        and maker == str(metadata.get("runtime_profile") or "").strip().lower()
        and maker == str(metadata.get("executable_assignee") or "").strip().lower()
    )
    topology_matches = (
        str(metadata.get("department_id") or "").strip()
        == str(routing.get("department_id") or "").strip()
        and routing.get("topology") == metadata.get("topology") == "single_root"
        and routing.get("max_child_depth") == metadata.get("max_child_depth") == 0
        and routing.get("peer_review_child_cards")
        == metadata.get("peer_review_child_cards")
        is False
        and routing.get("sol_review_child_cards")
        == metadata.get("sol_review_child_cards")
        is False
        and routing.get("subscription_scope")
        == metadata.get("subscription_scope")
        == "root_only"
        and routing.get("report_back_scope")
        == metadata.get("report_back_scope")
        == "root_only"
    )
    if not identity_matches or not topology_matches:
        raise OwnerCellGuardError(
            "owner-acceptance metadata does not match its canonical owner/maker/root-only routing contract"
        )
    if owner == maker:
        raise OwnerCellGuardError("accountable owner acceptor must be distinct from maker")
    return owner, maker


def command_board_contract(
    *, board: str | None, title: str, body: Any, assignee: Any,
    parents: Iterable[str], goal_id: str,
) -> dict[str, Any] | None:
    """Derive an Owner Cell contract for a routed command-board root.

    The function is deliberately inert for legacy boards, children, and bodies
    without reviewed routing metadata.  It uses only canonical installed runtime
    profiles and preserves the department front door as accountable owner even
    when the maker is a different profile.
    """
    if not is_command_board(board) or tuple(parents):
        return None
    metadata = _routing_metadata(body)
    department = str(metadata.get("department_id") or "").strip()
    source_surface = str(metadata.get("source_surface") or "").strip()
    return_surface = str(metadata.get("return_surface") or "").strip()
    if not department or not source_surface or not return_surface:
        # 2026-09-25 (Chris): no metadata => ordinary card owned by the assignee.
        return None
    topic = _topic_route(department)
    owner_acceptance_binding = _canonical_owner_acceptance_binding(metadata)
    executable = _installed_profile(metadata.get("executable_assignee"))
    requested = _installed_profile(assignee)
    if owner_acceptance_binding is not None:
        bound_owner, bound_maker = owner_acceptance_binding
        owner_profile = _installed_profile(bound_owner)
        runtime_profile = _installed_profile(bound_maker)
        if executable != runtime_profile or requested not in (None, runtime_profile):
            raise OwnerCellGuardError(
                "owner-acceptance metadata requires assignee bound to its canonical maker"
            )
    else:
        runtime_profile = requested or executable
        owner_profile = (
            _installed_profile(metadata.get("accountable_owner_profile"))
            or _installed_profile(topic.get("front_door_profile"))
            or executable
        )
    if runtime_profile is None or owner_profile is None:
        raise OwnerCellGuardError("command-board routing requires canonical installed owner and runtime profiles")
    owner_acceptance = metadata.get("owner_acceptance")
    owner_acceptance_enabled = (
        isinstance(owner_acceptance, Mapping)
        and owner_acceptance.get("mode") == "accountable_owner"
        and owner_profile != runtime_profile
    )
    reviewers = [
        profile for profile in (
            _installed_profile(item)
            for item in (metadata.get("reviewer_profiles") or topic.get("required_reviewers") or [])
        ) if profile and profile != runtime_profile and (
            profile != owner_profile or owner_acceptance_enabled
        )
    ]
    reviewers = list(dict.fromkeys(reviewers))[:1]
    policy = metadata.get("report_back_policy") or (metadata.get("callback_payload") or {}).get("report_back_policy") or {}
    report_policy = str(policy.get("mode") or "milestone_only") if isinstance(policy, Mapping) else "milestone_only"
    realm = str(metadata.get("security_realm") or metadata.get("privacy_tier") or topic.get("privacy_tier") or "").strip()
    if not realm:
        raise OwnerCellGuardError("command-board routing requires a security realm")
    contract = {
        "goal_id": str(goal_id),
        "objective": str(title).strip(),
        "owner_profile": owner_profile,
        "owner_role_alias": department,
        "runtime_profile": runtime_profile,
        "security_realm": realm,
        "router_mode": "reviewed" if reviewers else "single",
        "risk_class": "material" if reviewers else "low",
        "capability_bundle": department,
        "worker_budget": {"producers": 1, "reviewers": len(reviewers), "extras": 0, "max_depth": 1},
        "required_reviewer_profile": reviewers[0] if reviewers else None,
        **(
            {"owner_acceptance": {"mode": "accountable_owner"}}
            if owner_acceptance_enabled
            else {}
        ),
        "delegation_roles": {
            "maker": runtime_profile,
            "reviewer": reviewers[0] if reviewers else None,
        },
        "targets": [],
        "acceptance_test": {"source": "routing_metadata", "required": True},
        "rollback": {"procedure_ref": "same-card-rework"},
        "callback": {
            "source_surface": source_surface,
            "return_surface": return_surface,
            "report_policy": report_policy,
        },
        "approval_gates": list(metadata.get("approval_gates") or topic.get("approval_gates") or []),
        "command_board_flow": True,
    }

    from hermes_cli.owner_workflow import augment_contract
    return augment_contract(contract, metadata)


def resolve_task_runtime_profile(conn: Any, task_id: str, value: Any) -> str | None:
    """Resolve an Owner Cell assignment without changing accountable ownership."""
    row = conn.execute(
        """SELECT t.owner_profile, t.owner_role_alias, g.contract_json
             FROM tasks t LEFT JOIN owner_cell_goals g ON g.task_id = t.id
            WHERE t.id = ?""",
        (task_id,),
    ).fetchone()
    if row is None or not row["owner_profile"]:
        return None
    try:
        contract = json.loads(row["contract_json"] or "{}")
        command_board_flow = bool(contract.get("command_board_flow"))
    except Exception:
        contract = {}
        command_board_flow = False
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("owner-cell task requires an installed runtime profile")
    from hermes_cli.profiles import normalize_profile_name, profile_exists
    normalized = normalize_profile_name(raw)
    if normalized == str(row["owner_role_alias"] or "").strip().lower():
        normalized = str(row["owner_profile"])
    if command_board_flow and not profile_exists(normalized):
        raise ValueError(f"{raw!r} does not resolve to an installed runtime profile")
    if command_board_flow:
        roles = contract.get("delegation_roles") or {}
        allowed = {
            str(row["owner_profile"] or "").strip(),
            str(roles.get("maker") or "").strip(),
            str(roles.get("reviewer") or "").strip(),
        }
        allowed.discard("")
        if normalized not in allowed:
            raise OwnerCellGuardError(
                f"runtime profile {normalized!r} is not authorized by the owner-cell contract"
            )
    return normalized


def enforce_command_board_budget(
    conn: Any, *, board: str | None, parents: Iterable[str], edge_role: str = "dependency",
    child_assignee: str | None = None,
) -> None:
    """Constrain only explicit delegation; dependency DAG edges stay unlimited."""
    return  # 2026-09-25 (Chris): delegation budget ceremony removed.
    if edge_role == "dependency":
        return
    if edge_role not in {"delegation_maker", "delegation_reviewer"}:
        raise OwnerCellGuardError(f"invalid task link role: {edge_role}")
    parent_ids = tuple(str(item) for item in parents if item)
    if not parent_ids:
        return
    if not is_command_board(board):
        placeholders = ",".join("?" for _ in parent_ids)
        owned_lineage = conn.execute(
            "SELECT 1 FROM tasks WHERE id IN (" + placeholders + ") "
            "AND owner_profile IS NOT NULL LIMIT 1",
            parent_ids,
        ).fetchone()
        if owned_lineage is None:
            owned_lineage = conn.execute(
                "SELECT 1 FROM task_links l JOIN tasks r ON r.id = l.parent_id "
                "WHERE l.child_id IN (" + placeholders + ") "
                "AND r.owner_profile IS NOT NULL LIMIT 1",
                parent_ids,
            ).fetchone()
        if owned_lineage is None:
            return
    role = edge_role.removeprefix("delegation_")
    for parent_id in parent_ids:
        parent = conn.execute(
            "SELECT owner_profile FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()
        ancestors = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? AND edge_role LIKE 'delegation_%'", (parent_id,)
        ).fetchall()
        if ancestors:
            owned_root = conn.execute(
                "SELECT 1 FROM tasks WHERE id IN (" + ",".join("?" for _ in ancestors) + ") AND owner_profile IS NOT NULL LIMIT 1",
                tuple(row["parent_id"] for row in ancestors),
            ).fetchone()
            if owned_root:
                raise OwnerCellGuardError("command-board delegation budget forbids grandchildren")
        if parent is None or not parent["owner_profile"]:
            continue
        contract_row = conn.execute(
            "SELECT contract_json FROM owner_cell_goals WHERE task_id=?", (parent_id,)
        ).fetchone()
        if contract_row is not None:
            contract = json.loads(contract_row["contract_json"] or "{}")
            expected = (contract.get("delegation_roles") or {}).get(role)
            if not expected or str(child_assignee or "").strip() != expected:
                raise OwnerCellGuardError(
                    f"{role} assignee must match the root Owner Cell contract"
                )
        existing = conn.execute(
            "SELECT l.edge_role FROM task_links l WHERE l.parent_id = ?",
            (parent_id,),
        ).fetchall()
        same_role = sum(row["edge_role"] == edge_role for row in existing)
        if same_role:
            label = "one reviewer" if role == "reviewer" else "one maker"
            raise OwnerCellGuardError(f"command-board delegation budget permits {label}")


def assert_disposable_db_path(path: str | Path) -> Path:
    """Fail closed before a test/canary writes a canonical board database."""
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    canonical = {Path.home() / ".hermes" / "kanban.db"}
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned:
        canonical.add(Path(os.path.abspath(Path(pinned).expanduser())))
    # Reject known canonical paths lexically before resolving: even a metadata
    # probe of the real board is outside a disposable test's authority.
    if candidate in canonical or "/kanban/boards/" in str(candidate):
        raise OwnerCellGuardError("canonical board selected; disposable DB is required")
    candidate = candidate.resolve(strict=False)
    resolved_canonical = {
        item.parent.resolve(strict=False) / item.name for item in canonical
    }
    if pinned:
        resolved_canonical.add(Path(pinned).expanduser().resolve(strict=False))
    if candidate in resolved_canonical or "/kanban/boards/" in str(candidate):
        raise OwnerCellGuardError("canonical board selected; disposable DB is required")
    return candidate


def evaluate_churn(signals: Mapping[str, Any]) -> ChurnDecision:
    """Evaluate per-goal churn deterministically, with no LLM or side effects."""
    budget = signals.get("budget") or {}
    def ratio(name: str) -> float:
        # The public goal contract calls this dimension ``handoffs`` while
        # runtime snapshots use the explicit ``cross_owner_handoffs`` signal.
        # Honor either spelling so the actual dispatcher cannot silently omit
        # a handoff budget from the circuit-breaker calculation.
        budget_name = "handoffs" if name == "cross_owner_handoffs" and name not in budget else name
        ceiling = float(budget.get(budget_name, 0) or 0)
        return float(signals.get(name, 0) or 0) / ceiling if ceiling > 0 else 0.0
    burn = max(ratio("cards"), ratio("cross_owner_handoffs"), ratio("elapsed_seconds"), ratio("non_cache_tokens"))
    no_progress = int(signals.get("acceptance_gaps_closed", 0) or 0) < int(budget.get("acceptance_gaps_required", 1) or 0)
    failed_loops = int(signals.get("failed_review_loops", 0) or 0)
    warning = int(signals.get("cross_owner_handoffs", 0) or 0) >= 1
    if failed_loops >= 2 and not signals.get("senior_recovery_bound", False):
        action, stop, reason = "senior_consolidation", True, "two failed same-card loops require one senior owner"
    elif burn >= 1.0:
        action, stop, reason = "stop_spawn", True, "100% orchestration budget reached"
    elif burn >= 0.75:
        action, stop, reason = "pause_spawn", False, "75% orchestration budget requires recalibration"
    elif burn >= 0.50 and no_progress:
        action, stop, reason = "require_progress", False, "50% orchestration budget without acceptance progress"
    else:
        action, stop, reason = "allow", False, "within budget"
    key = _digest({"goal_id": str(signals.get("goal_id") or ""), "action": action}) if action in {"stop_spawn", "senior_consolidation"} else None
    return ChurnDecision(action, stop, warning, key, reason)


def install_schema(conn: Any) -> None:
    """Install only additive Owner Cell v1 tables and indexes."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS owner_cell_goals (
      task_id TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL UNIQUE,
      owner_profile TEXT NOT NULL,
      owner_role_alias TEXT NOT NULL,
      runtime_profile TEXT NOT NULL,
      security_realm TEXT NOT NULL,
      router_mode TEXT NOT NULL CHECK(router_mode IN ('script','single','reviewed','fanout')),
      risk_class TEXT NOT NULL,
      phase TEXT NOT NULL DEFAULT 'execute',
      capability_bundle TEXT NOT NULL,
      reviewer_profile TEXT,
      required_reviewer_profile TEXT,
      senior_owner_profile TEXT,
      review_loop_count INTEGER NOT NULL DEFAULT 0,
      current_state_version INTEGER NOT NULL DEFAULT 1,
      worker_budget_json TEXT NOT NULL,
      callback_json TEXT NOT NULL,
      approval_gates_json TEXT NOT NULL,
      contract_json TEXT NOT NULL,
      scope_fingerprint TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_owner_cell_goals_scope ON owner_cell_goals(security_realm, scope_fingerprint);
    CREATE TABLE IF NOT EXISTS goal_state_versions (
      goal_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      packet_json TEXT NOT NULL,
      packet_sha256 TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      created_by TEXT NOT NULL,
      PRIMARY KEY(goal_id, version)
    );
    CREATE TABLE IF NOT EXISTS task_targets (
      task_id TEXT NOT NULL,
      target_key TEXT NOT NULL,
      access_mode TEXT NOT NULL CHECK(access_mode IN ('read','mutate','read_exclusive')),
      expected_version TEXT,
      PRIMARY KEY(task_id, target_key)
    );
    CREATE TABLE IF NOT EXISTS target_registry (
      target_key TEXT PRIMARY KEY,
      security_realm TEXT NOT NULL,
      owner_profile TEXT NOT NULL,
      active_goal_id TEXT,
      target_version TEXT,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS target_leases (
      target_key TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      run_id INTEGER NOT NULL,
      lease_token TEXT NOT NULL,
      owner_profile TEXT NOT NULL,
      expected_state_version INTEGER NOT NULL,
      acquired_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,
      last_heartbeat_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS review_verdicts (
      task_id TEXT NOT NULL,
      review_round INTEGER NOT NULL,
      reviewer_profile TEXT NOT NULL,
      state_version INTEGER NOT NULL,
      artifact_ref TEXT NOT NULL,
      artifact_digest TEXT NOT NULL,
      verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REQUEST_CHANGES','FAIL_CLOSED')),
      defects_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY(task_id, review_round)
    );
    CREATE TABLE IF NOT EXISTS task_deliverables (
      goal_id TEXT NOT NULL,
      deliverable_key TEXT NOT NULL,
      task_id TEXT NOT NULL UNIQUE,
      target_keys_json TEXT NOT NULL,
      rationale TEXT NOT NULL,
      merge_contract TEXT NOT NULL,
      accepted_digest TEXT,
      PRIMARY KEY(goal_id, deliverable_key)
    );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(owner_cell_goals)")}
    for name, definition in {
        "review_artifact_ref": "TEXT",
        "review_artifact_digest": "TEXT",
        "review_state_version": "INTEGER",
        "required_reviewer_profile": "TEXT",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE owner_cell_goals ADD COLUMN {name} {definition}")


def select_router_mode(signals: Mapping[str, Any]) -> RouterDecision:
    """Pure, deterministic shadow routing; protected gates remain an overlay."""
    extras = signals.get("extra_workers") or []
    if extras:
        return RouterDecision("fanout", "material", "independent deliverables requested")
    if any(bool(signals.get(key)) for key in ("review_required", "material_code", "consequential", "privacy_risk", "security_risk", "explicit_verdict")):
        return RouterDecision("reviewed", "material", "independent review trigger")
    if bool(signals.get("deterministic")) and bool(signals.get("read_only") or signals.get("preapproved_reversible")):
        return RouterDecision("script", "low", "deterministic bounded operation")
    return RouterDecision("single", "low", "one bounded owner is sufficient")


def requires_human_approval(gates: Iterable[str]) -> bool:
    return bool({str(g).strip() for g in gates} & PROTECTED_GATES)


def is_frontstage_event(kind: str) -> bool:
    return str(kind or "").strip() in FRONTSTAGE_EVENTS


def _ensure_text(contract: Mapping[str, Any], field: str) -> str:
    value = str(contract.get(field) or "").strip()
    if not value:
        raise OwnerCellGuardError(f"missing required owner-cell field: {field}")
    return value


def _validate_targets(targets: Any) -> list[dict[str, str]]:
    if not isinstance(targets, list):
        raise OwnerCellGuardError("targets must be a list")
    cleaned: list[dict[str, str]] = []
    for target in targets:
        if not isinstance(target, Mapping):
            raise OwnerCellGuardError("target must be an object")
        key = str(target.get("target_key") or "").strip()
        access = str(target.get("access_mode") or "").strip()
        if not key or access not in VALID_ACCESS_MODES:
            raise OwnerCellGuardError("target requires canonical key and valid access mode")
        cleaned.append({"target_key": key, "access_mode": access, "expected_version": str(target.get("expected_version") or "")})
    return cleaned


def _owner_acceptance_enabled(contract: Mapping[str, Any]) -> bool:
    owner_acceptance = contract.get("owner_acceptance")
    return (
        isinstance(owner_acceptance, Mapping)
        and owner_acceptance.get("mode") == "accountable_owner"
    )


def owner_acceptance_allows(
    contract: Mapping[str, Any], reviewer_profile: str,
) -> bool:
    """Return whether this exact contract authorizes owner acceptance."""
    owner_profile = str(contract.get("owner_profile") or "").strip()
    maker_profile = str(contract.get("runtime_profile") or "").strip()
    return (
        _owner_acceptance_enabled(contract)
        and str(reviewer_profile or "").strip() == owner_profile
        and bool(owner_profile)
        and owner_profile != maker_profile
    )


def validate_goal_contract(contract: Mapping[str, Any]) -> None:
    for field in ("goal_id", "objective", "owner_profile", "owner_role_alias", "runtime_profile", "security_realm", "router_mode", "risk_class", "capability_bundle"):
        _ensure_text(contract, field)
    mode = str(contract["router_mode"])
    if mode not in VALID_MODES:
        raise OwnerCellGuardError("invalid router mode")
    if not isinstance(contract.get("callback"), Mapping) or not str(contract["callback"].get("return_surface") or "").strip():
        raise OwnerCellGuardError("missing callback return surface")
    if not isinstance(contract.get("acceptance_test"), Mapping) or not isinstance(contract.get("rollback"), Mapping):
        raise OwnerCellGuardError("missing acceptance test or rollback")
    budget = contract.get("worker_budget")
    if not isinstance(budget, Mapping):
        raise OwnerCellGuardError("missing worker budget")
    producers, reviewers, extras = (int(budget.get(key, 0)) for key in ("producers", "reviewers", "extras"))
    if producers != 1 or reviewers not in (0, 1) or extras < 0:
        raise OwnerCellGuardError("default worker budget permits one producer and at most one reviewer")
    targets = _validate_targets(contract.get("targets", []))
    if mode == "reviewed" and reviewers != 1:
        raise OwnerCellGuardError("reviewed mode requires one reviewer")
    if mode == "reviewed":
        required_reviewer = _ensure_text(contract, "required_reviewer_profile")
        owner_profile = str(contract["owner_profile"]).strip()
        runtime_profile = str(contract["runtime_profile"]).strip()
        owner_acceptance = _owner_acceptance_enabled(contract)
        owner_acceptance_allowed = owner_acceptance_allows(contract, required_reviewer)
        if required_reviewer == runtime_profile or (
            required_reviewer == owner_profile and not owner_acceptance_allowed
        ):
            raise OwnerCellGuardError("reviewed mode requires reviewer distinct from owner and maker")
        if owner_acceptance and not owner_acceptance_allowed:
            raise OwnerCellGuardError(
                "owner acceptance requires accountable owner reviewer distinct from maker"
            )
    extra_workers = contract.get("extra_workers") or []
    if mode != "fanout" and extra_workers:
        raise OwnerCellGuardError("extra workers require fanout mode")
    if mode == "fanout":
        if extras != len(extra_workers) or not extra_workers:
            raise OwnerCellGuardError("fanout requires a budgeted independent extra worker list")
        keys: set[str] = set()
        mutation_targets = {target["target_key"] for target in targets if target["access_mode"] == "mutate"}
        for worker in extra_workers:
            if not isinstance(worker, Mapping):
                raise OwnerCellGuardError("fanout worker must be an object")
            deliverable = str(worker.get("deliverable_key") or "").strip()
            rationale = str(worker.get("rationale") or "").strip()
            merge = str(worker.get("merge_contract") or "").strip()
            worker_targets = worker.get("target_keys") or []
            if not deliverable or deliverable in keys or not rationale or not merge or not worker_targets:
                raise OwnerCellGuardError("fanout workers require unique independent deliverable rationale and merge contract")
            keys.add(deliverable)
            overlap = mutation_targets.intersection({str(x) for x in worker_targets})
            if overlap:
                raise OwnerCellGuardError("fanout workers must have independent non-overlapping mutation targets")
            mutation_targets.update(str(x) for x in worker_targets)


def _scope_fingerprint(contract: Mapping[str, Any], targets: list[dict[str, str]]) -> str:
    return _digest({
        "realm": contract["security_realm"],
        "objective": " ".join(str(contract["objective"]).lower().split()),
        "targets": sorted(target["target_key"] for target in targets),
        "acceptance": contract.get("acceptance_test"),
    })


def create_goal(conn: Any, task_id: str, contract: Mapping[str, Any], *, created_by: str = "owner-cell") -> None:
    validate_goal_contract(contract)
    task_id = str(task_id).strip()
    if not task_id:
        raise OwnerCellGuardError("task_id is required")
    targets = _validate_targets(contract.get("targets", []))
    now = _now()
    # Target ownership is stronger than dedupe: a differently scoped goal
    # must receive the concrete target-collision reason instead of looking
    # like an ordinary duplicate.
    for target in targets:
        if target["access_mode"] == "mutate":
            busy = conn.execute("SELECT active_goal_id FROM target_registry WHERE target_key = ?", (target["target_key"],)).fetchone()
            if busy and busy["active_goal_id"] and busy["active_goal_id"] != contract["goal_id"]:
                raise OwnerCellGuardError("target already owned by an active goal")
    scope = _scope_fingerprint(contract, targets)
    clash = conn.execute(
        "SELECT task_id FROM owner_cell_goals WHERE security_realm = ? AND scope_fingerprint = ? AND phase NOT IN ('completed','blocked')",
        (contract["security_realm"], scope),
    ).fetchone()
    if clash:
        raise OwnerCellGuardError(f"duplicate active owner-cell scope: {clash['task_id']}")
    packet = {"version": 1, "facts": [], "artifact_refs": [], "target_versions": {target["target_key"]: target["expected_version"] for target in targets}}
    conn.execute(
        """INSERT INTO owner_cell_goals(task_id,goal_id,owner_profile,owner_role_alias,runtime_profile,security_realm,router_mode,risk_class,capability_bundle,reviewer_profile,required_reviewer_profile,senior_owner_profile,worker_budget_json,callback_json,approval_gates_json,contract_json,scope_fingerprint,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, contract["goal_id"], contract["owner_profile"], contract["owner_role_alias"], contract["runtime_profile"], contract["security_realm"], contract["router_mode"], contract["risk_class"], contract["capability_bundle"], None, contract.get("required_reviewer_profile"), contract.get("senior_owner_profile"), _canonical_json(contract["worker_budget"]), _canonical_json(contract["callback"]), _canonical_json(contract.get("approval_gates") or []), _canonical_json(dict(contract)), scope, now, now),
    )
    conn.execute("INSERT INTO goal_state_versions(goal_id,version,packet_json,packet_sha256,created_at,created_by) VALUES(?,?,?,?,?,?)", (contract["goal_id"], 1, _canonical_json(packet), _digest(packet), now, created_by))
    for target in targets:
        conn.execute("INSERT INTO task_targets(task_id,target_key,access_mode,expected_version) VALUES(?,?,?,?)", (task_id, target["target_key"], target["access_mode"], target["expected_version"] or None))
        if target["access_mode"] == "mutate":
            conn.execute("INSERT OR REPLACE INTO target_registry(target_key,security_realm,owner_profile,active_goal_id,target_version,updated_at) VALUES(?,?,?,?,?,?)", (target["target_key"], contract["security_realm"], contract["owner_profile"], contract["goal_id"], target["expected_version"] or None, now))


def goal_projection(conn: Any, task_id: str) -> Any:
    row = conn.execute("SELECT * FROM owner_cell_goals WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise OwnerCellGuardError("owner-cell goal not found")
    return row


def recover_owner_acceptance_senior_review(
    conn: Any,
    task_id: str,
    *,
    expected_contract_sha256: str,
    expected_state_version: int,
    expected_review_loop_count: int,
    expected_task_status: str,
    expected_owner_profile: str,
    expected_maker_profile: str,
    expected_reviewer_profile: str,
    expected_review_artifact_ref: str,
    expected_review_artifact_digest: str,
    evidence_comment_ids: Iterable[int],
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Repair one idle owner-acceptance review with missing senior metadata.

    This operator-only path binds the already-accountable owner as senior and
    restores the existing review projection.  It never records a verdict,
    changes the review loop/state, dispatches a worker, or creates a card.
    """
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    task_id = str(task_id or "").strip()
    expected_owner_profile = str(expected_owner_profile or "").strip()
    expected_maker_profile = str(expected_maker_profile or "").strip()
    expected_reviewer_profile = str(expected_reviewer_profile or "").strip()
    expected_review_artifact_ref = str(expected_review_artifact_ref or "").strip()
    expected_review_artifact_digest = str(expected_review_artifact_digest or "").strip()
    comment_ids = tuple(dict.fromkeys(int(value) for value in evidence_comment_ids))
    if not all((task_id, actor, reason, expected_contract_sha256,
                expected_owner_profile, expected_maker_profile,
                expected_reviewer_profile, expected_review_artifact_ref,
                expected_review_artifact_digest)) or not comment_ids:
        raise OwnerCellGuardError("complete operator expectations, actor, reason, and evidence comments are required")
    if expected_task_status not in {"triage", "blocked"}:
        raise OwnerCellGuardError("senior review recovery requires an expected held task status")

    conn.execute("SAVEPOINT recover_owner_acceptance_senior_review")
    try:
        row = goal_projection(conn, task_id)
        task = conn.execute(
            """SELECT id,status,assignee,current_run_id,worker_pid,claim_lock,
                      claim_expires,goal_id,owner_profile,runtime_profile,
                      router_mode,reviewer_profile,current_state_version
                 FROM tasks WHERE id=?""",
            (task_id,),
        ).fetchone()
        if task is None or task["status"] != expected_task_status:
            raise OwnerCellGuardError("task status changed before senior review recovery")
        if any(task[key] is not None for key in (
            "current_run_id", "worker_pid", "claim_lock", "claim_expires"
        )):
            raise OwnerCellGuardError("senior review recovery requires an unclaimed idle task")
        if conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1",
            (task_id,),
        ).fetchone() is not None:
            raise OwnerCellGuardError("senior review recovery cannot race an active run")
        if conn.execute(
            "SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=? LIMIT 1",
            (task_id, _now()),
        ).fetchone() is not None:
            raise OwnerCellGuardError("senior review recovery cannot race an active target lease")

        original_contract_json = str(row["contract_json"] or "")
        original_contract_sha256 = hashlib.sha256(
            original_contract_json.encode("utf-8")
        ).hexdigest()
        if original_contract_sha256 != expected_contract_sha256:
            raise OwnerCellGuardError("contract changed before senior review recovery")
        if (
            int(row["current_state_version"]) != int(expected_state_version)
            or int(row["review_state_version"] or -1) != int(expected_state_version)
            or int(row["review_loop_count"]) != int(expected_review_loop_count)
        ):
            raise OwnerCellGuardError("state or review loop changed before senior review recovery")
        if expected_review_loop_count != 1:
            raise OwnerCellGuardError("only the missing senior before the second review decision may be recovered")

        contract = json.loads(original_contract_json)
        validate_goal_contract(contract)
        owner = expected_owner_profile
        maker = expected_maker_profile
        reviewer = expected_reviewer_profile
        roles = contract.get("delegation_roles") or {}
        projection_matches = (
            row["router_mode"] == task["router_mode"] == contract.get("router_mode") == "reviewed"
            and row["phase"] == "review"
            and row["goal_id"] == task["goal_id"] == contract.get("goal_id")
            and row["owner_profile"] == task["owner_profile"] == contract.get("owner_profile") == owner
            and row["required_reviewer_profile"] == row["reviewer_profile"] == reviewer
            and task["reviewer_profile"] == task["runtime_profile"] == row["runtime_profile"] == reviewer
            and task["assignee"] == reviewer
            and contract.get("required_reviewer_profile") == reviewer
            and contract.get("runtime_profile") == maker
            and roles.get("maker") == maker
            and roles.get("reviewer") == reviewer
            and owner == reviewer
            and maker != reviewer
            and task["current_state_version"] == row["current_state_version"]
        )
        if not projection_matches or not owner_acceptance_allows(contract, reviewer):
            raise OwnerCellGuardError("owner-acceptance contract and review projection are inconsistent")
        if row["senior_owner_profile"] is not None or contract.get("senior_owner_profile") is not None:
            raise OwnerCellGuardError("senior owner is already bound")
        if _installed_profile(owner) != owner or _installed_profile(maker) != maker:
            raise OwnerCellGuardError("existing owner and maker must be installed canonical profiles")
        if (
            row["review_artifact_ref"] != expected_review_artifact_ref
            or row["review_artifact_digest"] != expected_review_artifact_digest
        ):
            raise OwnerCellGuardError("review artifact binding changed before recovery")
        try:
            artifact_sha256 = _regular_file_sha256(expected_review_artifact_ref)
        except OSError as exc:
            raise OwnerCellGuardError("bound review artifact is unavailable or unsafe") from exc
        if artifact_sha256 != normalize_digest(expected_review_artifact_digest):
            raise OwnerCellGuardError("bound review artifact hash does not match its review digest")

        placeholders = ",".join("?" for _ in comment_ids)
        comments = conn.execute(
            "SELECT id,task_id,author FROM task_comments WHERE id IN (" + placeholders + ")",
            comment_ids,
        ).fetchall()
        by_id = {int(comment["id"]): comment for comment in comments}
        if set(by_id) != set(comment_ids) or any(
            str(by_id[value]["author"] or "").strip() != reviewer
            for value in comment_ids
        ):
            raise OwnerCellGuardError("review evidence comments must exist and be authored by the pinned reviewer")

        contract["senior_owner_profile"] = owner
        validate_goal_contract(contract)
        amended_contract_json = _canonical_json(contract)
        now = _now()
        goal_cursor = conn.execute(
            """UPDATE owner_cell_goals
                  SET senior_owner_profile=?, contract_json=?, updated_at=?
                WHERE task_id=? AND contract_json=?
                  AND current_state_version=? AND review_state_version=?
                  AND review_loop_count=? AND phase='review'
                  AND senior_owner_profile IS NULL""",
            (owner, amended_contract_json, now, task_id, original_contract_json,
             expected_state_version, expected_state_version,
             expected_review_loop_count),
        )
        if goal_cursor.rowcount != 1:
            raise OwnerCellGuardError("owner-cell goal changed during senior review recovery")
        task_cursor = conn.execute(
            """UPDATE tasks SET status='review'
                WHERE id=? AND status=? AND assignee=?
                  AND current_run_id IS NULL AND worker_pid IS NULL
                  AND claim_lock IS NULL AND claim_expires IS NULL
                  AND owner_profile=? AND runtime_profile=?
                  AND reviewer_profile=? AND current_state_version=?""",
            (task_id, expected_task_status, reviewer, owner, reviewer,
             reviewer, expected_state_version),
        )
        if task_cursor.rowcount != 1:
            raise OwnerCellGuardError("task changed during senior review recovery")

        payload = {
            "schema": "owner-cell-senior-review-recovery-v1",
            "actor": actor,
            "reason": reason,
            "task_id": task_id,
            "goal_id": row["goal_id"],
            "owner_profile": owner,
            "senior_owner_profile": owner,
            "maker_profile": maker,
            "reviewer_profile": reviewer,
            "previous_status": expected_task_status,
            "restored_status": "review",
            "state_version": expected_state_version,
            "review_loop_count": expected_review_loop_count,
            "review_artifact_digest": expected_review_artifact_digest,
            "review_artifact_ref_sha256": hashlib.sha256(
                expected_review_artifact_ref.encode("utf-8")
            ).hexdigest(),
            "evidence_comments": [
                {"id": value, "task_id": by_id[value]["task_id"], "author": reviewer}
                for value in comment_ids
            ],
            "previous_contract_sha256": original_contract_sha256,
            "amended_contract_sha256": hashlib.sha256(
                amended_contract_json.encode("utf-8")
            ).hexdigest(),
            "verdict_recorded": False,
            "dispatch_performed": False,
        }
        conn.execute(
            """INSERT INTO task_events(task_id,run_id,kind,payload,created_at)
               VALUES(?,NULL,'owner_cell_senior_review_recovered',?,?)""",
            (task_id, _canonical_json(payload), now),
        )
        conn.execute("RELEASE SAVEPOINT recover_owner_acceptance_senior_review")
        return payload
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT recover_owner_acceptance_senior_review")
        conn.execute("RELEASE SAVEPOINT recover_owner_acceptance_senior_review")
        raise


def count_goal_cards(conn: Any, goal_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM owner_cell_goals WHERE goal_id = ?", (goal_id,)).fetchone()[0])


def update_state(conn: Any, task_id: str, *, expected_version: int, facts: list[str], artifact_refs: list[str] | None = None, created_by: str = "owner-cell") -> int:
    row = goal_projection(conn, task_id)
    if int(row["current_state_version"]) != int(expected_version):
        raise OwnerCellGuardError("stale state version")
    packet = {"version": expected_version + 1, "facts": list(facts), "artifact_refs": list(artifact_refs or []), "target_versions": {target["target_key"]: target["expected_version"] for target in conn.execute("SELECT target_key, expected_version FROM task_targets WHERE task_id = ?", (task_id,))}}
    now = _now()
    conn.execute("INSERT INTO goal_state_versions(goal_id,version,packet_json,packet_sha256,created_at,created_by) VALUES(?,?,?,?,?,?)", (row["goal_id"], expected_version + 1, _canonical_json(packet), _digest(packet), now, created_by))
    conn.execute("UPDATE owner_cell_goals SET current_state_version = ?, updated_at = ? WHERE task_id = ?", (expected_version + 1, now, task_id))
    return expected_version + 1


def begin_review(conn: Any, task_id: str, *, reviewer_profile: str, artifact_ref: str, artifact_digest: str) -> None:
    row = goal_projection(conn, task_id)
    if row["router_mode"] != "reviewed":
        raise OwnerCellGuardError("review is only valid for reviewed goals")
    required = str(row["required_reviewer_profile"] or "").strip()
    if not required or reviewer_profile != required:
        raise OwnerCellGuardError("review must use the required reviewer from routing metadata")
    contract = json.loads(row["contract_json"] or "{}")
    owner_acceptance = _owner_acceptance_enabled(contract)
    owner_acceptance_allowed = owner_acceptance_allows(contract, reviewer_profile)
    if reviewer_profile == str(contract.get("runtime_profile") or "").strip() or (
        reviewer_profile == row["owner_profile"] and not owner_acceptance_allowed
    ):
        raise OwnerCellGuardError("reviewer must be distinct from owner and maker")
    if owner_acceptance and not owner_acceptance_allowed:
        raise OwnerCellGuardError(
            "owner acceptance requires accountable owner reviewer distinct from maker"
        )
    existing = row["reviewer_profile"]
    if existing and existing != reviewer_profile:
        raise OwnerCellGuardError("same reviewer must remain pinned")
    if not artifact_ref or not artifact_digest:
        raise OwnerCellGuardError("review artifact reference and digest are required")
    conn.execute(
        """UPDATE owner_cell_goals
              SET reviewer_profile = ?, runtime_profile = ?, phase = 'review',
                  review_artifact_ref = ?, review_artifact_digest = ?,
                  review_state_version = ?, updated_at = ?
            WHERE task_id = ?""",
        (reviewer_profile, reviewer_profile, artifact_ref, artifact_digest,
         int(row["current_state_version"]), _now(), task_id),
    )


def rebind_review_artifact(
    conn: Any,
    task_id: str,
    *,
    reviewer_profile: str,
    state_version: int,
    expected_artifact_ref: str,
    expected_artifact_digest: str,
    artifact_ref: str,
    artifact_digest: str,
) -> None:
    """CAS-refresh review evidence without creating a content-repair loop."""
    row = goal_projection(conn, task_id)
    if row["router_mode"] != "reviewed" or row["phase"] != "review":
        raise OwnerCellGuardError("artifact rebind is only valid during reviewed goal review")
    if row["reviewer_profile"] != reviewer_profile:
        raise OwnerCellGuardError("artifact rebind must use the pinned reviewer")
    if (
        int(row["current_state_version"]) != int(state_version)
        or int(row["review_state_version"] or -1) != int(state_version)
    ):
        raise OwnerCellGuardError("artifact rebind is bound to stale state")
    if (
        row["review_artifact_ref"] != expected_artifact_ref
        or row["review_artifact_digest"] != expected_artifact_digest
    ):
        raise OwnerCellGuardError("artifact rebind expected binding does not match current review")
    if not artifact_ref or not artifact_digest:
        raise OwnerCellGuardError("artifact rebind reference and digest are required")
    if artifact_ref == expected_artifact_ref and artifact_digest == expected_artifact_digest:
        raise OwnerCellGuardError("artifact rebind must change the review evidence")
    cur = conn.execute(
        """
        UPDATE owner_cell_goals
           SET review_artifact_ref = ?, review_artifact_digest = ?, updated_at = ?
         WHERE task_id = ?
           AND review_artifact_ref = ? AND review_artifact_digest = ?
           AND review_state_version = ? AND current_state_version = ?
        """,
        (
            artifact_ref,
            artifact_digest,
            _now(),
            task_id,
            expected_artifact_ref,
            expected_artifact_digest,
            state_version,
            state_version,
        ),
    )
    if cur.rowcount != 1:
        raise OwnerCellGuardError("artifact rebind lost its exact current binding")


def record_verdict(conn: Any, task_id: str, *, reviewer_profile: str, state_version: int, artifact_ref: str, artifact_digest: str, verdict: str, defects: list[str]) -> None:
    row = goal_projection(conn, task_id)
    if verdict not in VALID_VERDICTS:
        raise OwnerCellGuardError("invalid structured verdict")
    if row["reviewer_profile"] != reviewer_profile:
        raise OwnerCellGuardError("reviewer is not pinned to this goal")
    if int(row["current_state_version"]) != int(state_version) or int(row["review_state_version"] or -1) != int(state_version):
        raise OwnerCellGuardError("review verdict is bound to stale state")
    if row["review_artifact_ref"] != artifact_ref or row["review_artifact_digest"] != artifact_digest:
        raise OwnerCellGuardError("review verdict artifact does not match the requested review")
    # Non-file evidence references remain supported. Native local artifacts must
    # match actual bytes before a PASS can be persisted or the claim completed.
    if verdict == "PASS" and Path(artifact_ref).is_absolute():
        try:
            actual_digest = _regular_file_sha256(artifact_ref)
        except OSError as exc:
            raise OwnerCellGuardError("review artifact is unavailable for completion") from exc
        if actual_digest != normalize_digest(artifact_digest):
            raise OwnerCellGuardError("review artifact bytes differ from bound digest; reviewer rebind required")
    round_number = int(row["review_loop_count"]) + 1
    conn.execute("INSERT OR REPLACE INTO review_verdicts(task_id,review_round,reviewer_profile,state_version,artifact_ref,artifact_digest,verdict,defects_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, round_number, reviewer_profile, state_version, artifact_ref, artifact_digest, verdict, _canonical_json(defects), _now()))


def _latest_verdict(conn: Any, task_id: str) -> Any:
    return conn.execute("SELECT * FROM review_verdicts WHERE task_id = ? ORDER BY review_round DESC", (task_id,)).fetchone()


def authorize_diagnosed_rework(conn: Any, task_id: str, *, expected_run_id: int,
        review_decision_capability: str, brief_ref: str, brief_digest: str,
        summary: str, acceptance_criteria: list[str]) -> dict:
    """One reserved, diagnosed correction after ordinary rework is exhausted.

    Only the current authenticated owner-reviewer can use this path. It never
    makes that reviewer a producer, resets counters, or substitutes for review.
    """
    from hermes_cli import kanban_db as kb
    if not isinstance(summary, str) or len(summary.strip()) < 20:
        raise OwnerCellGuardError('a concrete root-cause diagnosis is required')
    if (not isinstance(acceptance_criteria, list) or len(acceptance_criteria) < 2
            or any(not isinstance(x, str) or len(x.strip()) < 10 for x in acceptance_criteria)):
        raise OwnerCellGuardError('provide at least two concrete verification criteria')
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        row = goal_projection(conn, task_id)
        if not task or row is None:
            raise OwnerCellGuardError('existing owner-reviewed task required')
        contract = json.loads(row['contract_json'])
        validate_goal_contract(contract)
        if not review_decision_capability:
            raise OwnerCellGuardError('diagnosed correction requires authenticated current review authority')
        kb._validate_review_decision_capability(conn, task_id, review_decision_capability,
                                                expected_run_id=expected_run_id)
        owner = contract['owner_profile']
        limit = int((contract.get('worker_budget') or {}).get('max_rework_rounds', 6))
        if (task.status != 'running' or task.current_run_id != expected_run_id
                or row['phase'] != 'review' or task.assignee != owner
                or row['reviewer_profile'] != owner or row['required_reviewer_profile'] != owner
                or not owner_acceptance_allows(contract, owner)
                or row['review_state_version'] != row['current_state_version']
                or not row['review_artifact_digest'] or not row['review_artifact_ref']
                or int(row['review_loop_count']) != limit):
            raise OwnerCellGuardError('diagnosed correction requires the exhausted, current owner review')
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='diagnosed_rework_authorized'", (task_id,)).fetchone():
            raise OwnerCellGuardError('the single diagnosed correction is already authorized or spent')
        workspace = Path(task.workspace_path or '').resolve()
        brief = Path(brief_ref or '').resolve()
        if not task.workspace_path or not brief.is_relative_to(workspace) or not brief.is_file():
            raise OwnerCellGuardError('diagnosis must be a file inside the existing task workspace')
        if brief.stat().st_size > 131072:
            raise OwnerCellGuardError('diagnosis must be a bounded JSON packet')
        raw = brief.read_bytes()
        if hashlib.sha256(raw).hexdigest() != brief_digest:
            raise OwnerCellGuardError('diagnosis digest mismatch')
        try:
            diagnosis = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise OwnerCellGuardError('diagnosis must be JSON') from exc
        if not isinstance(diagnosis, dict) or any(
                not isinstance(diagnosis.get(key), str) or len(diagnosis[key].strip()) < 20
                for key in ('root_cause', 'source_evidence', 'changed_method', 'verification')):
            raise OwnerCellGuardError('diagnosis needs root_cause, source_evidence, changed_method and verification')
        payload = {'owner': owner, 'maker': contract['runtime_profile'],
            'state_version': row['current_state_version'], 'artifact_ref': row['review_artifact_ref'],
            'artifact_digest': row['review_artifact_digest'], 'from_review_count': row['review_loop_count'],
            'contract_digest': hashlib.sha256(row['contract_json'].encode()).hexdigest(),
            'brief_ref': str(brief), 'brief_digest': brief_digest,
            'summary': summary.strip(), 'acceptance_criteria': acceptance_criteria}
        kb._append_event(conn, task_id, 'diagnosed_rework_authorized', payload, run_id=expected_run_id)
        return {'status':'diagnosed_rework_authorized','task_id':task_id,
            'next_action':'Request changes on this same card with the diagnosed method. Original maker and all reviews remain required.'}


def authorize_operator_rework(conn: Any, task_id: str, *, authorization_ref: str,
        authorization_text: str, brief_ref: str, brief_digest: str) -> dict:
    """Record one explicit operator-directed correction, never an automatic retry.

    This is deliberately absent from worker tools. The operator must have a
    fresh user instruction; recording it neither supplies a reviewer verdict
    nor claims that the proposed document correction is sound.
    """
    import os
    from hermes_cli import kanban_db as kb
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise OwnerCellGuardError('operator authorization is unavailable to task workers')
    if not all(isinstance(x, str) and x.strip() for x in (authorization_ref, authorization_text)):
        raise OwnerCellGuardError('explicit user authorization reference and text are required')
    authorization_ref = authorization_ref.strip()
    authorization_text = authorization_text.strip()
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        row = goal_projection(conn, task_id)
        if not task or row is None:
            raise OwnerCellGuardError('existing reviewed task required')
        contract = json.loads(row['contract_json'])
        validate_goal_contract(contract)
        limit = int((contract.get('worker_budget') or {}).get('max_rework_rounds', 6))
        if (row['phase'] != 'review' or task.status not in ('running', 'review', 'blocked', 'triage')
                or not owner_acceptance_allows(contract, row['reviewer_profile'])
                or row['reviewer_profile'] != row['required_reviewer_profile']
                or row['review_state_version'] != row['current_state_version']
                or int(row['review_loop_count']) < limit
                or not row['review_artifact_ref'] or not row['review_artifact_digest']):
            raise OwnerCellGuardError('operator correction requires an exhausted, bound owner review')
        prior = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='operator_rework_authorized'", (task_id,)).fetchall()
        for event in prior:
            data = json.loads(event['payload'])
            if data['authorization_ref'] == authorization_ref:
                raise OwnerCellGuardError('this user authorization has already been used')
            if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='operator_rework_consumed' AND json_extract(payload,'$.authorization_event_id')=?", (task_id, event['id'])).fetchone():
                raise OwnerCellGuardError('an operator correction is already pending')
        brief = Path(brief_ref or '').resolve()
        workspace = Path(task.workspace_path or '').resolve()
        if (not task.workspace_path or not brief.is_relative_to(workspace)
                or not brief.is_file() or brief.stat().st_size > 131072
                or hashlib.sha256(brief.read_bytes()).hexdigest() != brief_digest):
            raise OwnerCellGuardError('exact bounded workspace correction brief is required')
        payload = {'authorization_ref': authorization_ref.strip(),
            'authorization_text': kb.redact_review_value(authorization_text.strip()),
            'owner': row['reviewer_profile'], 'maker': contract['runtime_profile'],
            'from_review_count': row['review_loop_count'], 'state_version': row['current_state_version'],
            'artifact_ref': row['review_artifact_ref'], 'artifact_digest': row['review_artifact_digest'],
            'contract_digest': hashlib.sha256(row['contract_json'].encode()).hexdigest(),
            'brief_ref': str(brief), 'brief_digest': brief_digest}
        kb._append_event(conn, task_id, 'operator_rework_authorized', payload)
        return {'status': 'operator_rework_authorized', 'task_id': task_id,
                'next_action': 'Current authenticated reviewer must issue fresh same-card request_changes.'}


def _operator_rework_grant(conn: Any, task_id: str, row: Any) -> dict | None:
    event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='operator_rework_authorized' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if event is None or conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='operator_rework_consumed' AND json_extract(payload,'$.authorization_event_id')=?", (task_id, event['id'])).fetchone():
        return None
    try:
        grant = json.loads(event['payload'])
    except (TypeError, ValueError):
        return None
    if not isinstance(grant, dict):
        return None
    expected = {'from_review_count': row['review_loop_count'], 'state_version': row['current_state_version'],
        'artifact_ref': row['review_artifact_ref'], 'artifact_digest': row['review_artifact_digest'],
        'contract_digest': hashlib.sha256(row['contract_json'].encode()).hexdigest(),
        'owner': row['reviewer_profile']}
    if any(grant.get(key) != value for key, value in expected.items()):
        raise OwnerCellGuardError('operator correction authorization is stale')
    task = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()
    brief = Path(grant.get('brief_ref') or '').resolve()
    if (not task['workspace_path'] or not brief.is_relative_to(Path(task['workspace_path']).resolve())
            or not brief.is_file() or brief.stat().st_size > 131072
            or hashlib.sha256(brief.read_bytes()).hexdigest() != grant.get('brief_digest')):
        raise OwnerCellGuardError('operator correction brief changed after authorization')
    return {**grant, 'authorization_event_id': event['id']}


def request_changes_same_card(conn: Any, task_id: str, *, expected_run_id: int | None = None,
        review_decision_capability: str | None = None) -> ReworkResult:
    row = goal_projection(conn, task_id)
    contract = json.loads(row["contract_json"] or "{}")
    limit = int((contract.get("worker_budget") or {}).get("max_rework_rounds", 6))
    if limit < 1 or limit > 6:
        raise OwnerCellGuardError("max_rework_rounds must be between 1 and 6")
    diagnosis = None
    operator_grant = None
    if int(row["review_loop_count"]) >= limit:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_events'").fetchone():
            raise OwnerCellGuardError('repair budget exhausted; accountable owner must diagnose before further execution')
        operator_grant = _operator_rework_grant(conn, task_id, row)
        if operator_grant:
            from hermes_cli import kanban_db as kb
            if not review_decision_capability:
                raise OwnerCellGuardError('operator correction requires authenticated current review authority')
            if not kb._validate_review_decision_capability(conn, task_id, review_decision_capability,
                    expected_run_id=expected_run_id):
                raise OwnerCellGuardError('operator correction requires authenticated current review authority')
        grant = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='diagnosed_rework_authorized' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        consumed = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='diagnosed_rework_consumed'", (task_id,)).fetchone()
        try:
            diagnosis = json.loads(grant['payload']) if grant and not consumed else None
        except (TypeError, ValueError):
            diagnosis = None
        if not isinstance(diagnosis, dict):
            diagnosis = None
        if not operator_grant and (not diagnosis or int(row['review_loop_count']) != limit
                or diagnosis.get('from_review_count') != row['review_loop_count']
                or diagnosis.get('state_version') != row['current_state_version']
                or diagnosis.get('artifact_ref') != row['review_artifact_ref']
                or diagnosis.get('artifact_digest') != row['review_artifact_digest']
                or diagnosis.get('contract_digest') != hashlib.sha256(row['contract_json'].encode()).hexdigest()
                or diagnosis.get('owner') != row['reviewer_profile']):
            raise OwnerCellGuardError('repair budget exhausted; owner-reviewer may use kanban_handoff action=diagnosed_rework once with a source-backed diagnosis and changed verification method; no counter reset or review waiver')
        diagnosis = operator_grant or diagnosis
        task = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()
        brief = Path(diagnosis.get('brief_ref') or '').resolve()
        workspace = Path(task['workspace_path'] or '').resolve()
        if (not task['workspace_path'] or not brief.is_relative_to(workspace)
                or not brief.is_file() or brief.stat().st_size > 131072
                or hashlib.sha256(brief.read_bytes()).hexdigest() != diagnosis.get('brief_digest')):
            raise OwnerCellGuardError('diagnosis changed after authorization')
    if row["phase"] != "review":
        raise OwnerCellGuardError("repair loop limit: a fresh review decision is required before another correction")
    verdict = _latest_verdict(conn, task_id)
    if verdict is None or verdict["verdict"] != "REQUEST_CHANGES":
        raise OwnerCellGuardError("same-card rework requires a current REQUEST_CHANGES verdict")
    if int(verdict["state_version"]) != int(row["current_state_version"]):
        raise OwnerCellGuardError("request changes verdict is stale")
    # Reworded review prose is not progress. Never spend another maker run on
    # bytes already rejected in an earlier round of this same goal.
    if conn.execute(
        "SELECT 1 FROM review_verdicts WHERE task_id=? AND verdict='REQUEST_CHANGES' "
        "AND review_round<? AND artifact_digest=? LIMIT 1",
        (task_id, verdict["review_round"], verdict["artifact_digest"]),
    ).fetchone() is not None:
        raise OwnerCellGuardError("unchanged rejected artifact; accountable owner must diagnose before retry")
    loops = int(row["review_loop_count"]) + 1
    owner = row["owner_profile"]
    phase = "rework"
    contract = json.loads(row["contract_json"] or "{}")
    # During review, begin_review() deliberately projects runtime_profile to
    # the reviewer so only the reviewer can hold the active review run. A
    # REQUEST_CHANGES verdict must route the same card back to the original
    # maker/runtime declared by the validated contract, while preserving the
    # accountable owner. Falling back to owner here strands distinct-maker
    # goals: the owner may be accountable but not the executable worker.
    runtime = str(contract.get("runtime_profile") or "").strip()
    if not runtime:
        raise OwnerCellGuardError("request changes requires the original maker runtime")
    if loops >= 2:
        senior = str(row["senior_owner_profile"] or "").strip()
        if not senior:
            # Missing optional escalation metadata must not strand a valid
            # same-card correction. Reuse the existing authorized owner;
            # do not promote an arbitrary profile or change the maker.
            validate_goal_contract(contract)
            senior = str(contract.get("owner_profile") or "").strip()
            if (
                not senior or senior != row["owner_profile"]
                or contract.get("required_reviewer_profile") != row["required_reviewer_profile"]
                or row["reviewer_profile"] != row["required_reviewer_profile"]
            ):
                raise OwnerCellGuardError("senior recovery requires consistent accountable owner and reviewer")
            contract["senior_owner_profile"] = senior
            contract["recovery_preserves_maker"] = True
            conn.execute(
                "UPDATE owner_cell_goals SET senior_owner_profile=?, contract_json=? WHERE task_id=?",
                (senior, _canonical_json(contract), task_id),
            )
        owner = senior
        # Accountable-owner acceptance deliberately allows the owner to be
        # the reviewer while a distinct contract maker does the execution.
        # On senior consolidation, preserve that maker boundary: the senior
        # owns the consolidated outcome without becoming their own maker.
        if (
            (contract.get("recovery_preserves_maker") is True
             and senior == str(contract.get("owner_profile") or "").strip())
            or (owner_acceptance_allows(contract, senior)
                and senior == str(row["required_reviewer_profile"] or "").strip())
        ):
            runtime = str(contract.get("runtime_profile") or "").strip()
        else:
            runtime = senior
        phase = "senior_consolidation"
    conn.execute("UPDATE owner_cell_goals SET owner_profile = ?, runtime_profile = ?, review_loop_count = ?, phase = ?, updated_at = ? WHERE task_id = ?", (owner, runtime, loops, phase, _now(), task_id))
    if operator_grant:
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, 'operator_rework_consumed', {
            'authorization_event_id': operator_grant['authorization_event_id'],
            'review_loop_count': loops, 'maker': runtime})
    elif diagnosis:
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, 'diagnosed_rework_consumed', {
            'brief_ref': diagnosis['brief_ref'], 'brief_digest': diagnosis['brief_digest'],
            'review_loop_count': loops, 'maker': runtime})
    return ReworkResult(
        phase=phase,
        owner_profile=owner,
        runtime_profile=runtime,
        review_loop_count=loops,
    )


def assert_completion_allowed(conn: Any, task_id: str) -> None:
    row = goal_projection(conn, task_id)
    if row["router_mode"] != "reviewed":
        return
    verdict = _latest_verdict(conn, task_id)
    if (
        verdict is None or verdict["verdict"] != "PASS"
        or int(verdict["state_version"]) != int(row["current_state_version"])
        or int(row["review_state_version"] or -1) != int(row["current_state_version"])
        or verdict["artifact_ref"] != row["review_artifact_ref"]
        or verdict["artifact_digest"] != row["review_artifact_digest"]
    ):
        raise OwnerCellGuardError("reviewed goal requires a current structured PASS verdict bound to its requested artifact before completion")


def dispatch_churn_decision(conn: Any, task_id: str) -> ChurnDecision | None:
    """Build a deterministic dispatcher snapshot for an opt-in Owner Cell goal."""
    row = conn.execute("SELECT * FROM owner_cell_goals WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    contract = json.loads(row["contract_json"])
    budget = contract.get("churn_budget") or {}
    if not budget:
        return None
    # Scheduled maker/reviewer/senior transitions are work, not routing churn.
    # Count only changes into an unplanned assignee, never repeat assignments
    # or ordinary review/request-changes events.
    planned = {str(contract.get(key) or "").strip() for key in (
        "owner_profile", "runtime_profile", "required_reviewer_profile",
        "senior_owner_profile",
    )} - {""}
    previous = str(contract.get("runtime_profile") or "").strip()
    handoffs = 0
    for event in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned' ORDER BY id",
        (task_id,),
    ):
        payload = json.loads(event["payload"] or "{}")
        assignee = str(payload.get("assignee") or "").strip()
        if assignee and assignee != previous:
            if assignee not in planned:
                handoffs += 1
            previous = assignee
    # The bounded second correction and its final review must be dispatchable.
    # request_changes_same_card still rejects a third correction; spending
    # limits below still apply to the authorized consolidation round.
    senior = str(row["senior_owner_profile"] or "").strip()
    recovery_bound = bool(
        2 <= int(row["review_loop_count"]) <= int((contract.get("worker_budget") or {}).get("max_rework_rounds", 6))
        and senior and senior == str(contract.get("senior_owner_profile") or "").strip()
        and row["owner_profile"] == senior
        and row["phase"] in {"senior_consolidation", "review"}
    )
    return evaluate_churn({
        "goal_id": row["goal_id"], "cards": count_goal_cards(conn, row["goal_id"]),
        "cross_owner_handoffs": handoffs, "elapsed_seconds": max(0, _now() - int(row["created_at"])),
        "non_cache_tokens": int((contract.get("churn_metrics") or {}).get("non_cache_tokens", 0)),
        "acceptance_gaps_closed": int((contract.get("churn_metrics") or {}).get("acceptance_gaps_closed", 0)),
        "failed_review_loops": int(row["review_loop_count"]), "budget": budget,
        "senior_recovery_bound": recovery_bound,
    })


def acquire_mutator_leases(conn: Any, task_id: str, *, run_id: int, expected_state_version: int, ttl_seconds: int = 900) -> list[str]:
    row = goal_projection(conn, task_id)
    if int(row["current_state_version"]) != int(expected_state_version):
        raise OwnerCellGuardError("stale state version before lease acquisition")
    targets = conn.execute("SELECT target_key FROM task_targets WHERE task_id = ? AND access_mode = 'mutate' ORDER BY target_key", (task_id,)).fetchall()
    now = _now()
    tokens: list[str] = []
    for target in targets:
        key = target["target_key"]
        existing = conn.execute("SELECT goal_id, run_id, expires_at FROM target_leases WHERE target_key = ?", (key,)).fetchone()
        if existing and int(existing["expires_at"]) >= now:
            raise OwnerCellGuardError("target already leased by another mutator")
        token = secrets.token_urlsafe(18)
        conn.execute("INSERT OR REPLACE INTO target_leases(target_key,goal_id,task_id,run_id,lease_token,owner_profile,expected_state_version,acquired_at,expires_at,last_heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (key, row["goal_id"], task_id, run_id, token, row["owner_profile"], expected_state_version, now, now + ttl_seconds, now))
        tokens.append(token)
    return tokens


def assert_mutation_fresh(conn: Any, task_id: str, lease_token: str, *, expected_state_version: int, observed_target_version: str) -> None:
    row = goal_projection(conn, task_id)
    if int(row["current_state_version"]) != int(expected_state_version):
        raise OwnerCellGuardError("stale state version before mutation")
    lease = conn.execute("SELECT * FROM target_leases WHERE task_id = ? AND lease_token = ?", (task_id, lease_token)).fetchone()
    if lease is None or int(lease["expires_at"]) < _now():
        raise OwnerCellGuardError("missing or expired mutator lease")
    registry = conn.execute("SELECT target_version FROM target_registry WHERE target_key = ?", (lease["target_key"],)).fetchone()
    if registry is None or str(registry["target_version"] or "") != str(observed_target_version or ""):
        raise OwnerCellGuardError("stale target version before mutation")


def assert_realm_access(conn: Any, task_id: str, security_realm: str, runtime_profile: str) -> None:
    row = goal_projection(conn, task_id)
    # Realm is the privacy wall and must match exactly. The runtime may be any
    # profile already bound to this same goal (maker, runtime, or its bound
    # reviewer); a drifted runtime field must not deadlock the owner's card.
    keys = row.keys() if hasattr(row, "keys") else ()
    bound = {row[k] for k in ("runtime_profile", "owner_profile",
                              "required_reviewer_profile", "reviewer_profile")
             if k in keys and row[k]}
    if row["security_realm"] != security_realm or runtime_profile not in bound:
        raise OwnerCellGuardError("privacy realm/runtime access rejected")


def callback_projection(conn: Any, task_id: str) -> dict[str, Any]:
    row = goal_projection(conn, task_id)
    return json.loads(row["callback_json"])

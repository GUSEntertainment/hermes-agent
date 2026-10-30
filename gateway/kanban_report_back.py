"""Trusted, Desk-first Kanban report-back policy.

This module resolves audience and privacy policy, then records local Desk/audit
intent.  It deliberately has no dependency on messaging adapters and cannot
perform a human-visible network delivery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.redact import redact_sensitive_text

_ROUTING_METADATA_RE = re.compile(
    r"## Routing metadata\s*```json\s*(\{.*?\})\s*```", re.DOTALL
)
_REPORT_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|(?:access|refresh)[_ -]?token|token|password|"
    r"(?:client[_ -]?)?secret|credential(?:s)?|authorization)\s*[:=]\s*\S+"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)"
)
_NOISE_EVENTS = {
    "claimed", "heartbeat", "retry", "internal_comment", "child_created",
    "crashed", "timed_out", "gave_up", "unblocked", "archived",
}
_PRIVACY_RANK = {
    "public": 0, "normal": 1, "team": 2, "company": 2,
    "creator_only": 3, "restricted": 4, "private": 4,
    "confidential": 5, "finance": 6, "personnel": 6, "family": 6,
    "legal": 7, "secrets": 8, "credentials": 8,
}
_SENSITIVE = {
    "creator_only", "restricted", "private", "confidential", "finance",
    "personnel", "family", "legal", "secrets", "credentials",
}


class ReportBackPolicyError(ValueError):
    """A report-back route could not be authenticated or validated."""


@dataclass(frozen=True)
class ReportBackDecision:
    task_id: str
    event_kind: str
    department_id: str
    department_owner: str
    runtime_assignee: str
    audience: str
    cockpit_board: Optional[str]
    source_session_key: str
    source_surface: str
    return_surface: str
    privacy_tier: str
    policy_mode: str
    desk_artifact_id: str
    idempotency_key: str
    telegram_allowed: bool
    telegram_reason: str
    dry_run: bool
    redacted_summary: str
    telegram_destination: dict[str, Any]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_routing_metadata(task: Any) -> dict[str, Any]:
    body = getattr(task, "body", None)
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


def _normalized_privacy(value: Any) -> str:
    token = re.sub(r"[\s/-]+", "_", str(value or "").strip().lower()).strip("_")
    return "team" if token in {"", "normal"} else token


def _most_restrictive(*values: Any) -> str:
    tiers = [_normalized_privacy(value) for value in values if str(value or "").strip()]
    if not tiers:
        return "team"
    return max(tiers, key=lambda tier: _PRIVACY_RANK.get(tier, 5))


def redact_report_text(text: str, *, privacy_tier: str = "team") -> tuple[str, list[str]]:
    safe = str(text or "")
    labels: list[str] = []
    redacted = redact_sensitive_text(safe, force=True)
    if redacted != safe:
        safe = redacted
        labels.append("secret")
    if _REPORT_LABELED_SECRET_RE.search(safe):
        safe = _REPORT_LABELED_SECRET_RE.sub("[REDACTED_SECRET]", safe)
        if "secret" not in labels:
            labels.append("secret")
    if _EMAIL_RE.search(safe):
        safe = _EMAIL_RE.sub("[REDACTED_EMAIL]", safe)
        labels.append("email")
    if _PHONE_RE.search(safe):
        safe = _PHONE_RE.sub("[REDACTED_PHONE]", safe)
        labels.append("phone")
    if _normalized_privacy(privacy_tier) in _SENSITIVE:
        lines = [line.strip() for line in safe.splitlines() if line.strip()]
        safe = lines[0][:240] if lines else "Restricted Desk update."
        labels.append("sensitive_summary")
    return safe[:700], labels


def _event_kind(event: Any) -> str:
    kind = str(getattr(event, "kind", "") or "")
    payload = _as_dict(getattr(event, "payload", None))
    if kind == "status" and payload.get("status") == "needs_approval":
        return "needs_approval"
    return kind


def is_reportable_event(event: Any) -> bool:
    kind = _event_kind(event)
    return kind not in _NOISE_EVENTS and kind in {"completed", "blocked", "needs_approval"}


def _cockpit_shaped(task: Any, metadata: Mapping[str, Any], board: Optional[str]) -> bool:
    values = [
        getattr(task, "tenant", ""), metadata.get("source_surface"),
        metadata.get("return_surface"), metadata.get("source_session_key"),
    ]
    return any(str(value or "").startswith("agent-os:cockpit") for value in values) or any(
        key in metadata for key in ("requesting_user", "audience", "cockpit_board")
    )


def _principal_lane(
    *, authenticated_principal: Optional[str], principal_registry: Any,
    metadata: Mapping[str, Any], board: Optional[str], task: Any,
) -> tuple[str, Optional[str]]:
    if not _cockpit_shaped(task, metadata, board):
        return "chris", None
    if not isinstance(authenticated_principal, str) or not authenticated_principal.strip():
        raise ReportBackPolicyError("cockpit report requires an authenticated principal")
    if not isinstance(principal_registry, Mapping):
        raise ReportBackPolicyError("cockpit principal registry is missing or malformed")
    principals = principal_registry.get("principals")
    if not isinstance(principals, Mapping):
        raise ReportBackPolicyError("cockpit principal registry is missing or malformed")
    lane = principals.get(authenticated_principal.strip())
    if not isinstance(lane, Mapping):
        raise ReportBackPolicyError("authenticated principal is not registered")
    audience = lane.get("audience")
    boards = lane.get("boards")
    if (
        not isinstance(audience, str) or not audience.strip()
        or not isinstance(boards, list) or not boards
        or any(not isinstance(item, str) or not item.strip() for item in boards)
    ):
        raise ReportBackPolicyError("authenticated principal registry entry is malformed")
    audience = audience.strip().lower()
    board_value = str(board or "").strip()
    if board_value not in {item.strip() for item in boards}:
        raise ReportBackPolicyError("authenticated principal is not authorized for report board")
    claimed = {
        str(value).strip().lower()
        for value in (metadata.get("requesting_user"), metadata.get("audience"))
        if isinstance(value, str) and value.strip()
    }
    session = str(metadata.get("source_session_key") or "").strip()
    if session.startswith("agent-os:cockpit:"):
        claimed.add(session.removeprefix("agent-os:cockpit:").lower())
    cockpit_board = str(metadata.get("cockpit_board") or "").strip()
    if claimed and claimed != {audience}:
        raise ReportBackPolicyError("task audience disagrees with authenticated principal")
    if cockpit_board and cockpit_board != board_value:
        raise ReportBackPolicyError("task cockpit board disagrees with authenticated principal")
    return audience, board_value


def _department_config(registry: Any, department_id: str) -> dict[str, Any]:
    if registry is None:
        return {}
    if not isinstance(registry, Mapping):
        raise ReportBackPolicyError("department registry is malformed")
    departments = registry.get("departments")
    if not isinstance(departments, Mapping):
        raise ReportBackPolicyError("department registry is malformed")
    value = departments.get(department_id)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReportBackPolicyError("department registry entry is malformed")
    return dict(value)


def _event_summary(task: Any, event: Any) -> str:
    payload = _as_dict(getattr(event, "payload", None))
    for key in ("summary", "reason", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(getattr(task, "result", None) or getattr(task, "title", None) or "Task updated")


def resolve_report_back_decision(
    *, task: Any, event: Any, board: Optional[str] = None,
    authenticated_principal: Optional[str] = None,
    principal_registry: Any = None,
    department_registry: Any = None,
    registry: Any = None,
    dry_run: bool = True,
) -> Optional[ReportBackDecision]:
    if task is None or event is None or not is_reportable_event(event):
        return None
    metadata = extract_routing_metadata(task)
    audience, cockpit_board = _principal_lane(
        authenticated_principal=authenticated_principal,
        principal_registry=principal_registry,
        metadata=metadata,
        board=board,
        task=task,
    )
    department_id = str(metadata.get("department_id") or "").strip().lower()
    departments = department_registry if department_registry is not None else registry
    department = _department_config(departments, department_id)
    privacy_tier = _most_restrictive(
        department.get("privacy_tier"), getattr(task, "security_realm", None),
        metadata.get("privacy_tier"),
    )
    destination = _as_dict(department.get("department_destination"))
    telegram_destination = _as_dict(destination.get("telegram_department_topic"))
    policy = _as_dict(department.get("report_back_policy"))
    policy_mode = str(policy.get("mode") or "desk_only")
    summary, redactions = redact_report_text(_event_summary(task, event), privacy_tier=privacy_tier)
    allowed = False
    reason = "Desk/audit-only report-back"
    if privacy_tier in _SENSITIVE or "secret" in redactions:
        reason = "sensitive report-back remains Desk-only"
    elif (
        policy_mode == "telegram_department_topic"
        and telegram_destination.get("enabled") is True
        and telegram_destination.get("chat_id") not in (None, "")
        and not dry_run
    ):
        allowed = True
        reason = "policy-approved redacted delivery"
    event_kind = _event_kind(event)
    task_id = str(getattr(task, "id", ""))
    status_version = str(getattr(event, "id", None) or getattr(event, "created_at", None) or "0")
    material = "|".join((task_id, audience, str(board or ""), department_id, event_kind, status_version))
    key = hashlib.sha256(material.encode()).hexdigest()[:32]
    return ReportBackDecision(
        task_id=task_id,
        event_kind=event_kind,
        department_id=department_id,
        department_owner=str(department.get("department_owner") or "Margot"),
        runtime_assignee=str(metadata.get("runtime_assignee") or getattr(task, "assignee", "") or ""),
        audience=audience,
        cockpit_board=cockpit_board,
        source_session_key=str(metadata.get("source_session_key") or ""),
        source_surface=str(metadata.get("source_surface") or getattr(task, "tenant", "") or "kanban"),
        return_surface=str(metadata.get("return_surface") or "kanban"),
        privacy_tier=privacy_tier,
        policy_mode=policy_mode,
        desk_artifact_id=f"kanban://{board or 'default'}/tasks/{task_id}",
        idempotency_key=key,
        telegram_allowed=allowed,
        telegram_reason=reason,
        dry_run=dry_run,
        redacted_summary=summary,
        telegram_destination=telegram_destination,
    )


def _ensure_claim_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS report_back_claims ("
        "task_id TEXT NOT NULL, target TEXT NOT NULL, semantic_key TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('claimed','recorded')), created_at INTEGER NOT NULL, "
        "PRIMARY KEY(task_id, target, semantic_key))"
    )


def _claim_and_record(
    conn: sqlite3.Connection, decision: ReportBackDecision, target: str,
    kind: str, payload: dict[str, Any], run_id: Optional[int],
) -> bool:
    now = int(time.time())
    with conn:
        _ensure_claim_table(conn)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO report_back_claims "
            "(task_id, target, semantic_key, state, created_at) VALUES (?, ?, ?, 'claimed', ?)",
            (decision.task_id, target, decision.idempotency_key, now),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (decision.task_id, run_id, kind, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.execute(
            "UPDATE report_back_claims SET state = 'recorded' "
            "WHERE task_id = ? AND target = ? AND semantic_key = ? AND state = 'claimed'",
            (decision.task_id, target, decision.idempotency_key),
        )
    return True


def _write_desk_record(decision: ReportBackDecision, board: Optional[str]) -> Optional[str]:
    root_value = os.environ.get("HERMES_DESK_DIR") or os.environ.get("AGENT_OS_DESK_DIR")
    if not root_value:
        return None
    root = Path(root_value).expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / f"kanban-{board or 'default'}-{decision.task_id}-{decision.idempotency_key}.json"
    payload = {
        "schemaVersion": 1,
        "package": {
            "sourceRequestId": decision.task_id,
            "audience": decision.audience,
            "cockpitBoard": decision.cockpit_board,
            "privacyTier": decision.privacy_tier,
            "summary": decision.redacted_summary,
            "sourceSurface": decision.source_surface,
            "returnSurface": decision.return_surface,
        },
    }
    try:
        if json.loads(path.read_text(encoding="utf-8")) == payload:
            return str(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # A crash may leave an old partial/corrupt artifact.  It is not a
        # canonical Desk record and must never suppress rematerialization.
        pass

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(path)


def record_report_back_dry_run(
    conn: sqlite3.Connection, *, task: Any, event: Any, board: Optional[str] = None,
    authenticated_principal: Optional[str] = None,
    principal_registry: Any = None,
    department_registry: Any = None,
    registry: Any = None,
    dry_run: bool = True,
) -> Optional[ReportBackDecision]:
    """Durably record one Desk intent then one secondary-delivery skip audit."""
    decision = resolve_report_back_decision(
        task=task, event=event, board=board,
        authenticated_principal=authenticated_principal,
        principal_registry=principal_registry,
        department_registry=department_registry,
        registry=registry,
        dry_run=dry_run,
    )
    if decision is None:
        return None
    run_id = getattr(event, "run_id", None)
    desk_payload = {
        "target": "desk", "idempotency_key": decision.idempotency_key,
        "source_event_id": getattr(event, "id", None),
        "audience": decision.audience, "privacy_tier": decision.privacy_tier,
        "message_preview": decision.redacted_summary,
        "canonical_before_telegram": True, "dry_run": dry_run,
    }
    if not dry_run:
        # Materialize before committing either the Desk audit claim or any
        # secondary-delivery claim.  A crash before this returns leaves no DB
        # suppression; a crash after the atomic replace is safe because the
        # next attempt validates and reuses the canonical artifact.
        desk_path = _write_desk_record(decision, board)
        if not desk_path:
            raise ReportBackPolicyError(
                "live report-back requires a configured canonical Desk directory"
            )
        desk_payload["desk_path"] = desk_path
    _claim_and_record(
        conn, decision, "desk", "report_back_desk_updated", desk_payload, run_id
    )
    _claim_and_record(
        conn,
        decision,
        "telegram_department_topic",
        (
            "report_back_telegram_claimed"
            if decision.telegram_allowed
            else "report_back_telegram_skipped"
        ),
        {
            "target": "telegram_department_topic",
            "idempotency_key": decision.idempotency_key,
            "source_event_id": getattr(event, "id", None),
            "audience": decision.audience,
            "privacy_tier": decision.privacy_tier,
            "telegram_destination": decision.telegram_destination,
            "message_preview": decision.redacted_summary,
            "live_send_allowed": decision.telegram_allowed,
            "reason": decision.telegram_reason,
            "dry_run": dry_run,
        },
        run_id,
    )
    return decision

"""Desk-first Kanban report-back routing helpers.

This module deliberately stops at the dry-run/audit boundary. It writes a
canonical Desk/Mission-Control-style audit event first, then records what would
happen for a Telegram department topic. It never calls a platform adapter and
therefore cannot send externally without a future, explicit live-send gate.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_ROUTING_METADATA_RE = re.compile(
    r"## Routing metadata\s*```json\s*(\{.*?\})\s*```",
    re.DOTALL,
)

_REPORTABLE_EVENT_KINDS = {"completed", "blocked", "needs_approval", "status"}
_NOISE_EVENT_KINDS = {
    "claimed",
    "heartbeat",
    "retry",
    "internal_comment",
    "child_created",
    "crashed",
    "timed_out",
    "gave_up",
    "unblocked",
    "archived",
}
_SENSITIVE_PRIVACY_TIERS = {
    "finance",
    "legal",
    "founder",
    "founders",
    "personnel",
    "hiring",
    "people",
    "credentials",
    "credential",
    "creator_only",
    "creator-only",
    "personal",
    "family",
}
_SENSITIVE_DEPARTMENT_HINTS = {
    "accountant_finance",
    "finance",
    "legal_compliance",
    "legal",
    "hiring_people",
    "people",
    "personal_family",
    "personal",
    "family",
    "claude_secondary_review",
    "creator_only",
}
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|ghp|gho|xox[baprs]|ya29)_[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\b(api[_ -]?key|token|password|secret|session[_ -]?cookie)\s*[:=]\s*\S+"),
)
_SENSITIVE_TELEGRAM_VISIBILITY_APPROVALS = {
    "allow",
    "allowed",
    "approved",
    "telegram",
    "telegram_allowed",
    "department_topic",
    "department_topic_allowed",
    "live_telegram",
}
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_DEFAULT_MARGOT_TOPICS_PATH = "/Users/Chris/.openclaw/workspace/Agent-Shared/routing/margot-departments-topics.json"
_TOPIC_REGISTRY_CACHE: tuple[str, float, dict[str, Any]] | None = None
_SAFE_DESK_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_SAFE_FILE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_MAX_DESK_COPY_BYTES = 25 * 1024 * 1024
_CALLBACK_RETURN_SURFACE = "agent-os:cockpit:voice-callback"
_CALLBACK_ANNOUNCEMENT_MAX_CHARS = 240
_PACKAGE_PRIORITIES = {"low", "normal", "high", "urgent"}
_PATH_TOKEN_RE = re.compile(r"(?:~|/)[^\s,;:)]+")
_MARKDOWN_TOKEN_RE = re.compile(r"[`*_#>\[\]()]")


@dataclass(frozen=True)
class ReportBackDecision:
    """Resolved dry-run decision for one task event."""

    task_id: str
    event_kind: str
    department_id: str
    department_owner: str
    runtime_assignee: str
    source_surface: str
    return_surface: str
    privacy_tier: str
    approval_gate: Optional[str]
    policy_mode: str
    desk_artifact_id: str
    idempotency_key: str
    telegram_allowed: bool
    telegram_reason: str
    dry_run: bool
    redacted_summary: str
    telegram_destination: dict[str, Any]


def extract_routing_metadata(task: Any) -> dict[str, Any]:
    """Return JSON routing metadata embedded in a Kanban task body, if present."""
    body = getattr(task, "body", None) if task is not None else None
    if not isinstance(body, str) or "## Routing metadata" not in body:
        return {}
    match = _ROUTING_METADATA_RE.search(body)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(*values: Any, default: str = "") -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _department_from_assignee(assignee: str) -> str:
    key = (assignee or "").strip().lower().replace("_", "-")
    if key in {"monitor"}:
        return "watchdog_health"
    if key in {"scarlett", "builder", "codex-lane", "qa"}:
        return "engineering_systems"
    if key in {"claire", "opslane"}:
        return "ops_control"
    if key in {"riley", "research"}:
        return "research"
    if key in {"lauren", "assistantops"}:
        return "assistant_ops"
    if key in {"maya"}:
        return "accountant_finance"
    if key in {"steven", "signer"}:
        return "legal_compliance"
    if key in {"harper"}:
        return "hiring_people"
    if key in {"sienna"}:
        return "personal_family"
    if key in {"geneva", "dakota"}:
        return "content_creative"
    if key in {"claude-review"}:
        return "claude_secondary_review"
    if key in {"default", "margot"}:
        return "chief_of_staff"
    return ""


def _registry_department(registry: Optional[dict[str, Any]], department_id: str) -> dict[str, Any]:
    if not isinstance(registry, dict):
        return {}
    departments = registry.get("departments")
    if isinstance(departments, dict):
        value = departments.get(department_id)
        return value if isinstance(value, dict) else {}
    value = registry.get(department_id)
    return value if isinstance(value, dict) else {}


def _topic_privacy_tier(topic: dict[str, Any]) -> str:
    dept_id = str(topic.get("department_id") or "").strip()
    privacy = str(topic.get("privacy") or "").strip().lower()
    if dept_id == "accountant_finance":
        return "finance"
    if dept_id == "legal_compliance":
        return "legal"
    if dept_id == "hiring_people":
        return "personnel"
    if privacy == "sensitive":
        return "restricted"
    return privacy or "normal"


def _normalize_report_back_policy(policy: Any) -> dict[str, Any]:
    """Normalize registry/task report-back policy aliases into one shape."""
    normalized = dict(policy) if isinstance(policy, dict) else {}
    if "include" not in normalized and isinstance(normalized.get("telegram_mirror_events"), list):
        normalized["include"] = normalized.get("telegram_mirror_events")
    suppress = normalized.get("suppress")
    if isinstance(suppress, list):
        exclude = list(normalized.get("exclude") or [])
        for item in suppress:
            if item == "worker_churn":
                for churn_kind in _NOISE_EVENT_KINDS:
                    if churn_kind not in exclude:
                        exclude.append(churn_kind)
            elif item not in exclude:
                exclude.append(item)
        normalized["exclude"] = exclude
    normalized.setdefault("mode", "milestone_only")
    return normalized


def _explicit_sensitive_telegram_allow(metadata: dict[str, Any], payload: dict[str, Any]) -> bool:
    """True only when task/event metadata explicitly approves sensitive live post."""
    gates = [
        _as_dict(metadata.get("privacy_gate")),
        _as_dict(payload.get("privacy_gate")),
        _as_dict(metadata.get("telegram_report_back")),
        _as_dict(payload.get("telegram_report_back")),
    ]
    for gate in gates:
        if not gate:
            continue
        auto_allowed = gate.get("auto_post_allowed") is True or gate.get("telegram_allowed") is True
        visibility_values = [
            gate.get("telegram_visibility"),
            gate.get("visibility"),
            gate.get("approval"),
            gate.get("approval_status"),
            gate.get("allowlist"),
        ]
        visibility_tokens: set[str] = set()
        for value in visibility_values:
            if isinstance(value, str) and value.strip():
                visibility_tokens.add(value.strip().lower())
            elif isinstance(value, list):
                visibility_tokens.update(str(item).strip().lower() for item in value if str(item).strip())
        approved_by = _first_str(gate.get("approved_by"), gate.get("visibility_approved_by"))
        if auto_allowed and (visibility_tokens & _SENSITIVE_TELEGRAM_VISIBILITY_APPROVALS or approved_by):
            return True
    return bool(
        metadata.get("telegram_report_back_approved") is True
        or payload.get("telegram_report_back_approved") is True
    )


def _load_margot_topic_registry() -> dict[str, Any]:
    """Load Chris's Margot OS department-topic map.

    Desk/Kanban stays canonical, but this shared JSON is the operational bridge
    used by the gateway to mirror meaningful department report-backs into the
    right Telegram forum topic. Missing/unreadable mapping is non-fatal.
    """
    global _TOPIC_REGISTRY_CACHE
    path = Path(os.environ.get("MARGOT_DEPARTMENT_TOPICS_PATH") or _DEFAULT_MARGOT_TOPICS_PATH).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache_key = str(path)
    if _TOPIC_REGISTRY_CACHE and _TOPIC_REGISTRY_CACHE[0] == cache_key and _TOPIC_REGISTRY_CACHE[1] == stat.st_mtime:
        return _TOPIC_REGISTRY_CACHE[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    chat_id = str(raw.get("chat_id") or "").strip()
    departments: dict[str, Any] = {}
    for topic in raw.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        dept_id = str(topic.get("department_id") or "").strip()
        topic_id = topic.get("topic_id")
        if not dept_id or topic_id is None or not chat_id:
            continue
        privacy_tier = _topic_privacy_tier(topic)
        dept_config = {
            "department_owner": str(topic.get("voice_label") or topic.get("department") or "Margot"),
            "runtime_assignee": str(topic.get("runtime_assignee") or "default"),
            "privacy_tier": privacy_tier,
            "report_back_policy": _normalize_report_back_policy(
                raw.get("report_back_policy")
                or {"mode": "milestone_only", "include": ["completed", "blocked", "needs_approval"]}
            ),
            "department_destination": {
                "telegram_department_topic": {
                    "enabled": True,
                    "dry_run_only": False,
                    "chat_id": chat_id,
                    "topic_id": int(topic_id),
                    "topic_label": str(topic.get("topic_name") or topic.get("department") or dept_id),
                    "icon_emoji": topic.get("icon_emoji") or topic.get("display_emoji"),
                }
            },
            # Topic existence only identifies the destination. Sensitive lanes
            # still require explicit per-task/event visibility approval before
            # any live Telegram mirror is allowed.
            "privacy_gate": {
                "auto_post_allowed": privacy_tier not in _SENSITIVE_PRIVACY_TIERS,
                "sensitive_summary_only": privacy_tier in _SENSITIVE_PRIVACY_TIERS,
            },
        }
        departments[dept_id] = dept_config
        aliases = topic.get("aliases") or []
        if isinstance(aliases, list):
            for alias in aliases:
                alias_id = str(alias or "").strip()
                if alias_id:
                    departments[alias_id] = dept_config
    registry = {"departments": departments}
    _TOPIC_REGISTRY_CACHE = (cache_key, stat.st_mtime, registry)
    return registry


def _department_config(registry: Optional[dict[str, Any]], department_id: str) -> dict[str, Any]:
    explicit = _registry_department(registry, department_id)
    if explicit:
        return explicit
    return _registry_department(_load_margot_topic_registry(), department_id)


def _event_report_kind(event: Any) -> str:
    kind = getattr(event, "kind", "") or ""
    payload = _as_dict(getattr(event, "payload", None))
    if kind == "status" and str(payload.get("status") or "").strip() == "needs_approval":
        return "needs_approval"
    return kind


def is_reportable_event(event: Any) -> bool:
    """True only for human-meaningful report-back events, not worker churn."""
    kind = _event_report_kind(event)
    if kind in _NOISE_EVENT_KINDS:
        return False
    return kind in {"completed", "blocked", "needs_approval"}


def redact_report_text(text: str, *, privacy_tier: str = "normal") -> tuple[str, list[str]]:
    """Apply conservative report-back redaction hooks.

    Returns the redacted text plus a list of redaction labels for audit.
    """
    redactions: list[str] = []
    safe = text or ""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(safe):
            safe = pattern.sub("[REDACTED_SECRET]", safe)
            if "secret" not in redactions:
                redactions.append("secret")
    if _EMAIL_RE.search(safe) and privacy_tier in _SENSITIVE_PRIVACY_TIERS:
        safe = _EMAIL_RE.sub("[REDACTED_EMAIL]", safe)
        redactions.append("email")
    if privacy_tier in {"finance", "legal", "personnel", "hiring", "people", "personal", "family"}:
        # Keep the human-facing report terse for sensitive lanes. Detailed packets
        # belong in restricted Desk artifacts, not department topics.
        lines = [line.strip() for line in safe.splitlines() if line.strip()]
        safe = lines[0][:240] if lines else "Restricted packet updated in Desk."
        if "sensitive_summary" not in redactions:
            redactions.append("sensitive_summary")
    return safe[:700], redactions


def _event_summary(task: Any, event: Any) -> str:
    payload = _as_dict(getattr(event, "payload", None))
    for key in ("summary", "reason", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = getattr(task, "result", None)
    if isinstance(result, str) and result.strip():
        return result.strip()
    title = getattr(task, "title", "") or getattr(task, "id", "task")
    kind = _event_report_kind(event)
    return f"{kind}: {title}"


def _desk_artifact_id(task: Any, board: Optional[str], metadata: dict[str, Any], dept: dict[str, Any]) -> str:
    explicit = metadata.get("desk_artifact")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if isinstance(explicit, dict):
        for key in ("artifact_id_or_url", "artifact_id", "url", "id"):
            value = explicit.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    dept_artifact = dept.get("desk_artifact")
    if isinstance(dept_artifact, dict):
        value = dept_artifact.get("artifact_id_or_url") or dept_artifact.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    board_part = board or "default"
    return f"kanban://{board_part}/tasks/{getattr(task, 'id', '')}"


def _desk_root() -> Path:
    configured = os.environ.get("AGENT_OS_DESK_DIR") or os.environ.get("HERMES_DESK_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes" / "desk"


def _safe_desk_id(value: str, fallback: str) -> str:
    cleaned = _SAFE_DESK_ID_RE.sub("-", (value or "").strip()).strip(".-_:")
    return (cleaned or fallback)[:96]


def _safe_file_name(value: str, fallback: str) -> str:
    cleaned = _SAFE_FILE_NAME_RE.sub("-", Path(value or fallback).name).strip(".-_")
    return (cleaned or fallback)[:120]


def _artifact_kind(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in {".md", ".txt", ".rtf", ".doc", ".docx"}:
        return "doc"
    if ext == ".pdf":
        return "pdf"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if ext in {".csv", ".xls", ".xlsx", ".numbers"}:
        return "spreadsheet"
    if ext in {".zip", ".tar", ".gz", ".tgz"}:
        return "archive"
    return "other"


def _clean_spoken_fragment(value: Any, *, fallback: str = "work") -> str:
    """Sanitize text that may be read aloud by the cockpit.

    The app reads callback.announcement verbatim, so strip markdown, local
    paths, media directives, callback ids, and line breaks before we write the
    Desk record. This is intentionally conservative; better a plain sentence
    than an orb reading `/Users/...` out loud.
    """
    text = str(value or "").strip() or fallback
    text = re.sub(r"MEDIA:\S+", "", text)
    text = _PATH_TOKEN_RE.sub("", text)
    text = _EMAIL_RE.sub("someone", text)
    text = re.sub(r"\b(?:t|run|job|pkg|art|ver)_[A-Za-z0-9_-]{6,}\b", "", text)
    text = re.sub(r"\b[a-f0-9]{12,}\b", "", text, flags=re.I)
    text = _MARKDOWN_TOKEN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—:.;,")
    return text or fallback


def _truncate_spoken_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= _CALLBACK_ANNOUNCEMENT_MAX_CHARS:
        return text
    # Leave enough room for a clean offer ending.
    offer = " Want to hear it?"
    budget = _CALLBACK_ANNOUNCEMENT_MAX_CHARS - len(offer)
    clipped = text[:budget].rsplit(" ", 1)[0].strip(" -–—:.;,")
    return f"{clipped}.{offer}"[:_CALLBACK_ANNOUNCEMENT_MAX_CHARS]


def _desk_callback_priority(metadata: dict[str, Any], payload: dict[str, Any]) -> str:
    raw = _first_str(
        payload.get("priority"),
        payload.get("desk_priority"),
        metadata.get("priority"),
        metadata.get("desk_priority"),
        default="normal",
    ).lower()
    return raw if raw in _PACKAGE_PRIORITIES else "normal"


def _desk_callback_announcement(*, decision: ReportBackDecision, task: Any, event: Any, metadata: dict[str, Any]) -> str:
    payload = _as_dict(getattr(event, "payload", None))
    callback = _as_dict(payload.get("callback")) or _as_dict(metadata.get("callback"))
    authored = _first_str(
        callback.get("announcement"),
        payload.get("announcement"),
        payload.get("callback_announcement"),
        payload.get("voice_announcement"),
        metadata.get("announcement"),
        metadata.get("callback_announcement"),
        metadata.get("voice_announcement"),
    )
    if authored:
        announcement = _clean_spoken_fragment(authored)
        if not re.search(r"\?\s*$", announcement):
            announcement = f"{announcement.rstrip('.!')} — want to hear it?"
        return _truncate_spoken_sentence(announcement)

    worker = _clean_spoken_fragment(decision.department_owner or decision.runtime_assignee or "Margot", fallback="Margot")
    raw_title = getattr(task, "title", "") or decision.redacted_summary or "the update"
    subject = _clean_spoken_fragment(raw_title, fallback="the update")
    if decision.event_kind == "blocked":
        announcement = f"{worker} needs a decision on {subject}. Want the blocker?"
    elif decision.event_kind == "needs_approval":
        announcement = f"{worker} needs approval on {subject}. Want to review it?"
    else:
        announcement = f"{worker} has {subject} ready for review. Want to hear it?"
    return _truncate_spoken_sentence(announcement)


def _completion_artifact_paths(event: Any) -> list[str]:
    payload = _as_dict(getattr(event, "payload", None))
    raw = payload.get("artifacts")
    if not isinstance(raw, list):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        value = item.strip()
        if value in seen:
            continue
        seen.add(value)
        paths.append(value)
    return paths


def write_desk_intake_record(
    *,
    decision: ReportBackDecision,
    task: Any,
    event: Any,
    board: Optional[str] = None,
) -> Optional[str]:
    """Materialize a Kanban terminal event into the local Desk intake store.

    The app surface owns Desk rendering/review. The gateway owns the agent-side
    handoff contract: terminal Kanban events and artifact paths become Desk JSON
    records before any Telegram mirror is attempted.
    """
    root = _desk_root()
    package_id = _safe_desk_id(
        f"kanban-{board or 'default'}-{decision.task_id}-{decision.event_kind}",
        "kanban-report-back",
    )
    record_path = root / f"{package_id}-{decision.idempotency_key}.json"
    if record_path.exists():
        return str(record_path)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    title = str(getattr(task, "title", "") or decision.task_id).strip()[:180]
    source_surface = decision.source_surface or "kanban"
    desk_source_surface = source_surface if source_surface in {
        "telegram_topic", "telegram_dm", "mission_control", "wearable", "phone", "cron", "manual", "unknown", "other"
    } else "other"
    privacy_tier = decision.privacy_tier or "team"
    if privacy_tier == "normal":
        privacy_tier = "team"
    if privacy_tier in {"personnel", "hiring", "people", "credentials", "credential", "restricted", "sensitive", "confidential", "private"}:
        privacy_tier = "team"
    if privacy_tier == "creator-only":
        privacy_tier = "creator_only"

    artifact_paths = _completion_artifact_paths(event)
    callback_priority = _desk_callback_priority(extract_routing_metadata(task), _as_dict(getattr(event, "payload", None)))
    callback_id = _safe_desk_id(
        f"{decision.task_id}-{decision.event_kind}-{decision.idempotency_key}",
        f"callback-{decision.idempotency_key}",
    )
    callback_announcement = _desk_callback_announcement(
        decision=decision,
        task=task,
        event=event,
        metadata=extract_routing_metadata(task),
    )
    artifacts: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    files_dir = root / "files" / package_id

    root.mkdir(parents=True, exist_ok=True)
    for idx, artifact_path in enumerate(artifact_paths, start=1):
        art_id = _safe_desk_id(f"{package_id}-artifact-{idx}", f"artifact-{idx}")
        ver_id = _safe_desk_id(f"{art_id}-v1", f"artifact-{idx}-v1")
        name = _safe_file_name(artifact_path, f"artifact-{idx}")
        preview = artifact_path
        source_links = [artifact_path]
        version_extra: dict[str, Any] = {}
        try:
            src = Path(artifact_path).expanduser()
            stat = src.stat()
            if src.is_file() and stat.st_size <= _MAX_DESK_COPY_BYTES:
                files_dir.mkdir(parents=True, exist_ok=True)
                dest = files_dir / name
                if not dest.exists():
                    shutil.copyfile(src, dest)
                rel_path = dest.relative_to(root).as_posix()
                mime_type, _ = mimetypes.guess_type(str(dest))
                version_extra.update({"filePath": rel_path, "byteLength": int(stat.st_size)})
                if mime_type:
                    version_extra["mimeType"] = mime_type
                preview = f"Ready for review in Desk: {name}"
        except OSError:
            preview = f"Artifact path recorded for follow-up: {artifact_path}"

        artifact_ids.append(art_id)
        artifacts.append({
            "id": art_id,
            "packageId": package_id,
            "title": name,
            "kind": _artifact_kind(artifact_path),
            "status": "ready_for_review" if decision.event_kind == "completed" else "draft",
            "currentVersionId": ver_id,
            "versionIds": [ver_id],
            "createdByAgentId": decision.department_owner or decision.runtime_assignee or "Margot",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "previewText": preview,
            "sourceLinks": source_links,
            "exportFormats": [Path(artifact_path).suffix.lower().lstrip(".") or "file"],
            "tags": ["kanban", decision.department_id, decision.event_kind],
        })
        versions.append({
            "id": ver_id,
            "artifactId": art_id,
            "label": "v1",
            "createdAt": now_iso,
            "createdByAgentId": decision.department_owner or decision.runtime_assignee or "Margot",
            "changelog": "Returned from Kanban completion for Desk review.",
            **version_extra,
        })

    if not artifacts:
        art_id = _safe_desk_id(f"{package_id}-status", "status")
        ver_id = _safe_desk_id(f"{art_id}-v1", "status-v1")
        artifact_ids.append(art_id)
        artifacts.append({
            "id": art_id,
            "packageId": package_id,
            "title": "Status update",
            "kind": "brief",
            "status": "ready_for_review" if decision.event_kind == "completed" else "draft",
            "currentVersionId": ver_id,
            "versionIds": [ver_id],
            "createdByAgentId": decision.department_owner or decision.runtime_assignee or "Margot",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "previewText": decision.redacted_summary or title,
            "sourceLinks": [decision.desk_artifact_id],
            "exportFormats": ["status"],
            "tags": ["kanban", decision.department_id, decision.event_kind],
        })
        versions.append({
            "id": ver_id,
            "artifactId": art_id,
            "label": "v1",
            "createdAt": now_iso,
            "createdByAgentId": decision.department_owner or decision.runtime_assignee or "Margot",
            "changelog": "Status surfaced to Desk.",
        })

    record = {
        "schemaVersion": 1,
        "package": {
            "id": package_id,
            "title": title,
            "departmentId": decision.department_id or "chief_of_staff",
            "producingAgentId": decision.department_owner or decision.runtime_assignee or "Margot",
            "sourceRequestId": decision.task_id,
            "sourceSurface": desk_source_surface,
            "sourceSurfaceId": source_surface,
            "returnSurface": _CALLBACK_RETURN_SURFACE,
            "project": f"Kanban/{board or 'default'}",
            "status": "returned" if decision.event_kind == "completed" else "needs_review",
            "priority": callback_priority,
            "summary": decision.redacted_summary or title,
            "createdAt": now_iso,
            "returnedAt": now_iso,
            "updatedAt": now_iso,
            "needsChris": True,
            "privacyTier": privacy_tier,
            "followUpStatus": "needs_chris_review",
            "callback": {
                "returnSurface": _CALLBACK_RETURN_SURFACE,
                "callbackId": callback_id,
                "voicePing": True,
                "announcement": callback_announcement,
            },
            "tags": ["kanban", decision.department_id, decision.event_kind],
            "artifactIds": artifact_ids,
            "reviewNoteIds": [],
            "source_surface": decision.source_surface,
            "return_surface": _CALLBACK_RETURN_SURFACE,
        },
        "artifacts": artifacts,
        "versions": versions,
        "reviewNotes": [],
    }
    tmp_path = record_path.with_suffix(record_path.suffix + f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(record_path)
    return str(record_path)


def _policy_allows_event(policy: dict[str, Any], event_kind: str) -> tuple[bool, str]:
    mode = str(policy.get("mode") or "milestone_only")
    include = policy.get("include")
    exclude = set(str(x) for x in (policy.get("exclude") or []))
    if event_kind in exclude:
        return False, f"event excluded by report_back_policy: {event_kind}"
    if mode in {"desk_only", "source_only", "digest"}:
        return False, f"policy mode is {mode}"
    if mode == "approval_only" and event_kind != "needs_approval":
        return False, "policy mode is approval_only"
    if isinstance(include, list) and include and event_kind not in {str(x) for x in include}:
        return False, f"event not included by report_back_policy: {event_kind}"
    return True, "allowed_by_policy"


def resolve_report_back_decision(
    *,
    task: Any,
    event: Any,
    board: Optional[str] = None,
    registry: Optional[dict[str, Any]] = None,
    dry_run: bool = True,
) -> Optional[ReportBackDecision]:
    """Resolve Desk/source/department-topic report-back fields separately."""
    if task is None or event is None or not is_reportable_event(event):
        return None

    metadata = extract_routing_metadata(task)
    payload = _as_dict(getattr(event, "payload", None))
    runtime_assignee = _first_str(metadata.get("runtime_assignee"), getattr(task, "assignee", None))
    department_id = _first_str(
        payload.get("department_id"),
        metadata.get("department_id"),
        metadata.get("department"),
        default=_department_from_assignee(runtime_assignee),
    ).lower()
    dept = _department_config(registry, department_id)
    destination = _as_dict(metadata.get("department_destination")) or _as_dict(dept.get("department_destination"))
    telegram_destination = _as_dict(destination.get("telegram_department_topic"))

    policy = _normalize_report_back_policy(
        _as_dict(metadata.get("report_back_policy")) or _as_dict(dept.get("report_back_policy"))
    )
    policy_mode = str(policy.get("mode") or "milestone_only")
    event_kind = _event_report_kind(event)
    privacy_tier = _first_str(payload.get("privacy_tier"), metadata.get("privacy_tier"), dept.get("privacy_tier"), default="normal").lower()
    department_owner = _first_str(
        metadata.get("department_owner"),
        metadata.get("owner_name"),
        dept.get("department_owner"),
        default=runtime_assignee.replace("-", " ").replace("_", " ").title() if runtime_assignee else "Margot",
    )
    source_surface = _first_str(
        metadata.get("source_surface"),
        getattr(task, "tenant", None),
        default="kanban",
    )
    return_surface = _first_str(metadata.get("return_surface"), source_surface, default="kanban")
    approval_gate = payload.get("approval_gate") or metadata.get("approval_gate") or dept.get("approval_gate")
    approval_gate = str(approval_gate) if approval_gate else None
    desk_artifact_id = _desk_artifact_id(task, board, metadata, dept)
    redacted_summary, redactions = redact_report_text(_event_summary(task, event), privacy_tier=privacy_tier)

    policy_allowed, policy_reason = _policy_allows_event(policy, event_kind)
    privacy_gate = {**_as_dict(dept.get("privacy_gate")), **_as_dict(metadata.get("privacy_gate"))}
    auto_post_allowed = bool(privacy_gate.get("auto_post_allowed", True))
    explicit_sensitive_allow = _explicit_sensitive_telegram_allow(metadata, payload)
    if explicit_sensitive_allow:
        auto_post_allowed = True
    sensitive_lane = (
        privacy_tier in _SENSITIVE_PRIVACY_TIERS
        or department_id in _SENSITIVE_DEPARTMENT_HINTS
        or bool(redactions and "secret" in redactions)
    )
    if sensitive_lane and not explicit_sensitive_allow:
        telegram_allowed = False
        if "secret" in redactions:
            telegram_reason = "sensitive lane contains secret-like material; Desk-only/manual-approval until visibility is explicitly allowed"
        else:
            telegram_reason = "sensitive lane defaults Desk-only/manual-approval until Telegram visibility is explicitly allowed"
    elif not auto_post_allowed:
        telegram_allowed = False
        telegram_reason = "privacy gate does not allow Telegram auto-post"
    elif not policy_allowed:
        telegram_allowed = False
        telegram_reason = policy_reason
    elif not telegram_destination.get("enabled", False):
        telegram_allowed = False
        telegram_reason = "telegram department destination disabled"
    elif telegram_destination.get("dry_run_only", True):
        telegram_allowed = False
        telegram_reason = "telegram department destination is dry_run_only"
    elif dry_run:
        telegram_allowed = False
        telegram_reason = "gateway report-back router is in dry-run mode"
    else:
        telegram_allowed = True
        telegram_reason = "allowed"

    status_version = str(getattr(event, "id", "") or getattr(event, "created_at", "") or int(time.time()))
    key_material = "|".join([
        str(getattr(task, "id", "")),
        department_id,
        event_kind,
        desk_artifact_id,
        status_version,
    ])
    idempotency_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]

    return ReportBackDecision(
        task_id=str(getattr(task, "id", "")),
        event_kind=event_kind,
        department_id=department_id,
        department_owner=department_owner,
        runtime_assignee=runtime_assignee,
        source_surface=source_surface,
        return_surface=return_surface,
        privacy_tier=privacy_tier,
        approval_gate=approval_gate,
        policy_mode=policy_mode,
        desk_artifact_id=desk_artifact_id,
        idempotency_key=idempotency_key,
        telegram_allowed=telegram_allowed,
        telegram_reason=telegram_reason,
        dry_run=dry_run,
        redacted_summary=redacted_summary,
        telegram_destination=telegram_destination,
    )


def _has_audit(conn: sqlite3.Connection, task_id: str, target: str, idempotency_key: str) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN "
        "('report_back_desk_updated', 'report_back_telegram_preview', 'report_back_telegram_skipped')",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        if payload.get("target") == target and payload.get("idempotency_key") == idempotency_key:
            return True
    return False


def _append_audit_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any], run_id: Optional[int]) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, json.dumps(payload, ensure_ascii=False), now),
    )


def record_report_back_dry_run(
    conn: sqlite3.Connection,
    *,
    task: Any,
    event: Any,
    board: Optional[str] = None,
    registry: Optional[dict[str, Any]] = None,
    dry_run: bool = True,
) -> Optional[ReportBackDecision]:
    """Record Desk-first report-back audit events for a Kanban terminal event.

    The write order is intentional and tested: Desk audit first, Telegram topic
    preview/skip second. Existing matching idempotency keys are not duplicated.
    """
    decision = resolve_report_back_decision(
        task=task,
        event=event,
        board=board,
        registry=registry,
        dry_run=dry_run,
    )
    if decision is None:
        return None

    run_id = getattr(event, "run_id", None)
    desk_record_path = None
    if not dry_run:
        try:
            desk_record_path = write_desk_intake_record(
                decision=decision,
                task=task,
                event=event,
                board=board,
            )
        except Exception:
            # Desk intake must never prevent notifier cursor progress or Telegram
            # source callbacks. The audit row below still records the intended
            # Desk artifact id so an operator can repair/replay the intake.
            desk_record_path = None
    with conn:
        if not _has_audit(conn, decision.task_id, "desk", decision.idempotency_key):
            _append_audit_event(
                conn,
                decision.task_id,
                "report_back_desk_updated",
                {
                    "target": "desk",
                    "idempotency_key": decision.idempotency_key,
                    "source_event_id": getattr(event, "id", None),
                    "source_event_kind": decision.event_kind,
                    "desk_artifact": {
                        "artifact_id_or_url": decision.desk_artifact_id,
                        "status": decision.event_kind,
                        "department_id": decision.department_id,
                        "department_owner": decision.department_owner,
                        "runtime_assignee": decision.runtime_assignee,
                        "privacy_tier": decision.privacy_tier,
                        "approval_gate": decision.approval_gate,
                        "source_surface": decision.source_surface,
                        "return_surface": decision.return_surface,
                        "record_path": desk_record_path,
                        "updated_at": int(time.time()),
                    },
                    "canonical_before_telegram": True,
                    "dry_run": decision.dry_run,
                },
                run_id,
            )

        target = "telegram_department_topic"
        if not _has_audit(conn, decision.task_id, target, decision.idempotency_key):
            event_kind = "report_back_telegram_preview" if decision.telegram_allowed else "report_back_telegram_skipped"
            _append_audit_event(
                conn,
                decision.task_id,
                event_kind,
                {
                    "target": target,
                    "idempotency_key": decision.idempotency_key,
                    "source_event_id": getattr(event, "id", None),
                    "source_event_kind": decision.event_kind,
                    "department_id": decision.department_id,
                    "department_owner": decision.department_owner,
                    "runtime_assignee": decision.runtime_assignee,
                    "source_surface": decision.source_surface,
                    "return_surface": decision.return_surface,
                    "privacy_tier": decision.privacy_tier,
                    "policy_mode": decision.policy_mode,
                    "desk_artifact_id": decision.desk_artifact_id,
                    "telegram_destination": {
                        "chat_id": decision.telegram_destination.get("chat_id"),
                        "topic_id": decision.telegram_destination.get("topic_id"),
                        "topic_label": decision.telegram_destination.get("topic_label"),
                    },
                    "message_preview": decision.redacted_summary,
                    "live_send_allowed": decision.telegram_allowed,
                    "reason": decision.telegram_reason,
                    "dry_run": decision.dry_run,
                },
                run_id,
            )
    return decision

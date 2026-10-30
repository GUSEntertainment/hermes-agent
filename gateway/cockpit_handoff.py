"""Validated cross-user cockpit handoff envelopes.

A handoff transfers selected attention, never a private conversation history.
The recipient lane is resolved from reviewed user policy and all provenance is
bounded to identifiers plus a concise reason/suggested next ask.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from gateway.cockpit_user_routing import (
    CockpitRoutingError,
    resolve_cockpit_lane,
)


VALID_PACKAGE_CLASSES = frozenset({"deliverable", "decision", "status"})
_FORBIDDEN_TRANSCRIPT_KEYS = frozenset(
    {"raw_transcript", "transcript", "conversation", "conversation_history", "messages"}
)


class CockpitHandoffError(CockpitRoutingError):
    """Raised when a cross-user transfer lacks safe, consistent provenance."""


@dataclass(frozen=True)
class CockpitHandoff:
    from_user: str
    to_user: str
    source_board: str
    target_board: str
    audience: str
    package_class: str
    suggested_ask: str
    transferred_at: str
    source_package_id: Optional[str] = None
    source_artifact_id: Optional[str] = None
    source_task_id: Optional[str] = None
    transfer_reason: Optional[str] = None

    def as_metadata(self) -> dict[str, Any]:
        handoff = {
            "kind": "cross_user",
            "fromUser": self.from_user,
            "toUser": self.to_user,
            "sourceBoard": self.source_board,
            "targetBoard": self.target_board,
            "transferredAt": self.transferred_at,
            "suggestedAsk": self.suggested_ask,
        }
        optional = {
            "sourcePackageId": self.source_package_id,
            "sourceArtifactId": self.source_artifact_id,
            "sourceTaskId": self.source_task_id,
            "transferReason": self.transfer_reason,
        }
        handoff.update({key: value for key, value in optional.items() if value})
        return {
            "requesting_user": self.to_user,
            "audience": self.audience,
            "cockpit_board": self.target_board,
            "source_session_key": f"agent-os:cockpit:{self.to_user}",
            "source_surface": "agent-os:cockpit:handoff",
            "return_surface": "agent-os:cockpit:voice-callback",
            "class": self.package_class,
            "handoff": handoff,
        }


def _bounded_text(value: Any, field: str, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise CockpitHandoffError(f"{field} is required")
    if len(text) > max_chars:
        raise CockpitHandoffError(f"{field} is too long")
    return text


def build_cockpit_handoff(
    *,
    from_user: Any,
    to_user: Any,
    source_board: str,
    suggested_ask: str,
    package_class: str = "status",
    source_package_id: Optional[str] = None,
    source_artifact_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    transfer_reason: Optional[str] = None,
    transferred_at: Optional[str] = None,
) -> CockpitHandoff:
    try:
        source = resolve_cockpit_lane(from_user, board=source_board)
        target = resolve_cockpit_lane(to_user)
    except CockpitRoutingError as exc:
        raise CockpitHandoffError(str(exc)) from exc
    if source.user == target.user:
        raise CockpitHandoffError("cross-user handoff target must differ from source")
    if package_class not in VALID_PACKAGE_CLASSES:
        raise CockpitHandoffError("invalid handoff package class")
    if not any((source_package_id, source_artifact_id, source_task_id)):
        raise CockpitHandoffError("handoff requires a source package, artifact, or task id")
    ask = _bounded_text(suggested_ask, "suggestedAsk", max_chars=400)
    reason = None
    if transfer_reason:
        reason = _bounded_text(transfer_reason, "transferReason", max_chars=500)
    timestamp = transferred_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return CockpitHandoff(
        from_user=source.user,
        to_user=target.user,
        source_board=source.board,
        target_board=target.board,
        audience=target.audience,
        package_class=package_class,
        suggested_ask=ask,
        transferred_at=timestamp,
        source_package_id=source_package_id,
        source_artifact_id=source_artifact_id,
        source_task_id=source_task_id,
        transfer_reason=reason,
    )


def validate_cockpit_handoff(metadata: Mapping[str, Any]) -> CockpitHandoff:
    if not isinstance(metadata, Mapping):
        raise CockpitHandoffError("handoff metadata must be an object")
    forbidden = _FORBIDDEN_TRANSCRIPT_KEYS.intersection(metadata)
    if forbidden:
        raise CockpitHandoffError("raw conversation content is not allowed in a handoff")
    handoff = metadata.get("handoff")
    if not isinstance(handoff, Mapping):
        raise CockpitHandoffError("handoff object is required")
    forbidden = _FORBIDDEN_TRANSCRIPT_KEYS.intersection(handoff)
    if forbidden:
        raise CockpitHandoffError("raw conversation content is not allowed in a handoff")
    built = build_cockpit_handoff(
        from_user=handoff.get("fromUser"),
        to_user=handoff.get("toUser"),
        source_board=str(handoff.get("sourceBoard") or ""),
        suggested_ask=str(handoff.get("suggestedAsk") or ""),
        package_class=str(metadata.get("class") or "status"),
        source_package_id=handoff.get("sourcePackageId"),
        source_artifact_id=handoff.get("sourceArtifactId"),
        source_task_id=handoff.get("sourceTaskId"),
        transfer_reason=handoff.get("transferReason"),
        transferred_at=str(handoff.get("transferredAt") or "") or None,
    )
    expected = built.as_metadata()
    for key in ("requesting_user", "audience", "cockpit_board"):
        if metadata.get(key) != expected[key]:
            raise CockpitHandoffError(f"handoff {key} does not match the recipient lane")
    if handoff.get("targetBoard") != built.target_board:
        raise CockpitHandoffError("handoff target board does not match the recipient")
    return built


def build_handoff_package(
    *, handoff: CockpitHandoff, title: str, summary: str, artifacts: Optional[list[Any]] = None
) -> dict[str, Any]:
    """Build an additive Desk-shaped package for the recipient only."""
    safe_title = _bounded_text(title, "title", max_chars=180)
    safe_summary = _bounded_text(summary, "summary", max_chars=2000)
    payload = handoff.as_metadata()
    payload.update(
        {
            "title": f"From {handoff.from_user.title()}: {safe_title}",
            "summary": safe_summary,
            "needsChris": handoff.package_class == "decision",
            "followUpStatus": (
                "needs_chris_review" if handoff.package_class == "decision" else "none"
            ),
        }
    )
    if artifacts:
        payload["artifacts"] = list(artifacts)
    return payload

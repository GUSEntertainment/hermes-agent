"""Fail-closed typed ingress for messages authored by another bot."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping

_PREFIX = "HERMES_HANDOFF_V1:"
_ALLOWED_KEYS = {"kind", "version", "request_id", "source_agent", "target_profile", "message"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_MESSAGE_CHARS = 8_000


@dataclass(frozen=True)
class BotIngressDecision:
    allowed: bool
    silent: bool
    reason: str
    message: str
    handoff: dict[str, Any] | None = None


class BotHandoffReplayStore:
    """Durably claim request IDs before a handoff can create a session."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def claim(self, *, platform: str, sender_id: str, target_profile: str, request_id: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bot_handoff_claims ("
                "platform TEXT NOT NULL, sender_id TEXT NOT NULL, "
                "target_profile TEXT NOT NULL, request_id TEXT NOT NULL, "
                "claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY(target_profile, request_id))"
            )
            try:
                db.execute(
                    "INSERT INTO bot_handoff_claims(platform,sender_id,target_profile,request_id) VALUES(?,?,?,?)",
                    (platform, sender_id, target_profile, request_id),
                )
                db.commit()
                return True
            except sqlite3.IntegrityError:
                return False


def _invalid(reason: str = "bot_handoff_invalid") -> BotIngressDecision:
    return BotIngressDecision(False, True, reason, "", None)


def _handoff_policy(config: Mapping[str, Any] | None, platform: str) -> list[Mapping[str, Any]]:
    if not isinstance(config, Mapping):
        return []
    gateway = config.get("gateway")
    root = gateway if isinstance(gateway, Mapping) else config
    handoff = root.get("bot_handoff") if isinstance(root, Mapping) else None
    platform_cfg = handoff.get(platform) if isinstance(handoff, Mapping) else None
    senders = platform_cfg.get("allowed_senders") if isinstance(platform_cfg, Mapping) else None
    return [row for row in senders if isinstance(row, Mapping)] if isinstance(senders, list) else []


def classify_bot_ingress(
    *,
    source_is_bot: bool,
    text: str,
    source_user_id: str | None = None,
    source_profile: str | None = None,
    platform: str = "",
    config: Mapping[str, Any] | None = None,
    replay_store: BotHandoffReplayStore | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> BotIngressDecision:
    """Authorize a bounded handoff against exact transport identity and policy."""
    raw_text = str(text or "")
    if not source_is_bot:
        return BotIngressDecision(True, False, "human", raw_text, None)
    if not raw_text.startswith(_PREFIX):
        return _invalid("bot_handoff_required")
    encoded = raw_text[len(_PREFIX):].strip()
    if not encoded or len(encoded) > _MAX_MESSAGE_CHARS * 2:
        return _invalid()
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _invalid()
    if not isinstance(payload, dict) or set(payload) != _ALLOWED_KEYS:
        return _invalid()
    if payload.get("kind") != "handoff" or payload.get("version") != 1:
        return _invalid()

    request_id = str(payload.get("request_id") or "").strip()
    source_agent = str(payload.get("source_agent") or "").strip()
    target_profile = str(payload.get("target_profile") or "").strip()
    message = str(payload.get("message") or "").strip()
    sender_id = str(source_user_id or "").strip()
    actual_profile = str(source_profile or "default").strip()
    platform_name = str(platform or "").strip().lower()
    if not all((_IDENTIFIER.fullmatch(request_id), _IDENTIFIER.fullmatch(source_agent), _IDENTIFIER.fullmatch(target_profile))):
        return _invalid()
    if not message or len(message) > _MAX_MESSAGE_CHARS or not sender_id or target_profile != actual_profile:
        return _invalid()

    exact_match = any(
        str(row.get("sender_id") or "").strip() == sender_id
        and str(row.get("source_agent") or "").strip() == source_agent
        and str(row.get("target_profile") or "").strip() == target_profile
        for row in _handoff_policy(config, platform_name)
    )
    if not exact_match:
        return _invalid("bot_handoff_unauthorized")
    if replay_store is None:
        return _invalid("bot_handoff_replay_store_unavailable")
    try:
        claimed = replay_store.claim(
            platform=platform_name,
            sender_id=sender_id,
            target_profile=target_profile,
            request_id=request_id,
        )
    except Exception:
        return _invalid("bot_handoff_replay_store_unavailable")
    if not claimed:
        return _invalid("bot_handoff_replay")

    handoff = {
        "kind": "handoff", "version": 1, "request_id": request_id,
        "source_agent": source_agent, "target_profile": target_profile,
    }
    return BotIngressDecision(True, False, "typed_handoff", message, handoff)

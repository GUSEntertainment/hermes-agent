"""Default-off fresh-session rotation policy for selected gateway lanes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_DM_CHAT_TYPES = {"private", "dm", "direct", "direct_message"}
_DEFAULT_TOKEN_THRESHOLD = 120_000


@dataclass(frozen=True)
class RotationDecision:
    rotate: bool
    reason: str = ""
    token_count: int = 0
    threshold: int = _DEFAULT_TOKEN_THRESHOLD
    token_source: str = "estimated"


def _value(source: Any, name: str) -> str:
    value = getattr(source, name, "")
    return str(getattr(value, "value", value) or "").strip().lower()


def _set(value: Any) -> set[str]:
    raw: Sequence[Any] = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else [value]
    return {part.strip().lower() for item in raw if item is not None for part in str(item).split(",") if part.strip()}


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    raw = config.get("session_rotation")
    if raw is None and isinstance(config.get("gateway"), Mapping):
        raw = config["gateway"].get("session_rotation")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _block(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, Mapping) else {}


def _selectors(source: Any, session_key: str) -> dict[str, str]:
    return {
        "session_keys": session_key.lower(),
        "platforms": _value(source, "platform"),
        "chat_types": _value(source, "chat_type"),
        "chat_ids": _value(source, "chat_id"),
        "thread_ids": _value(source, "thread_id"),
        "tenants": _value(source, "tenant"),
        "boards": _value(source, "board") or _value(source, "board_id"),
        "lane_kinds": _value(source, "lane_kind"),
    }


def _matches(source: Any, session_key: str, raw: Mapping[str, Any], name: str) -> bool:
    block = _block(raw, name)
    for key, actual in _selectors(source, session_key).items():
        values = set(_set(block.get(key)))
        values.update(_set(raw.get(f"{name}_{key}")))
        if name == "include":
            values.update(_set(raw.get(key)))
        if actual and actual in values:
            return True
    return False


def _dm_exact(source: Any, session_key: str, raw: Mapping[str, Any]) -> bool:
    chat_id = _value(source, "chat_id")
    key = session_key.lower()
    include = _block(raw, "include")
    if chat_id and chat_id in (_set(include.get("chat_ids")) | _set(raw.get("chat_ids")) | _set(raw.get("include_chat_ids"))):
        return True
    if key and key in (_set(include.get("session_keys")) | _set(raw.get("session_keys")) | _set(raw.get("include_session_keys"))):
        return True
    opt = _block(raw, "interactive_dm_opt_in")
    return _truthy(opt.get("enabled")) and (
        (chat_id and chat_id in _set(opt.get("allow_chat_ids")))
        or (key and key in _set(opt.get("allow_session_keys")))
    )


def rotation_enabled(config: Mapping[str, Any] | None) -> bool:
    """Cheap default-off gate used before any transcript read."""
    return _truthy(_cfg(config).get("enabled", False))


def should_rotate_session(
    *, source: Any, session_key: str, history: list[dict[str, Any]],
    last_prompt_tokens: int = 0, estimated_tokens: int = 0,
    config: Mapping[str, Any] | None = None,
) -> RotationDecision:
    raw = _cfg(config)
    if not _truthy(raw.get("enabled", False)):
        return RotationDecision(False, "disabled")
    try:
        threshold = int(raw.get("token_threshold", raw.get("threshold", _DEFAULT_TOKEN_THRESHOLD)))
    except (TypeError, ValueError):
        return RotationDecision(False, "invalid_threshold")
    if threshold <= 0:
        return RotationDecision(False, "invalid_threshold", threshold=threshold)
    if not history or len(history) < 4:
        return RotationDecision(False, "short_history", threshold=threshold)
    if _matches(source, session_key, raw, "exclude"):
        return RotationDecision(False, "excluded", threshold=threshold)
    token_count = int(last_prompt_tokens or 0)
    token_source = "actual" if token_count > 0 else "estimated"
    if token_count <= 0:
        token_count = int(estimated_tokens or 0)
    if token_count < threshold:
        return RotationDecision(False, "below_threshold", token_count, threshold, token_source)

    chat_type = _value(source, "chat_type")
    exact_dm = _dm_exact(source, session_key, raw) if chat_type in _DM_CHAT_TYPES else False
    if chat_type in _DM_CHAT_TYPES and not exact_dm:
        return RotationDecision(False, "dm_exempt", token_count, threshold, token_source)
    explicit = _matches(source, session_key, raw, "include") or exact_dm
    mode = str(raw.get("mode", "opt_in")).strip().lower()
    selected = mode == "all" or (mode in {"all_non_dm", "non_dm", "groups"} and chat_type not in _DM_CHAT_TYPES)
    if not explicit and not selected:
        return RotationDecision(False, "not_selected", token_count, threshold, token_source)
    return RotationDecision(True, "threshold", token_count, threshold, token_source)


def build_pickup_brief_messages(*, old_session_id: str, new_session_id: str, decision: RotationDecision, source: Any) -> list[dict[str, str]]:
    return [{
        "role": "assistant",
        "content": (
            "Fresh-session rotation pickup brief.\n"
            f"A prior lane session crossed {decision.token_count:,} {decision.token_source} tokens "
            f"(threshold {decision.threshold:,}) and was replaced.\n"
            "Source and session identifiers are redacted from this durable brief.\n"
            "Continue from the incoming user message. If older context is required, use "
            "session_search, kanban_show, or the relevant Desk/board artifact."
        ),
    }]

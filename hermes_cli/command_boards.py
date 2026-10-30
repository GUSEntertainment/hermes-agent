from __future__ import annotations

from typing import Any


def _validated_default(default: Any) -> tuple[str, ...] | None:
    if not isinstance(default, str):
        return None
    value = default.strip()
    return (value,) if value else None


def resolve_command_boards(
    config: Any, *, default: str = "chris-home"
) -> tuple[str, ...] | None:
    """Resolve the ordered command-board allowlist.

    An explicitly present ``command_boards`` is authoritative and fail-closed:
    malformed input selects zero boards. Only absence permits the validated
    singular legacy value and, when that is absent, the validated default.
    """
    fallback = _validated_default(default)
    if not isinstance(config, dict):
        return fallback
    kanban = config.get("kanban")
    if not isinstance(kanban, dict):
        return fallback
    if "command_boards" in kanban:
        raw = kanban.get("command_boards")
        if not isinstance(raw, list) or any(
            not isinstance(item, str) for item in raw
        ):
            return ()
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            value = item.strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return tuple(result)
    if "command_board" in kanban:
        legacy = kanban.get("command_board")
        if not isinstance(legacy, str) or not legacy.strip():
            return ()
        return (legacy.strip(),)
    return fallback

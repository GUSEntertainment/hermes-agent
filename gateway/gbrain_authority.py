"""Requester-scoped GBrain discovery and dispatch. No identity from model args/env.

The seven-verb transports use separate source-scoped grants on one writer. This first deployment
is read-only; trusted ingestion is a separate, scrubbed pipeline, not a chat verb.
Family activation follows the owner-recorded test waiver and populated source-grant denials.
"""
from __future__ import annotations

import json
from typing import Any

# Exact registered server names. Unknown GBrain names are denied, not inferred.
REALMS = {"gbrain": "business", "gbrain_business": "business",
          "gbrain_private": "chris-only", "gbrain_family": "family"}
READ_VERBS = frozenset({"recall", "entity", "context_pack", "delta"})
DENIED = json.dumps({"error": "Tool unavailable"})
# Owner-approved activation; source-grant denials passed; Josh test gate waived.
FAMILY_ENABLED = True


def _text(var: Any) -> str:
    value = var.get()
    return value if isinstance(value, str) else ""


def authority() -> tuple[str, bool]:
    """Return authenticated human and private-audience flag, or deny.

    API execution_scope is credential-bound by the native API adapter. Telegram
    identity comes only from task-local adapter context; private recall additionally
    requires a one-to-one DM addressed to that exact authenticated user. No DB,
    name/title, parent-task, profile, chat-id-as-identity, or environment fallback.
    """
    try:
        from gateway.josh_board_authority import _execution
        scope = _execution.get()
        if scope is not None:
            profile, principal = scope
            if principal not in {"chris", "connor"}:
                return "unknown", False
            # An API owner visiting another person's lane does not grant private recall.
            return principal, principal == "chris" and profile in {None, "default", "chris"}
        from gateway import session_context as sc
        if _text(sc._SESSION_PLATFORM) != "telegram":
            return "unknown", False
        uid = _text(sc._SESSION_USER_ID)
        # Shared-history policy identities, exact match; no substring inference.
        who = {"1963997029": "chris", "8668899280": "connor"}.get(uid, "unknown")
        private = (_text(sc._SESSION_CHAT_TYPE) in {"dm", "private"}
                   and _text(sc._SESSION_CHAT_ID) == uid)
        # A known sender does not authenticate everyone reading a group reply.
        if not private:
            return "unknown", False
        return who, private
    except Exception:
        return "unknown", False


def is_gbrain_server(server: str) -> bool:
    return server.lower().replace("-", "_").startswith("gbrain")


def can_call(server: str, verb: str) -> bool:
    if not is_gbrain_server(server):
        return True
    realm = REALMS.get(server)
    if realm is None or verb not in READ_VERBS:
        return False
    who, private = authority()
    if realm == "business":
        return who in {"chris", "connor"}
    if realm == "family" and not FAMILY_ENABLED:
        return False
    return who == "chris" and private


def tool_allowed(name: str) -> bool:
    if not name.startswith("mcp__"):
        return True
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return False
    return can_call(parts[1], parts[2])


def filter_definitions(definitions: list[dict]) -> list[dict]:
    return [d for d in definitions if tool_allowed(d.get("function", {}).get("name", ""))]


def cache_scope() -> tuple:
    # Include audience as well as identity: Chris's DM must not warm a group cache.
    return (*authority(), FAMILY_ENABLED)


def dispatch_error(server: str, verb: str, args: dict) -> str | None:
    if not is_gbrain_server(server):
        return None
    if not can_call(server, verb):
        return DENIED
    # Realm and authenticated identity are runtime-owned, never model-controlled.
    forbidden = {"source", "source_id", "sourceId", "sources", "realm", "home",
                 "user_id", "principal", "session_id", "sessionId", "_meta"}
    if not isinstance(args, dict) or forbidden.intersection(args):
        return DENIED
    return None

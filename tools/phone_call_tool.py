#!/usr/bin/env python3
"""Hermes registry wrapper for the server-authorized phone obligation API."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROCESSORS = Path(
    os.getenv(
        "PHONE_ASSISTANT_PROCESSORS",
        "/Users/Chris/.openclaw/workspace/Agent-Hermes/phone-stack/processors",
    )
)
if str(PROCESSORS) not in sys.path:
    sys.path.insert(0, str(PROCESSORS))

from phone_agent_requests import default_setting_value, safe_tool_result  # noqa: E402
from tools.registry import registry  # noqa: E402


PHONE_CALL_SCHEMA = {
    "name": "phone_call",
    "description": (
        "Create, list, inspect, acknowledge, cancel, or reschedule the enrolled caller's durable "
        "phone-call obligations. Use create only when the caller explicitly asks for a "
        "phone call. Urgent mode requires a concrete, fresh event and server-side "
        "profile authorization; a missed message or priority label is insufficient. "
        "This tool queues a call and returns a receipt; it does not prove that the caller "
        "answered. Acknowledge and cancel require the exact reminder ID and revision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create", "list", "status", "acknowledge", "cancel", "reschedule"],
            },
            "mode": {"type": "string", "enum": ["explicit_request", "urgent"]},
            "reminder_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 1200},
            "due_at": {
                "description": "ISO-8601 timestamp with UTC offset, Unix timestamp, or 'now'.",
                "anyOf": [{"type": "string"}, {"type": "number"}],
            },
            "expires_at": {
                "description": "ISO-8601 timestamp with UTC offset or Unix timestamp.",
                "anyOf": [{"type": "string"}, {"type": "number"}],
            },
            "timezone": {"type": "string"},
            "original_expression": {"type": "string"},
            "realm": {"type": "string", "enum": ["business", "owner:chris", "owner:connor"]},
            "account": {"type": "string", "enum": ["chris:gus"]},
            "recurrence_key": {"type": "string"},
            "urgency": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_evidence_ref": {"type": "string"},
                    "concrete_situation": {"type": "string"},
                    "deadline_at": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                    "why_wait_harmful": {"type": "string"},
                    "why_call_helps": {"type": "string"},
                    "source_valid_until": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                },
                "required": [
                    "source_evidence_ref",
                    "concrete_situation",
                    "deadline_at",
                    "why_wait_harmful",
                    "why_call_helps",
                    "source_valid_until",
                ],
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
}


def _available() -> bool:
    # Visibility is not authority. Every invocation validates bound caller state.
    return (PROCESSORS / "phone_agent_requests.py").is_file()


registry.register(
    name="phone_call",
    toolset="phone",
    schema=PHONE_CALL_SCHEMA,
    handler=lambda args, **_kwargs: safe_tool_result(args),
    check_fn=_available,
    description=PHONE_CALL_SCHEMA["description"],
    emoji="☎️",
)

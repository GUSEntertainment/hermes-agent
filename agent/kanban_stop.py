"""Turn-end guard for kanban workers.

Kanban workers must end with a successful completion, review, owner handoff, or block. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional

from agent.delegation_context import owned_kanban_task


# Every tool that ends this worker's responsibility for the card, not just the two that
# close it out: ``kanban_request_review`` moves it to ``review`` (goals.py's continuation /
# finalize prompts tell builders to call it) and ``kanban_request_changes`` returns it to
# ``ready`` (the sdlc-review skill tells reviewers to). Nudging after either asks a worker
# that did the right thing to ``kanban_complete`` a card it must not close.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_schedule",
    "kanban_request_review",
    "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    # In-process children inherit the parent process environment, but never
    # own its native task. Reuse the same context predicate as tool admission;
    # do not clear process-wide env or weaken the parent terminal requirement.
    from agent.delegation_context import is_dispatcher_owned_worker_context
    if not is_dispatcher_owned_worker_context():
        return False
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """Only confirmed, same-task handoffs end a worker's responsibility."""
    if not messages:
        return False
    pending = {}
    terminal = _TERMINAL_KANBAN_TOOLS | {"kanban_request_review", "kanban_handoff", "kanban_request_changes", "kanban_rebind_review_artifact"}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if call_id:
                    pending[call_id] = _tool_call_name(tc)
        elif msg.get("role") == "tool":
            name = msg.get("name") or pending.get(msg.get("tool_call_id"))
            if name not in terminal:
                continue
            try:
                result = json.loads(msg.get("content") or "{}")
            except (ValueError, TypeError):
                continue
            if (not isinstance(result, dict) or result.get("ok") is not True
                    or result.get("error") or result.get("success") is False
                    or result.get("task_id") != os.environ.get("HERMES_KANBAN_TASK")):
                continue
            if name == "kanban_handoff" and (result.get("phase") not in {"execute", "advisory_wait"}
                                              or result.get("status") not in {"ready", "todo"}):
                continue
            if name == "kanban_request_review" and result.get("status") != "review":
                continue
            if result.get('continue_work') is True or result.get('status') == 'running':
                continue
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    # The transcript is the status source: this text is only reached when the session made no
    # handoff call, so it never tells a worker to close a card it already sent to review.
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"No successful completion or review handoff was observed for task `{tid}`. "
        "Read `kanban_show` to confirm current state before changing it. "
        "Ending a still-running task without a board handoff causes a protocol violation.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, `kanban_request_review` when the verified deliverable needs review, "
        "OR `kanban_block(reason=...)` for a genuine unresolved blocker. "
        "Correct rejected tool arguments and retry; preserve an already successful review handoff.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]

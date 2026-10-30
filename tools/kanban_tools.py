"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

Registered only under the dispatcher (``HERMES_KANBAN_TASK`` set) or when the profile
enables the ``kanban`` toolset. Tools rather than ``hermes kanban`` shell-outs: they run
in the agent's process (reach ``kanban.db`` from a container/SSH terminal backend, no
shlex quoting of JSON metadata, structured-JSON failures). Humans use CLI/dashboard.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import no_cache_check_fn, registry, tool_error
from hermes_cli.config import cfg_get, load_config
from tools.kanban_tools_schemas import (
    KANBAN_ATTACH_SCHEMA,
    KANBAN_ATTACH_URL_SCHEMA, KANBAN_ATTACHMENTS_SCHEMA, KANBAN_BLOCK_SCHEMA, KANBAN_COMMENT_SCHEMA,
    KANBAN_COMPLETE_SCHEMA, KANBAN_CREATE_SCHEMA, KANBAN_HEARTBEAT_SCHEMA, KANBAN_LINK_SCHEMA,
    KANBAN_LIST_SCHEMA, KANBAN_REQUEST_CHANGES_SCHEMA, KANBAN_REQUEST_REVIEW_SCHEMA,
    KANBAN_SCHEDULE_SCHEMA, KANBAN_SHOW_SCHEMA, KANBAN_UNBLOCK_SCHEMA)

logger = logging.getLogger(__name__)

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


# --- Gating ---

def _profile_has_kanban_toolset() -> bool:
    from tools.kanban_toolset_context import kanban_toolset_requested

    requested = kanban_toolset_requested()
    if requested:
        return True
    try:
        config = load_config()
        # Preserve the legacy profile-wide opt-in for callers using bundles.
        if "kanban" in (config.get("toolsets") or []):
            return True
        if requested is not None:
            # Never borrow another platform's opt-in during schema assembly.
            return False
        # Offer-time skill discovery has no platform selection. A saved opt-in
        # makes the playbook relevant; actual schemas still use the scope above.
        from hermes_cli.tools_config import _get_platform_tools

        platforms = config.get("platform_toolsets") or {}
        return any(
            "kanban" in _get_platform_tools(config, platform, include_default_mcp_servers=False)
            for platform, names in platforms.items() if isinstance(names, list)
        )
    except Exception:
        return False


def _delegation_ctx(predicate: str, default: bool) -> bool:
    """``agent.delegation_context.<predicate>()``; ``default`` when it cannot be evaluated."""
    try:
        from agent import delegation_context
        return getattr(delegation_context, predicate)()
    except Exception:
        return default


def _is_delegated_child_context() -> bool:
    return _delegation_ctx("is_delegated_child_context", False)


def _is_dispatcher_owned_worker() -> bool:
    """False for delegate_task children AND for cron jobs fired in-process from
    a worker — i.e. whenever HERMES_KANBAN_* is present but not ours."""
    return _delegation_ctx("is_dispatcher_owned_worker_context", True)


def _visible(*, to_env_worker: bool) -> bool:
    """check_fn core: never for delegate children; dispatcher-spawned env workers
    (HERMES_KANBAN_TASK) per flag; else the profile toolset decides."""
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return to_env_worker
    return _profile_has_kanban_toolset()


@no_cache_check_fn
def _check_kanban_mode() -> bool:
    """Lifecycle tools: dispatcher workers + profiles with the ``kanban`` toolset."""
    return _visible(to_env_worker=True)


@no_cache_check_fn
def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock): hidden from task workers."""
    return _visible(to_env_worker=False)


# --- Shared helpers: validation failures raise _Reject; _kanban_handler renders it ---

# Worker tools that terminate or transition a run's ownership. An unbound worker
# (HERMES_KANBAN_RUN_ID unresolvable) must not run these: expected_run_id=None
# would silently skip the run-ownership CAS in kanban_db. Non-lifecycle tools
# (heartbeat / attach / attach_url) do not terminate a run and are not gated.
_RUN_LIFECYCLE_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_schedule",
    "kanban_request_review", "kanban_request_changes",
    "kanban_handoff", "kanban_rebind_review_artifact",
})

class _Reject(Exception):
    """Carries a finished ``tool_error`` payload out of a validation helper."""

    def __init__(self, message: str):
        super().__init__(tool_error(message))


def _check(cond: Any, message: str) -> None:
    """Reject (as a tool error) unless ``cond`` is truthy."""
    if not cond:
        raise _Reject(message)


# Keys a handler reads that its LLM-facing schema deliberately does not declare:
# ``session_id`` is provenance stamped by internal callers (31fe2290393), ``project_id``
# the pre-``project`` alias still honoured by ``_handle_create`` (e7811345c17).
_UNDECLARED_ARGS: dict[str, frozenset[str]] = {
    "kanban_create": frozenset({"session_id", "project_id"}),
    # ``title`` is the pre-schema alias of ``filename`` that ``_handle_attach_url`` still honours.
    "kanban_attach_url": frozenset({"title"}),
}


def _persisted_identity() -> str:
    """Profile name persisted into board records (comment author, task creator).

    ``hermes_cli.profiles.current_profile_name`` resolves the profile this call runs FOR — the bound
    home override under a multiplexed tick or turn, else the dispatcher's ``HERMES_PROFILE`` pin,
    else the process home; the generic ``"worker"`` only when nothing names a profile. Never taken
    from tool args: board records are injected into future workers' prompts, so a caller-supplied
    identity could forge an authoritative-looking author (see #19713).
    """
    from hermes_cli.profiles import current_profile_name

    return current_profile_name("worker") or "worker"


def _private_task_boundary(name, handler):
    from functools import wraps
    @wraps(handler)
    def guarded(args, **kw):
        from gateway.member_worker_policy import authorize_private_kanban
        policy = authorize_private_kanban(name, args)
        if policy is not None:
            args = dict(args, task_id=args.get('task_id') or policy['task'], board=policy['board'])
        result = handler(args, **kw)
        if policy is not None and name in {'kanban_show', 'kanban_attachments'}:
            # Never return a page assembled under authority revoked during I/O.
            if authorize_private_kanban(name, args) != policy:
                raise PermissionError('Private task authority changed during read')
        return result
    return guarded


def _kanban_handler(tool_name: str) -> Callable:
    """Wrap a handler so every failure is a structured tool error. ``ValueError``
    (invalid board slug, DB validation such as cycle/self-link, ``AttachmentTooLarge``)
    is reported without a traceback; anything else is logged with ``logger.exception``."""
    def deco(fn):
        fn = _private_task_boundary(tool_name, fn)
        @functools.wraps(fn)
        def wrapper(args: dict, **kw) -> str:
            try:
                # Reject typos before a handoff can succeed without its artifacts.
                properties = registry.get_schema(tool_name)["parameters"]["properties"]
                allowed = set(properties) | _UNDECLARED_ARGS.get(tool_name, frozenset())
                unknown = sorted(set(args) - allowed)
                _check(not unknown,
                       f"{tool_name}: unknown parameter(s): {', '.join(unknown)}. "
                       f"Valid parameters: {', '.join(sorted(properties))}. Nothing changed.")
                return fn(args, **kw)
            except _Reject as e:
                return e.args[0]
            except Exception as e:
                if not isinstance(e, ValueError):
                    logger.exception(f"{tool_name} failed")
                return tool_error(f"{tool_name}: {e}")
        return wrapper
    return deco


def _reject_delegated_child_mutation(tool_name: str) -> None:
    """A delegate_task child shares the parent's process, so inherited HERMES_KANBAN_*
    env is not proof of ownership: it may report findings but must not mutate."""
    if _delegation_ctx("is_delegated_child_process_context", False):
        raise _Reject(
            f"{tool_name} refused: delegate_task child agents are not Kanban run owners. "
            "Return findings to the parent agent; the dispatcher worker or an explicitly "
            "configured Kanban orchestrator must perform board mutations.")


def _default_task_id(arg: Any) -> Optional[str]:
    """``task_id`` arg or the dispatcher's env var. A delegate child or an
    in-process cron job must never inherit the worker's task id implicitly."""
    bound = os.environ.get("HERMES_KANBAN_TASK")
    if bound and os.environ.get("HERMES_PROFILE") in {"claude-review", "fable-review"} and arg and arg != bound:
        raise PermissionError("Advisory workers can access only their own redacted packet task")
    if arg is not None:
        val = str(arg).strip()
        if val:
            return val
    if _is_delegated_child_context() or not _is_dispatcher_owned_worker():
        return None
    if not _is_dispatcher_owned_worker():
        # A cron job fired in-process from a worker must never inherit the
        # worker's task id as an implicit default.
        return None
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid:
        val = env_tid.strip()
        if val:
            return val
    return None


def _require_task_id(args: dict) -> str:
    """Resolve the target task or reject with a message the caller can act on.

    The env default only exists for a dispatcher-spawned worker; every other
    caller must name a task, so a rejection points at ``kanban_list`` instead of
    an env var a chat caller cannot set (#91431).
    """
    tid = _default_task_id(args.get("task_id"))
    if tid:
        return tid
    if os.environ.get("HERMES_KANBAN_TASK"):
        # Env task present but not usable here (delegate child / cron run beside
        # a worker): it is not this session's to default to.
        raise _Reject(
            "task_id is required: this session does not own the inherited "
            "HERMES_KANBAN_TASK, so it is not a valid default. Pass an explicit "
            "task_id.")
    raise _Reject(
        "task_id is required: this session has no dispatcher-assigned task to "
        "default to. Pass an explicit task_id; discover task ids with kanban_list.")


def _own_task_env(task_id: str, var: str) -> Optional[str]:
    """``$var`` only when this worker is scoped to ``task_id``; else None."""
    return os.environ.get(var) if os.environ.get("HERMES_KANBAN_TASK") == task_id else None

def _worker_run_id(task_id: str) -> Optional[int]:
    """This worker's dispatcher run id when it is scoped to task_id."""
    raw = _own_task_env(task_id, "HERMES_KANBAN_RUN_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _stamp_worker_session_metadata(task_id: str, metadata: Optional[dict]) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    session_id = _own_task_env(task_id, "HERMES_SESSION_ID")
    return {**(metadata or {}), "worker_session_id": session_id} if session_id else metadata




def _normalize_authorization_metadata(metadata: Optional[dict]) -> tuple[Optional[dict], Optional[str]]:
    """Normalize versioned authorization-continuity metadata if present."""
    if metadata is None or "authorization_continuity" not in metadata:
        return metadata, None
    from hermes_cli import kanban_db as kb

    normalized = kb.normalize_authorization_continuity(
        metadata.get("authorization_continuity")
    )
    if normalized is None:
        return None, "metadata.authorization_continuity must be an object"
    updated = dict(metadata)
    updated["authorization_continuity"] = normalized
    return updated, None


def _authorization_contract_for_child(
    kb: Any,
    conn: Any,
    *,
    self_task_id: Optional[str],
    parents: list[str],
    requested: Any,
) -> tuple[Optional[dict], Optional[str]]:
    """Return the authorization block safe to append to a child task body."""
    if requested is not None:
        normalized = kb.normalize_authorization_continuity(requested)
        if normalized is None:
            return None, "authorization_continuity must be an object"
        if not kb.authorization_continuity_allows_inheritance(normalized):
            return None, (
                "authorization_continuity cannot mark this child as previously "
                "authorized because it contains an explicit hold, scope expansion, "
                "or unresolved Chris decision. Create the card without inherited "
                "authorization, or make the new decision concrete in the task spec."
            )
        return normalized, None

    if not self_task_id or self_task_id not in set(parents):
        return None, None
    inherited = kb.authorization_continuity_for_task(
        conn, self_task_id, include_parents=True
    )
    if not kb.authorization_continuity_allows_inheritance(inherited):
        return None, None
    inherited = dict(inherited)
    existing = inherited.get("inherited_from") or []
    if isinstance(existing, str):
        existing = [existing]
    inherited["inherited_from"] = list(dict.fromkeys([self_task_id, *existing]))[:8]
    inherited.setdefault(
        "inheritance",
        "Inherited only for child work that stays inside the parent task scope.",
    )
    return inherited, None
def _enforce_worker_task_ownership(tid: str) -> None:
    """A dispatcher-spawned worker may only mutate its own HERMES_KANBAN_TASK; a
    prompt-injected ``task_id`` must not corrupt sibling/cross-tenant runs.
    Orchestrators (toolset enabled, no env task) legitimately route child tasks.

    Tools like ``kanban_complete`` / ``kanban_block`` / ``kanban_heartbeat`` mutate run-lifecycle state, so
    a buggy or prompt-injected worker that passed an explicit ``task_id`` for some other task could corrupt
    sibling or cross-tenant runs (see #19534).
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        raise _Reject(
            f"worker is scoped to task {env_tid}; refusing to mutate {tid}. Use kanban_comment "
            f"to hand off information to other tasks, or kanban_create to spawn follow-up work.")


def _worker_guard(tool_name: str, args: dict) -> str:
    """Worker mutation preamble, in order: delegate-child rejection, task id
    resolution, task-scope ownership, run-identity proof. Returns the task id.

    A dispatcher-spawned worker (``HERMES_KANBAN_TASK`` set) that cannot name
    its run id is refused on the run-lifecycle mutations: ``expected_run_id=None``
    would silently skip the run-ownership CAS in ``kanban_db`` (``complete_task`` /
    ``block_task`` / ``request_review`` / ``request_changes`` only append
    ``AND current_run_id = ?`` when the value is not ``None``), so an unbound
    stale worker could complete a card a live successor owns. This mirrors
    ``agent/kanban_stop.py``, which already treats an unbound run id as unknown
    and fails closed. CLI / human / orchestrator paths (no ``HERMES_KANBAN_TASK``)
    legitimately pass ``expected_run_id=None`` and are unaffected. Non-lifecycle
    worker tools (heartbeat / attach / attach_url) do not terminate a run and are
    not gated here.
    """
    _reject_delegated_child_mutation(tool_name)
    tid = _require_task_id(args)
    _enforce_worker_task_ownership(tid)
    if (
        tool_name in _RUN_LIFECYCLE_TOOLS
        and os.environ.get("HERMES_KANBAN_TASK")
        and _worker_run_id(tid) is None
    ):
        raise _Reject(
            f"{tool_name} refused: this worker cannot resolve its "
            "HERMES_KANBAN_RUN_ID, so it cannot prove ownership of the card's "
            "current run. A stale or unbound worker must not terminate a run a "
            "live successor owns. Re-run through the dispatcher so the run id is "
            "pinned, or use an orchestrator/CLI path that passes an explicit "
            "expected_run_id."
        )
    return tid


def _require_orchestrator_tool(tool_name: str) -> None:
    """The check_fn already hides orchestrator tools from workers; this catches
    a stale registration or test harness routing a worker here anyway."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise _Reject(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers must use "
            "kanban_complete, kanban_request_review, kanban_request_changes, kanban_block, "
            "kanban_heartbeat, or kanban_comment for their assigned task.")


def _connect(board: Optional[str] = None):
    """Authority-checked board open -> ``(kb, conn)``; caller closes ``conn``.

    [GUS v0.21.5 port] Kept as the single admission path (member/administrator/
    private-worker/advisory board scoping); upstream's ``_board`` context manager
    delegates here. A failed post-open validation closes the connection."""
    if (os.environ.get("HERMES_KANBAN_TASK")
            and os.environ.get("HERMES_PROFILE") in {"claude-review", "fable-review"}
            and board and board != os.environ.get("HERMES_KANBAN_BOARD")):
        raise PermissionError("Advisory workers cannot switch boards")
    from gateway.josh_board_authority import admitted_board, _execution
    from gateway.member_worker_policy import authenticated_private_policy
    from hermes_cli.board_owner_manage import (
        require_owner, member_board, administrator_board, _validate_member_connection,
    )
    member = None
    administrative = None
    if _execution.get() is not None and authenticated_private_policy() is None:
        member = require_owner()
    if member is not None:
        administrative = administrator_board(member, board)
        board = administrative or member_board(member, board)
    else:
        board = admitted_board(board)
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    try:
        if member is not None:
            _validate_member_connection(conn, {'board': administrative} if administrative else member)
            if require_owner() != member:
                raise PermissionError(
                    'Administrator enrollment changed while opening board' if administrative
                    else 'Member enrollment changed while opening board')
            if administrative:
                administrator_board(member, administrative)
    except Exception:
        conn.close()
        raise
    return kb, conn


@contextmanager
def _board(board: Optional[str], *, quiet_close: bool = False):
    """``with _board(slug) as (kb, conn)``; lazy import so the module loads in non-kanban
    contexts. ``board=None`` keeps the env/symlink resolution chain; an explicit slug
    overrides it per call. ``quiet_close`` swallows close() errors (best-effort bridges)."""
    kb, conn = _connect(board=board)
    try:
        yield kb, conn
    finally:
        try:
            conn.close()
        except Exception:
            if not quiet_close:
                raise


def _existing_task(kb, conn, tid: str):
    task = kb.get_task(conn, tid)
    _check(task is not None, f"task {tid} not found")
    return task


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _ok_landed(kb, conn, tid: str, default_status: str, **extra: Any) -> str:
    """Success payload reporting where the task actually landed (routing may
    not leave it in the requested status)."""
    run = kb.latest_run(conn, tid)
    landed = kb.get_task(conn, tid)
    return _ok(task_id=tid, run_id=run.id if run else None,
               status=landed.status if landed else default_status, **extra)


def _redact(value: Any) -> str:
    return redact_sensitive_text(str(value), force=True)


def _redact_opt(value: Any) -> Any:
    return _redact(value) if value else value


def _redact_metadata(metadata: dict) -> Optional[dict]:
    """Redact via a JSON round-trip; None if the result can't be re-parsed."""
    try:
        return json.loads(redact_sensitive_text(json.dumps(metadata), force=True))
    except json.JSONDecodeError:
        return None


def _coerce_str_list(value: Any, name: str, what: str, *, strip: bool = False):
    """Accept a single string (convenience) or a list/tuple; with ``strip`` the
    items are stringified, stripped, and empties dropped."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _Reject(f"{name} must be a list of {what}, got {type(value).__name__}")
    if strip:
        value = [str(x).strip() for x in value if str(x).strip()]
    return value


def _require_dict_metadata(metadata: Any) -> None:
    _check(metadata is None or isinstance(metadata, dict),
           f"metadata must be an object/dict, got {type(metadata).__name__}")


def _merge_artifacts(metadata: Any, artifacts: list[str]) -> dict:
    """Fold ``artifacts`` into ``metadata["artifacts"]`` (merged with, never overwriting, a
    list the worker passed manually). Artifacts ride inside metadata so the completed-event
    payload needs no DB schema change; the gateway notifier uploads each as an attachment."""
    _require_dict_metadata(metadata)
    metadata = {} if metadata is None else metadata
    existing = metadata.get("artifacts")
    if isinstance(existing, (list, tuple)):
        merged = (str(item).strip() for item in [*existing, *artifacts])
        metadata["artifacts"] = list(dict.fromkeys(s for s in merged if s))
    else:
        metadata["artifacts"] = artifacts
    return metadata


def _require_text(args: dict, name: str, message: Optional[str] = None) -> Any:
    """``args[name]``; rejects when missing or blank."""
    value = args.get(name)
    _check(value and str(value).strip(), message or f"{name} is required")
    return value


_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


def _parse_bool_arg(args: dict, name: str) -> bool:
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return bool(value)
    parsed = _BOOL_WORDS.get(str(value).strip().lower())
    _check(parsed is not None, f"{name} must be a boolean or 'true'/'false'")
    return parsed


def _opt_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    return int(value) if value is not None else default


_TASK_FIELDS = tuple(
    "id title body assignee status tenant priority workspace_kind workspace_path created_by "
    "created_at started_at completed_at result current_run_id model_override "
    "provider_override completion_contract last_failure_error capability_status "
    "capability_checked_at".split())
_TASK_SUMMARY_FIELDS = tuple(
    "id title assignee status priority tenant workspace_kind workspace_path project_id created_by "
    "created_at started_at completed_at current_run_id model_override provider_override".split())
_RUN_FIELDS = tuple("id profile status outcome summary error metadata started_at ended_at".split())
_COMMENT_FIELDS = ("author", "body", "created_at")
_EVENT_FIELDS = ("kind", "payload", "created_at", "run_id")
_ATTACHMENT_FIELDS = tuple(
    "id filename content_type size uploaded_by stored_path created_at".split())
_CREATED_FIELDS = ("status", "workspace_kind", "workspace_path", "project_id")


def _fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """``{name: getattr(obj, name)}``; every value None when ``obj`` is None."""
    return {n: getattr(obj, n) if obj is not None else None for n in names}


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        **_fields(task, _TASK_SUMMARY_FIELDS), "parents": parents, "children": children,
        "parent_count": len(parents), "child_count": len(children)}


# --- Goal-mode judge gate ---

_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """``judge_goal`` fails open (no auxiliary model -> ``"continue"``), which is
    indistinguishable from "not done yet" and would wedge every goal_mode
    worker; so the gate is enforced only when a judge is actually reachable."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


# Per-tool guidance for a judge rejection: verdict -> message. ``{reason}``/``{tid}`` are filled in.
_GOAL_GATE_MESSAGES = {
    "kanban_complete": {
        "blocked": (
            "Goal completion rejected: judge ruled the goal unachievable — {reason}. The task "
            "will NOT complete silently. Either re-scope the task with kanban_edit, or record "
            "the block with kanban_block and hand the decision to a human / reviewer."),
        "continue": (
            "Goal completion rejected by judge: {reason}. To proceed, either: (1) provide "
            "explicit acceptance evidence in your summary matching the task's criteria, or (2) "
            "create continuation tasks with parents=[{tid}] and keep this task alive.")},
    "kanban_request_review": {
        "blocked": (
            "Goal review handoff rejected: judge ruled the goal unachievable — {reason}. "
            "Record the block with kanban_block instead of requesting review."),
        "continue": (
            "Goal review handoff rejected by judge: {reason}. Provide acceptance evidence "
            "matching the card before requesting review.")}}


def _goal_mode_handoff_rejection(task, evidence: str, *, phase: str = "completion") -> Optional[str]:
    """[GUS v0.21.5 port] Custom phase-aware judge API (carried from live): returns
    the judge's rejection reason, or None to allow. ``review`` never consults an
    auxiliary model (native checks govern review entry); an unknown phase raises
    so it cannot disable completion judgment. Fails open like ``_goal_gate``."""
    if phase not in {"completion", "review"}:
        raise ValueError("unknown goal handoff phase")
    if phase == "review":
        return None
    if not task or not task.goal_mode or not _goal_judge_available():
        return None
    from hermes_cli.kanban_judgment import handoff_goal
    try:
        verdict, reason, _, _, transport_failed = judge_goal(
            goal=handoff_goal(task, phase=phase), last_response=evidence.strip())
    except Exception as judge_exc:
        logger.warning("goal judge check failed, allowing lifecycle handoff: %s", judge_exc, exc_info=True)
        return None
    if transport_failed:
        return None
    return None if verdict == "done" else reason


def _goal_gate(tool_name: str, task, tid: str, evidence: str) -> None:
    """Goal-mode pre-handoff judge gate: a worker must not complete / request
    review before acceptance criteria are met. ``blocked`` gets its own
    guidance; any other non-``done`` verdict gets the ``continue`` guidance.
    A broken judge fails open (logged) so it cannot permanently wedge work."""
    if tool_name == "kanban_request_review":
        return  # Native maker/reviewer/artifact checks govern entry to review.
    if not task or not task.goal_mode or not _goal_judge_available():
        return
    from hermes_cli.kanban_judgment import handoff_goal
    goal = handoff_goal(task, phase="completion")
    try:
        # Headless gate runs outside any agent turn: bind the per-task relay-affinity scope
        # (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{tid}")
        try:
            verdict, reason, _, _, transport_failed = judge_goal(
                goal=goal, last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        logger.warning(
            "goal judge check failed, allowing lifecycle handoff: %s", judge_exc, exc_info=True)
        return
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        logger.warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return
    if verdict == "done":
        return
    key = "blocked" if verdict == "blocked" else "continue"
    raise _Reject(_GOAL_GATE_MESSAGES[tool_name][key].format(reason=reason, tid=tid))


# --- Runtime-activity → board bridges (auto-heartbeat, live comment injection) ---
# The dispatcher watchdog reads ``tasks.last_heartbeat_at``, not the agent's in-process
# activity timestamp, so normal work is mirrored onto the board here (``kanban_heartbeat``
# stays for notes / pre-extending a claim). Best-effort: never raise into the agent loop;
# rate-limited per process (a race costs one harmless extra write); no-op outside a
# dispatcher-spawned worker.

# --------------------------------------------------------------------------- Runtime-activity →
# board-heartbeat bridge (#31752)
# --------------------------------------------------------------------------- When the agent ticks
# ``_touch_activity`` during normal work (between tool calls, mid-stream chunks, etc.), we want the kanban
# board's ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher watchdog (which reads
# ``tasks.last_heartbeat_at``, not the agent's in-process timestamp) doesn't reclaim an actively-running
# worker as stale. The model is not required to call the explicit ``kanban_heartbeat`` tool for this to work
# — that tool stays available for workers that want to attach a note or pre-emptively extend a claim across
# a known-long op. Constraints: - Best-effort: never raise. The agent loop must not care if the bridge fails
# (board missing, DB locked, etc.). - Rate-limited to one DB write per 60s per-process; runtime activity can
# tick on every chunk/tool result and we don't need that resolution. - No-op outside dispatcher-spawned
# worker context (no ``HERMES_KANBAN_TASK``). - No durable note on these auto-heartbeats; that's reserved
# for the explicit tool which carries a model-supplied note.
_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0
_auto_heartbeat_fence_warned = False


def register_current_worker_from_env() -> bool:
    """Record this worker's pid on its run when the dispatcher died before it could
    (``adopt_worker_pid``). False only when the board says the run was already reclaimed:
    the caller must exit. Anything unreadable (no run id, delegate child, board error)
    lets the worker run, as before."""
    tid = os.environ.get("HERMES_KANBAN_TASK")
    run_id = _worker_run_id(tid) if tid else None
    if run_id is None or _is_delegated_child_context():
        return True
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        with _board(None, quiet_close=True) as (_kb, conn):
            return kbd.adopt_worker_pid(conn, tid, run_id, os.getpid())
    except Exception:
        logger.debug("kanban worker registration for %s failed", tid, exc_info=True)
        return True


def heartbeat_current_worker_from_env() -> bool:
    """Claim extension + board heartbeat for the current worker; True iff both writes
    succeed. ``HERMES_KANBAN_RUN_ID`` pins the run row so a reclaimed stale run is not
    heartbeated; ``HERMES_KANBAN_CLAIM_LOCK`` absent -> default claimer (local workers)."""
    global _auto_heartbeat_last_attempt, _auto_heartbeat_fence_warned
    tid = os.environ.get("HERMES_KANBAN_TASK")
    now = time.monotonic()
    if not tid or (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    if _is_delegated_child_context():
        # An in-process delegate child's activity is not the worker's liveness; checked before
        # stamping the window so a chatty child cannot starve the worker's own heartbeat.
        return False
    _auto_heartbeat_last_attempt = now
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        with _board(None, quiet_close=True) as (kb, conn):
            ops = ((kb.heartbeat_claim, {"claimer": os.environ.get("HERMES_KANBAN_CLAIM_LOCK")}),
                   (kbd.heartbeat_worker, {"note": None, "expected_run_id": _worker_run_id(tid)}))
            succeeded = True
            for fn, kwargs in ops:
                op = fn.__name__
                try:
                    succeeded = bool(fn(conn, tid, **kwargs)) and succeeded
                except PermissionError as exc:
                    # The board fence rejected the worker's own liveness write: this process
                    # inherited HERMES_DELEGATED_CHILD_CONTEXT next to HERMES_KANBAN_TASK, so it is
                    # a delegate descendant, not the dispatcher's worker (kanban_complete refuses
                    # too). Loud once: at DEBUG the board just showed a worker that never beats.
                    succeeded = False
                    if not _auto_heartbeat_fence_warned:
                        _auto_heartbeat_fence_warned = True
                        logger.warning(
                            "kanban auto-heartbeat for task %s refused (%s): this process carries "
                            "HERMES_DELEGATED_CHILD_CONTEXT together with HERMES_KANBAN_TASK, so the board "
                            "treats it as a delegate_task descendant and its claim will not be extended by "
                            "activity. Only the dispatcher's own spawn grants worker scope; do not copy a "
                            "worker's environment into a hand-launched process.", tid, exc)
                except Exception:
                    logger.debug("auto-heartbeat: %s failed", op, exc_info=True)
                    succeeded = False
        return succeeded
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


# Live operator-note injection: poll the task for new comments and steer them in
# OUT-OF-BAND, so a user can talk to a running task without block → comment → unblock.
# Watermarked per task (seeded on first poll: that history is already in the context).
_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0
_comment_poll_last_attempt: float = 0.0
_comment_watermark: dict[str, int] = {}


def inject_new_comments_from_env(agent: Any) -> bool:
    """Steer new operator comments on the worker's task into ``agent``; True iff a
    steer was injected; never raises. Own comments (``_persisted_identity``) are skipped."""
    global _comment_poll_last_attempt
    # Operator notes address the dispatcher-owned worker; a delegate_task child sharing
    # this process must neither receive them nor advance the shared watermark (#112817).
    tid = os.environ.get("HERMES_KANBAN_TASK") if _is_dispatcher_owned_worker() else None
    now = time.monotonic()
    if (not tid or agent is None or not hasattr(agent, "steer")
            or (now - _comment_poll_last_attempt) < _COMMENT_POLL_MIN_INTERVAL_SECONDS):
        return False
    _comment_poll_last_attempt = now
    seen = _comment_watermark.get(tid)
    try:
        with _board(None, quiet_close=True) as (kb, conn):
            rows = kb.list_comments_after(conn, tid, after_id=seen or 0)
    except Exception:
        logger.debug("comment-inject: bridge failed", exc_info=True)
        return False
    if seen is None:
        _comment_watermark[tid] = max((c.id for c in rows), default=0)
    if seen is None or not rows:
        return False
    # Advance past everything read (including our own notes) so nothing is re-injected.
    _comment_watermark[tid] = max(c.id for c in rows)
    # Same resolution the write side used, so a worker skips its OWN comments even
    # when the dispatcher did not pin HERMES_PROFILE (echoed notes would otherwise
    # re-enter the live turn as fake operator steering).
    own = _persisted_identity()
    fresh = [c for c in rows if (c.author or "").strip() != own and (c.body or "").strip()]
    if not fresh:
        return False
    lines = [f"- {c.author or 'operator'}: {c.body.strip()}" for c in fresh]
    note = ("New note" + ("s" if len(fresh) > 1 else "")
            + " on your kanban task from the operator (delivered mid-run). "
            + "Take it into account for the work you're doing right now:\n" + "\n".join(lines))
    try:
        return bool(agent.steer(note))
    except Exception:
        logger.debug("comment-inject: steer failed", exc_info=True)
        return False


# --- Handlers ---

def _owner_board_routes(kb, conn, task, board=None):
    """Expose supported control paths without changing session authority."""
    from hermes_cli.board_owner_manage import require_owner, member_board, _validate_member_connection
    try:
        member = require_owner()
    except PermissionError:
        member = None
    from hermes_cli.board_owner_manage import administrator_board
    administrative = administrator_board(member, board) if member is not None else None
    if administrative:
        _validate_member_connection(conn, {'board': administrative})
        member = None
    if member is None and not administrative and not _check_kanban_orchestrator_mode():
        return None
    if member is not None:
        member_board(member, board)
        _validate_member_connection(conn, member)
    import hashlib
    import shlex

    prefix = ["hermes", "kanban", "--board", board or kb.get_current_board()]
    command = lambda *args: shlex.join(prefix + [str(x) for x in args])
    fields = {k: getattr(task, k) for k in ("id", "title", "body", "assignee", "status", "tenant")}
    snapshot = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    stop = conn.execute(
        "SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN "
        "('blocked','block_loop_detected','triaged','escalated') ORDER BY id DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    latest_comment = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_comments WHERE task_id=?", (task.id,)).fetchone()[0]
    waiting = []
    for pid in kb.parent_ids(conn, task.id):
        parent = kb.get_task(conn, pid)
        if not kb._task_row_satisfies_dependency(conn, pid):
            waiting.append({"task_id": pid, "title": parent.title if parent else None,
                            "status": parent.status if parent else "missing",
                            "owner": parent.assignee if parent else None})
    routes = {
        "show": command("show", task.id, "--json"),
        "archive": command("archive", task.id, "--reason", "DISPOSITION_AND_EVIDENCE"),
        "restore": command("restore", task.id, "--reason", "REASON"),
    }
    if task.status == "triage" and stop:
        routes["resume_dry_run"] = command("resume-triage", task.id, "--stop-event", stop["id"],
            "--auth-comment", "AUTHORIZATION_COMMENT_ID", "--latest-comment", latest_comment,
            "--task-sha", snapshot, "--reason", "RESOLUTION_EVIDENCE", "--dry-run")
    native = None
    try:
        from hermes_cli.board_owner_manage import require_owner, describe
        require_owner()
        native = dict(tool="kanban_manage", **describe(conn, task.id))
    except PermissionError:
        pass
    return {"native": native, "waiting_on": waiting, "latest_stop_event_id": stop["id"] if stop else None,
            "latest_comment_id": latest_comment, "resume_snapshot_sha256": snapshot,
            "command_templates": routes,
            "guidance": "For an authorized request to work on or resume this card, use kanban_manage(action='work', task_id=..., reason='the current instruction'). The controller resolves the current state and recovery bookkeeping. No copied hashes, special approval wording, or exhaustive plan is needed for ordinary work. Use the existing worker and normal tools; an optional plan is only for a genuinely changed brief, worker, requirements or budget. Preserve the original request, real dependencies, private boundaries, action restrictions and review findings. The receipt distinguishes started/queued work from a specific remaining wait. Verify the actual worker and result. Do not repeatedly restart an unchanged failed worker run. A recovery attempt limits worker starts, not individual tool calls. Within the same run and existing budget, correct routine tool errors or try equivalent already-authorized tools and sources. Do not add a stop-after-first-error or one-shot capture restriction unless the user explicitly requires it. Preserve explicit user stops, action restrictions and privacy. For a genuine wait, use kanban_manage(action='block', task_id=..., kind='needs_input'|'capability'|'transient', reason='what is missing and the next step'); this works from triage and starts no worker. Other precise cleanup actions remain available: archive obsolete work reversibly, restore paused, remove a specifically obsolete dependency, recover a held review or repair its exact artifact reference."}


def _check_board_owner_mode():
    if not _check_kanban_orchestrator_mode():
        return False
    try:
        from hermes_cli.board_owner_manage import require_owner
        require_owner()
        return True
    except PermissionError:
        return False


def _invoke_board_controller(args):
    import subprocess
    from pathlib import Path
    from gateway.session_context import get_session_env, get_bound_session_env
    from hermes_constants import get_hermes_home
    from hermes_cli.board_owner_manage import require_owner, ROOT
    member = require_owner()
    if member is not None:
        from gateway.app_business_authority import protected
        from gateway.board_control_transport import issue
        executable = ROOT / 'scripts' / 'hermes-kanban-guarded'
        protected(executable, ROOT)
        session = (get_bound_session_env('HERMES_SESSION_ID')
                   or get_bound_session_env('HERMES_SESSION_KEY')
                   or get_bound_session_env('HERMES_SESSION_MESSAGE_ID'))
        request = issue(args, member, session)
        # No member-controlled DB, profile HOME, PYTHONPATH or actor env crosses.
        env = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'HOME':str(ROOT.parent),
               'HERMES_HOME':str(ROOT), 'PYTHONIOENCODING':'utf-8',
               'PYTHONDONTWRITEBYTECODE':'1'}
    else:
        executable = Path(get_hermes_home()) / 'scripts' / 'hermes-kanban-guarded'
        request = args
        env = os.environ.copy()
        for name in ('HERMES_SESSION_SOURCE', 'HERMES_SESSION_PROFILE'):
            env[name] = get_session_env(name)
    if not executable.is_file():
        raise ValueError('Installed guarded board controller is unavailable')
    result = subprocess.run([str(executable), '--owner-tool-json'], input=json.dumps(request),
                            text=True, capture_output=True, timeout=30, env=env)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ValueError('Board controller did not return a receipt; inspect the task before retrying')
    return data


def _handle_manage(args: dict, **kw) -> str:
    try:
        from hermes_cli.board_owner_manage import require_owner, member_board
        from gateway.josh_board_authority import admitted_board
        member = require_owner()
        from hermes_cli.board_owner_manage import administrator_board, requested_board
        if member is None and not _profile_has_kanban_toolset():
            raise PermissionError('The owner profile must enable its existing kanban toolset')
        board = requested_board(member, args.get('board')) if member is not None else admitted_board(args.get('board'))
        administrative = member is not None and board != member['board']
        payload = dict(args, board=board)
        action = args.get('action')
        if action == 'follow_up':
            if set(args) - {'action','task_id','reason','board'} or not args.get('reason'):
                raise ValueError('follow_up needs only task_id and the existing request; routes are never model arguments')
            with _board(board) as (kb, conn):
                from gateway.owner_attention_binding import bind_current
                return json.dumps(bind_current(conn,args.get('task_id'),action=action))
        receipt = _invoke_board_controller(payload)
        if not administrative and action in {'work','block'} and not args.get('dry_run') and receipt.get('ok') is not False and not receipt.get('error'):
            try:
                with _board(board) as (kb, conn):
                    from gateway.owner_attention_binding import bind_current
                    bound = bind_current(conn,args.get('task_id'),action=action)
                    receipt.update({key:value for key,value in bound.items() if key.startswith('follow_up_')})
            except Exception:
                # The work action already succeeded: never imply it should be
                # repeated because a separate source binding was unavailable.
                receipt.update(follow_up_bound=False,follow_up_error='No verified current conversation was attached; use follow_up from the owner conversation.')
        if administrative:
            receipt.update(follow_up_bound=False, original_owner_preserved=True)
        return json.dumps(receipt)
    except Exception as exc:
        return tool_error(f'kanban_manage: {exc}')


@_kanban_handler("kanban_show")
def _handle_show(args: dict, **kw) -> str:
    """Full task state: row, parents, children, comments, runs, last 50 events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        # No dispatcher task in scope and no explicit id: the caller asked "what
        # should I be looking at". A chat profile cannot set HERMES_KANBAN_TASK,
        # so an error naming the env var is dead end (#91431) — answer instead.
        return json.dumps({
            "current_task": None,
            "hint": (
                "No dispatcher-assigned task in this session, so there is no "
                "task to show by default. Call kanban_list to see task ids on "
                "the board, then kanban_show(task_id=<id>) for full state."),
        })
    board = args.get("board")
    from gateway.member_worker_policy import authenticated_private_policy
    private_policy = authenticated_private_policy()
    with _board(board) as (kb, conn):
        task = _existing_task(kb, conn, tid)
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
        runs = kb.list_runs(conn, tid)
        parents = kb.parent_ids(conn, tid)
        children = kb.child_ids(conn, tid)
        if private_policy is not None:
            readable = {private_policy["task"], private_policy.get("preparation_for")}
            parents = [value for value in parents if value in readable]
            children = [value for value in children if value in readable]
        # A genuine system-generated readiness repair has a strict request
        # ceiling. Keep the authoritative task and owner contracts, but do
        # not replay an ever-growing audit trail plus a second formatted
        # copy of the same contract on every orientation call. A fence or
        # request ceiling alone is insufficient; bind this to the durable
        # preparation event and dependency edge. Other prepared workers
        # use their own current plan and active run binding below.
        from hermes_cli.kanban_completion import request_token_limit
        bounded_worker = False
        readiness_binding = None
        worker_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
        if request_token_limit() is not None and worker_id:
            worker = kb.get_task(conn, worker_id)
            if worker is not None and worker.created_by == "worker-readiness":
                binding_row = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? "
                    "AND kind='readiness_preparation_for' ORDER BY id DESC LIMIT 1",
                    (worker_id,),
                ).fetchone()
                if binding_row is not None:
                    try:
                        readiness_binding = json.loads(binding_row[0])
                    except (TypeError, ValueError):
                        readiness_binding = None
                original_id = (
                    readiness_binding.get("task_id")
                    if isinstance(readiness_binding, dict) else None
                )
                if isinstance(original_id, str) and tid in {worker_id, original_id}:
                    bounded_worker = conn.execute(
                        "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=? "
                        "AND edge_role='dependency'",
                        (worker_id, original_id),
                    ).fetchone() is not None
        # Current state appears once. Only the active task's bound worker
        # receives the smaller default; explicit pages remain available.
        from hermes_cli.owner_work_preparation import inspect as inspect_preparation
        preparation = inspect_preparation(conn, tid)
        worker_run = _worker_run_id(tid)
        prepared_worker = bool(
            preparation.get("current")
            and worker_id == tid and _is_dispatcher_owned_worker()
            and task.status == "running" and worker_run is not None
            and worker_run == task.current_run_id
            and task.assignee == os.environ.get("HERMES_PROFILE")
            and any(r.id == worker_run and r.profile == task.assignee
                    and r.status == "running" and r.ended_at is None for r in runs)
        )
        history_limit = args.get("history_limit", 3 if prepared_worker else 8)
        before_id = args.get("comments_before_id")
        events_before_id = args.get("events_before_id")
        include_event_payloads = args.get("include_event_payloads", False)
        if type(history_limit) is not int or not 1 <= history_limit <= 20:
            raise ValueError("history_limit must be an integer from 1 to 20")
        if before_id is not None and (type(before_id) is not int or before_id < 1):
            raise ValueError("comments_before_id must be a positive comment id")
        if events_before_id is not None and (type(events_before_id) is not int or events_before_id < 1):
            raise ValueError("events_before_id must be a positive event id")
        if type(include_event_payloads) is not bool:
            raise ValueError("include_event_payloads must be a boolean")
        eligible_comments = [c for c in comments if before_id is None or c.id < before_id]
        shown_comments = eligible_comments[-min(history_limit, 3):] if bounded_worker else eligible_comments[-history_limit:]
        eligible_events = [e for e in events if events_before_id is None or e.id < events_before_id]
        shown_events = eligible_events[-history_limit:]
        shown_runs = runs[-2:]

        def _event_dict(event):
            result = {"id": event.id, "kind": event.kind, "payload": event.payload,
                      "created_at": event.created_at, "run_id": event.run_id}
            # The current plan is already present above. Keep its audit
            # identity, actor, reason and disposition, without replaying
            # complete prior briefs and readiness inventories by default.
            # Unknown/malformed events and all holds/findings stay intact.
            if event.kind == "owner_work_prepared" and not include_event_payloads:
                payload = event.payload
                if isinstance(payload, dict) and payload.get("version") == 1 and isinstance(payload.get("plan"), dict):
                    omitted = [key for key in ("plan", "readiness") if key in payload]
                    result.update({
                        "payload": {key: value for key, value in payload.items() if key not in omitted},
                        "payload_omitted": omitted,
                        "full_payload_query": {"task_id": tid, **({"board": board} if board else {}),
                                               "events_before_id": event.id + 1,
                                               "history_limit": 1, "include_event_payloads": True},
                    })
            return result

        event_records = [_event_dict(event) for event in shown_events]

        def _run_dict(r):
            return {
                "id": r.id, "profile": r.profile,
                "status": r.status, "outcome": r.outcome,
                "summary": r.summary, "error": r.error,
                "metadata_available": bool(r.metadata),
                "started_at": r.started_at, "ended_at": r.ended_at,
            }

        from hermes_cli.owner_workflow import describe as describe_workflow
        from hermes_cli.owner_work_start import instruction_recovery
        child_execution = None
        if os.environ.get("HERMES_KANBAN_TASK") == tid and _is_dispatcher_owned_worker():
            admission = conn.execute("SELECT worker_admission_contract FROM tasks WHERE id=?", (tid,)).fetchone()
            if admission and admission[0]:
                from hermes_cli.child_execution_receipts import current_task
                child_execution = current_task(conn, tid)
        delivery = None
        if private_policy is not None:
            from gateway.private_action_worker_policy import delivery_instructions
            delivery = delivery_instructions(conn, private_policy, tid)
        return json.dumps({
            **({"delivery_instructions": delivery} if private_policy is not None else {}),
            "child_execution": child_execution,
            "preparation": preparation,
            "instruction_recovery": instruction_recovery(conn, tid),
            "board_administration": _owner_board_routes(kb, conn, task, board),
            "task": {**_fields(task, _TASK_FIELDS),
                     "capability_projection": json.loads(task.capability_projection) if task.capability_projection else None},
            "owner_cell": kb.owner_cell_read_state(conn, tid),
            "owner_workflow": describe_workflow(conn, tid),
            "parents": parents,
            "unsatisfied_parents": [
                {"id": pid, "status": status} for pid, status in kb.unsatisfied_parents(conn, tid)
                if private_policy is None or pid in readable],
            "children": children,
            "comments": [
                {"id": c.id, "author": c.author, "body": c.body,
                 "created_at": c.created_at}
                for c in shown_comments
            ],
            "events": event_records,
            "runs": [_run_dict(r) for r in shown_runs],
            "worker_context": None,
            "history_window": {
                "comments": {"shown": len(shown_comments), "total": len(comments)},
                "events": {"shown": len(shown_events), "total": len(events)},
                "runs": {"shown": len(shown_runs), "total": len(runs)},
                "truncated": any((len(shown_comments) < len(comments),
                                  len(shown_events) < len(events),
                                  len(shown_runs) < len(runs))),
                "next_comments_before_id": shown_comments[0].id if shown_comments and len(eligible_comments) > len(shown_comments) else None,
                "next_events_before_id": shown_events[0].id if shown_events and len(eligible_events) > len(shown_events) else None,
                "omitted_event_payload_ids": [event["id"] for event in event_records if event.get("payload_omitted")],
                "prepared_worker": prepared_worker,
                "full_history": "Use this same show tool with comments_before_id or events_before_id and history_limit for older same-task history. Set include_event_payloads=true to retrieve complete preparation audit payloads; each compact event supplies its exact query. Full run metadata remains in hermes kanban log. Original task, holds and owner review state remain authoritative. Read relevant referenced approvals and findings before recovery; missing from this window does not mean absent.",
                "readiness_binding": readiness_binding,
            },
        })


@_kanban_handler("kanban_list")
def _handle_list(args: dict, **kw) -> str:
    """Task summaries with the same core filters as the CLI."""
    _require_orchestrator_tool("kanban_list")
    include_archived = _parse_bool_arg(args, "include_archived")
    limit = args.get("limit")
    try:
        limit = KANBAN_LIST_DEFAULT_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    _check(limit >= 1, "limit must be >= 1")
    _check(limit <= KANBAN_LIST_MAX_LIMIT, f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    with _board(args.get("board")) as (kb, conn):
        # Match CLI list: dependencies cleared since the last dispatcher tick
        # should be visible to orchestrators immediately.
        promoted = kb.recompute_ready(conn)
        # One extra row lets the output report truncation without dumping the board.
        rows = kb.list_tasks(
            conn, assignee=args.get("assignee"), status=args.get("status"),
            tenant=args.get("tenant"), include_archived=include_archived, limit=limit + 1)
        truncated = len(rows) > limit
        tasks = rows[:limit]
        return json.dumps({
            "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
            "count": len(tasks), "limit": limit, "truncated": truncated,
            "next_limit": (min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                           if truncated and limit < KANBAN_LIST_MAX_LIMIT else None),
            "promoted": promoted})


@_kanban_handler("kanban_complete")
def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _worker_guard("kanban_complete", args)
    _retry_change_note_gate(tid, args.get("board"), "kanban_complete", args)
    summary = _redact_opt(args.get("summary"))
    result = _redact_opt(args.get("result"))
    metadata = args.get("metadata")
    if isinstance(metadata, dict):
        # Keep the unredacted dict if the redacted JSON cannot be re-parsed.
        metadata = _redact_metadata(metadata) or metadata
    created_cards = _coerce_str_list(
        args.get("created_cards"), "created_cards", "task ids", strip=True)
    artifacts = _coerce_str_list(args.get("artifacts"), "artifacts", "file paths", strip=True)
    if artifacts:
        metadata = _merge_artifacts(metadata, artifacts)
    _check(summary or result, "provide at least one of: summary (preferred), result")
    _require_dict_metadata(metadata)
    metadata, auth_error = _normalize_authorization_metadata(metadata)
    _check(auth_error is None, auth_error)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    with _board(args.get("board")) as (kb, conn):
        # Goal-mode pre-completion judge gate (Issue #38367). Prevent workers from bypassing the auxiliary
        # judge by calling kanban_complete before acceptance criteria are met. Only enforce when a judge is
        # actually reachable — see _goal_judge_available for why an unavailable judge fails open.
        task = kb.get_task(conn, tid)
        _goal_gate("kanban_complete", task, tid, (summary or result or "").strip())
        # Explicit review-worthy documents cross a human trust boundary.
        # Stage them before the existing completion transaction so a failed
        # validation/copy leaves the upstream lifecycle untouched.
        from tools.kanban_artifact_staging import (
            review_worthy_requested,
            stage_review_worthy_artifacts,
        )

        if review_worthy_requested(metadata):
            assert isinstance(metadata, dict)
            declared = metadata.get("artifacts")
            paths = (
                [str(path).strip() for path in declared if str(path).strip()]
                if isinstance(declared, (list, tuple))
                else []
            )
            try:
                metadata["artifacts"] = stage_review_worthy_artifacts(
                    kb, task_id=tid, board=args.get("board"), artifacts=paths,
                )
            except ValueError as artifact_err:
                return tool_error(
                    f"kanban_complete refused the review-worthy artifacts: "
                    f"{artifact_err}. Your task is still in-flight."
                )

        try:
            from agent.review_authority import decision_token
            run_id = _worker_run_id(tid)
            ok = kb.complete_task(
                conn, tid, result=result, summary=summary, metadata=metadata,
                created_cards=created_cards, expected_run_id=run_id,
                review_decision_capability=decision_token(tid, run_id))
        except kb.ArtifactPreservationError as artifact_err:
            # Structured rejection — surface the phantom ids so the worker can retry with a corrected list
            # or drop the field. Audit event already landed in the DB. The task itself was NOT mutated (the
            # gate runs before the write txn), so the worker can simply call kanban_complete again. Spell
            # that out — without it the model often interprets a tool_error as a terminal failure and either
            # blocks or crashes the run instead of retrying. See #22923.
            return tool_error(
                f"kanban_complete could not preserve the declared artifacts: {artifact_err}. "
                f"Your task is still in-flight and its scratch workspace was kept. Fix the "
                f"artifact path or storage error, then retry kanban_complete with the same "
                f"handoff.")
        except kb.LiveClaimError as claim_err:
            # Env-less caller (orchestrator, another session) on a card a dispatcher
            # worker is executing: refusing here is what keeps that worker's run open.
            return tool_error(
                f"kanban_complete refused: {claim_err}. Nothing changed. Wait for the worker "
                f"to finish, or an operator can run `hermes kanban complete --force {tid}`.")
        except kb.HallucinatedCardsError as hall_err:
            # The gate runs before the write txn, so the task was NOT mutated;
            # say so explicitly or the model treats the error as terminal and
            # blocks/crashes instead of retrying. Audit event already landed.
            return tool_error(
                f"kanban_complete blocked: the following created_cards do not exist or were not "
                f"created by this worker: {', '.join(hall_err.phantom)}. Your task is still "
                f"in-flight (no state change). Retry kanban_complete with the same "
                f"summary/metadata and either drop these ids from created_cards, or pass "
                f"created_cards=[] to skip the card-claim check entirely.")
        except kb.EmptyCompletionError as empty_err:
            # Same shape as the card gate: nothing was mutated, the audit event
            # already landed; the worker retries with evidence instead of stalling.
            return tool_error(
                f"kanban_complete blocked: {empty_err}. Your task is still in-flight (no state "
                f"change). Retry kanban_complete with a non-empty summary or result describing "
                f"what was done.")
        task = kb.get_task(conn, tid)
        if not ok:
            # complete_task reports every refusal as bare False; a reopened or
            # never-finished parent is the actionable one. Name the blockers so
            # the worker/operator completes the parents instead of re-running.
            blockers = kb.unsatisfied_parents(conn, tid)
            if blockers:
                detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                raise _Reject(
                    f"could not complete {tid}: unsatisfied parent dependencies: "
                    f"{detail}; complete the parents first (done or archived)")
            _check(False, (task.last_failure_error if task else None) or
                   f"could not complete {tid} (unknown id, stale run, or already terminal)")
        run = kb.latest_run(conn, tid)
        # Artifact staging is atomic with the completion write, so a worker that
        # read `kanban_attachments` before completing saw an empty list and has
        # no way to observe what its completion just registered (#117360).
        # Report the card's durable attachment set in the result.
        from hermes_cli.owner_workflow import describe as describe_workflow
        return _ok(owner_workflow=describe_workflow(conn, tid),
                   task_id=tid, run_id=run.id if run else None,
                   attachments=[
                       _fields(a, _ATTACHMENT_FIELDS)
                       for a in kb.list_attachments(conn, tid)])


def _record_needs(conn, tid, args, *, reason, kind, source):
    """Best-effort: store the plain 'Needs from Chris' line for this card."""
    try:
        from hermes_cli import kanban_needs as _needs
        explicit = args.get("needs_from_chris")
        if explicit is not None:
            explicit = redact_sensitive_text(str(explicit), force=True)
        return _needs.record(conn, tid, explicit=explicit, reason=reason,
                             kind=kind, source=source, link=args.get("needs_link"))
    except Exception:
        logger.debug("kanban needs_from_chris record failed", exc_info=True)
        return None


def _retry_change_note_gate(tid, board, tool, args=None):
    """Repeated-failure retry must record a change note first (t_64ae1aaf).
    Applies only to the dispatched worker acting on its own task. A genuine
    owner-input / dependency block stays possible (t_b338f418 E)."""
    if tid != os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if tool == "kanban_block" and (args or {}).get("kind") in ("needs_input", "dependency"):
        return None
    try:
        from hermes_cli.retry_course_correction import gate_error
        kb, conn = _connect(board=board)
        try:
            msg = gate_error(conn, tid, tool)
        finally:
            conn.close()
    except Exception:
        return None
    if msg:
        raise _Reject(msg)
    return None


@_kanban_handler("kanban_block")
def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _worker_guard("kanban_block", args)
    _retry_change_note_gate(tid, args.get("board"), "kanban_block", args)
    reason = _redact(
        _require_text(args, "reason", "reason is required — explain what input you need"))
    kind = args.get("kind")
    with _board(args.get("board")) as (kb, conn):
        _check(kind is None or kind in kb.VALID_BLOCK_KINDS,
               f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)")
        # The goal loop treats ANY blocked status as terminal, so kanban_block
        # would be an escape hatch around the completion judge: goal_mode tasks
        # may only block on genuine external blockers.
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete judge gate in #38367).
        # kanban_block is a second exit path out of the goal loop — run_kanban_goal_loop() treats ANY
        # `blocked` status as terminal, identically to `done`, regardless of kind. Without this, a worker
        # that learns kanban_complete is gated can just call kanban_block(reason="anything") to escape the
        # loop instead. Restrict goal_mode tasks to the kinds that represent a genuine external blocker the
        # worker cannot resolve itself; `capability` and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        reason = kb.authorized_block_reason(
            conn, tid, kind=kind, reason=reason,
            authorization_checked=args.get("authorization_checked"),
            new_decision=args.get("new_decision"),
        )
        _check(not (task and task.goal_mode and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS),
               f"goal_mode tasks can only block with kind in "
               f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). If the task is actually "
               f"finished or cannot proceed for another reason, call kanban_complete instead — "
               f"the completion judge will evaluate it.")
        ok = kb.block_task(conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not block {tid} (unknown id or not in running/ready)")
        _record_needs(conn, tid, args, reason=reason, kind=kind, source="block")
        landed_kind = kb.get_task(conn, tid).block_kind
        extra: dict = {"block_kind": landed_kind}
        if kind == "dependency" and landed_kind != kind:
            # block_task re-kinds a dependency wait that no open parent can satisfy.
            extra["requested_kind"] = kind
            extra["note"] = (
                "kind='dependency' only waits on an incomplete parent; no parent is open, "
                "so this was recorded as needs_input (sticky until a human unblocks) "
                "instead of parking in todo where the dispatcher would respawn it."
            )
        return _ok_landed(kb, conn, tid, "blocked", **extra)


@_kanban_handler("kanban_schedule")
def _handle_schedule(args: dict, **kw) -> str:
    """Park the current task until an orchestrator re-gates it."""
    tid = _worker_guard("kanban_schedule", args)
    raw_reason = args.get("reason")
    _check(raw_reason is None or isinstance(raw_reason, str), "reason must be a string")
    reason = _redact(raw_reason.strip()) if raw_reason and raw_reason.strip() else None
    with _board(args.get("board")) as (kb, conn):
        # The goal loop treats ``scheduled`` as terminal like ``blocked``, so
        # parking would bypass the completion judge (see kanban_block, #38696).
        task = kb.get_task(conn, tid)
        _check(not (task and task.goal_mode),
               "goal_mode tasks cannot be scheduled: use kanban_block with kind "
               f"in {sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} for a genuine external "
               "blocker, or kanban_complete so the completion judge can evaluate it.")
        ok = kb.schedule_task(
            conn, tid, reason=reason, expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not schedule {tid} (unknown id or not in todo/ready/running/blocked)")
        return _ok_landed(kb, conn, tid, "scheduled", reason=reason)


def _review_handoff_input_error(conn, tid, reviewer, metadata):
    """Explain correctable payload errors without granting review authority."""
    goal = conn.execute(
        "SELECT router_mode, required_reviewer_profile FROM owner_cell_goals WHERE task_id=?",
        (tid,),
    ).fetchone()
    if goal is None or goal["router_mode"] != "reviewed":
        return None
    required = goal["required_reviewer_profile"]
    artifact = (metadata or {}).get("owner_cell_artifact")
    from hermes_cli import kanban_db as kb
    reviewer_matches = reviewer and kb._canonical_assignee(reviewer) == required
    artifact_valid = isinstance(artifact, dict) and all(
        isinstance(artifact.get(key), str) and artifact[key].strip()
        for key in ("ref", "digest")
    )
    if reviewer_matches and artifact_valid:
        return None
    return tool_error(
        "Review handoff payload needs correction; this is not a missing tool or dependency. "
        "Read kanban_show for the task contract, use its required reviewer, and supply "
        "metadata.owner_cell_artifact with ref and digest for the exact verified artifact. "
        "Correct these arguments and retry on this same task. Do not block solely on this "
        "validation error, invent approval, or present a plan/brief as the finished deliverable.",
        code="review_handoff_input_invalid",
        recoverable=True,
        required_reviewer_profile=required,
        required_fields=["reviewer", "metadata.owner_cell_artifact.ref",
                         "metadata.owner_cell_artifact.digest"],
    )


@_kanban_handler("kanban_handoff")
def _handle_owner_handoff(args: dict, **kw) -> str:
    tid = _worker_guard("kanban_handoff", args)
    from hermes_cli.owner_workflow import handoff
    from hermes_cli.kanban_db import WorkerAdmissionError
    try:
        with _board(args.get("board")) as (kb, conn):
            if args.get("action", "maker") in {"maker", "owner"}:
                from hermes_cli.owner_workflow import continue_direct_producer
                direct = continue_direct_producer(conn, tid, expected_run_id=_worker_run_id(tid),
                                                  profile=os.environ.get("HERMES_PROFILE"))
                if direct:
                    return _ok(**direct)
            if args.get("action") == "owner":
                from hermes_cli.owner_workflow import start_owner_completion
                from agent.review_authority import decision_token
                run_id = _worker_run_id(tid)
                return _ok(**start_owner_completion(conn, tid, expected_run_id=run_id,
                    review_decision_capability=decision_token(tid, run_id),
                    summary=redact_sensitive_text(str(args.get("summary") or ""), force=True),
                    acceptance_criteria=json.loads(redact_sensitive_text(
                        json.dumps(args.get("acceptance_criteria")), force=True))))
            if args.get("action") == "diagnosed_rework":
                from hermes_cli.owner_cell import authorize_diagnosed_rework
                from agent.review_authority import decision_token
                run_id = _worker_run_id(tid)
                return _ok(**authorize_diagnosed_rework(conn, tid, expected_run_id=run_id,
                    review_decision_capability=decision_token(tid, run_id),
                    brief_ref=args.get('brief_ref'), brief_digest=args.get('brief_digest'),
                    summary=redact_sensitive_text(str(args.get('summary') or ''), force=True),
                    acceptance_criteria=json.loads(redact_sensitive_text(
                        json.dumps(args.get('acceptance_criteria')), force=True))))
            if args.get("action") == "advisory_waiver":
                from hermes_cli.owner_workflow import record_advisory_waiver
                from agent.review_authority import decision_token
                run_id = _worker_run_id(tid)
                return _ok(**record_advisory_waiver(conn, tid, expected_run_id=run_id,
                    review_decision_capability=decision_token(tid, run_id), profile=args.get("profile"),
                    decision=args.get("waiver_decision"), confirm_manual=args.get("confirm_manual", False),
                    authorization_ref=args.get("authorization_ref"), authorization_text=args.get("authorization_text"),
                    summary=args.get("summary")))
            if args.get("action") == "advisory":
                from hermes_cli.owner_workflow import request_advisory
                from agent.review_authority import decision_token
                run_id = _worker_run_id(tid)
                return _ok(**request_advisory(conn, tid, expected_run_id=run_id,
                    review_decision_capability=decision_token(tid, run_id),
                    profile=args.get("profile"), packet=args.get("packet"), summary=args.get("summary"),
                    confirm_manual=args.get("confirm_manual", False), board=args.get("board"),
                    artifact_ref=args.get("brief_ref"), artifact_digest=args.get("brief_digest")))
            values = {k: args.get(k) for k in ("brief_ref", "brief_digest", "requirements", "acceptance_criteria", "summary", "repair")}
            values = json.loads(redact_sensitive_text(json.dumps(values), force=True))
            result = handoff(conn, tid, expected_run_id=_worker_run_id(tid), board=args.get("board"), **values)
            return _ok(**result)
    except WorkerAdmissionError as exc:
        return tool_error("Maker readiness failed; the owner still holds this task. Correct the stated requirements or approved setup, then retry. If network/account/path access is owner-only, use the owner's existing authorized tools to retrieve and stage non-secret same-scope inputs inside this workspace, then hand off offline processing. Do not copy credentials or broaden the maker's permissions.",
                          code="maker_not_ready", issues=(exc.readiness or {}).get("issues", []))
    except (ValueError, OSError) as exc:
        return tool_error(str(exc))


@_kanban_handler("kanban_request_review")
def _handle_request_review(args: dict, **kw) -> str:
    """Move implementation into the first-class review phase."""
    tid = _worker_guard("kanban_request_review", args)
    _retry_change_note_gate(tid, args.get("board"), "kanban_request_review", args)
    summary = _redact(_require_text(
        args, "summary", "summary is required — describe what was implemented and how it "
        "was verified so the reviewer has context"))
    metadata = args.get("metadata")
    _require_dict_metadata(metadata)
    if metadata is not None:
        metadata = _redact_metadata(metadata)
        _check(metadata is not None, "metadata could not be safely serialized")
    artifacts = _coerce_str_list(args.get("artifacts"), "artifacts", "file paths", strip=True)
    if artifacts:
        metadata = _merge_artifacts(metadata, artifacts)
    metadata, auth_error = _normalize_authorization_metadata(metadata)
    _check(auth_error is None, auth_error)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    # Reviewer is model-supplied free text stored durably on the event payload.
    reviewer = _redact_opt(args.get("reviewer") or None)
    with _board(args.get("board")) as (kb, conn):
        _goal_gate("kanban_request_review", kb.get_task(conn, tid), tid, summary)
        try:
            # [GUS] Owner-cell payload correction first (actionable, recoverable code).
            input_error = _review_handoff_input_error(conn, tid, reviewer, metadata)
            if input_error is not None:
                return input_error
            if reviewer:
                from hermes_cli.profiles import list_profile_names, profile_exists

                # A non-profile reviewer would park the card in `review` on an assignee
                # the dispatcher can never spawn (#106163).
                _check(profile_exists(reviewer),
                       f"reviewer profile {reviewer!r} is not installed. "
                       f"Installed profiles: {', '.join(list_profile_names())}")
            with kb.write_txn(conn):
                admission = conn.execute(
                    "SELECT worker_admission_contract FROM tasks WHERE id=?", (tid,),
                ).fetchone()
                if admission and admission[0] and _worker_run_id(tid) is not None:
                    from hermes_cli.child_execution_receipts import capture
                    capture(conn, tid, server_session_id=kw.get("session_id"))
                ok, fail_reason = kb.request_review(
                    conn, tid, summary=summary, metadata=metadata, reviewer=reviewer,
                    expected_run_id=_worker_run_id(tid), with_reason=True, _allow_nested=True)
                _check(ok, f"could not request review: {fail_reason or 'transition refused'}")
        except kb.ArtifactPreservationError as artifact_err:
            # Same contract as kanban_complete (#22923): the transition rolled
            # back, the task is untouched and retryable — say so explicitly or
            # the model treats the tool_error as terminal.
            return tool_error(
                f"kanban_request_review could not preserve the declared artifacts: {artifact_err}. "
                f"Your task is still in-flight (no state change) and its scratch workspace was "
                f"kept. Fix the artifact path or storage error, then retry "
                f"kanban_request_review with the same handoff.")
        _check(ok, f"could not request review for {tid}: "
                   f"{fail_reason or 'unknown id or not in running/ready'}")
        _record_needs(conn, tid, args, reason=summary, kind=None, source="review")
        return _ok_landed(kb, conn, tid, "review")


@_kanban_handler("kanban_rebind_review_artifact")
def _handle_rebind_review_artifact(args: dict, **kw) -> str:
    """Evidence-only reviewer handoff; authority never comes from tool arguments."""
    tid = _worker_guard("kanban_rebind_review_artifact", args)
    try:
        with _board(args.get("board")) as (kb, conn):
            from agent.review_authority import decision_token
            run_id = _worker_run_id(tid)
            kb.rebind_review_artifact_task(
                conn, tid,
                **{name: args.get(name) for name in (
                    "state_version", "expected_artifact_ref", "expected_artifact_digest",
                    "artifact_ref", "artifact_digest", "reason",
                )},
                expected_run_id=run_id,
                review_decision_capability=decision_token(tid, run_id),
            )
            return _ok(task_id=tid, status="review", fresh_review_required=True)
    except (ValueError, PermissionError) as exc:
        return tool_error(f"kanban_rebind_review_artifact: {exc}")
    except Exception:
        logger.exception("kanban_rebind_review_artifact failed")
        return tool_error("kanban_rebind_review_artifact failed; state unchanged")


@_kanban_handler("kanban_request_changes")
def _handle_request_changes(args: dict, **kw) -> str:
    """Return a reviewer-owned running task to its implementer."""
    tid = _worker_guard("kanban_request_changes", args)
    reason = _redact(
        _require_text(args, "reason", "reason is required — describe the changes needed"))
    with _board(args.get("board")) as (kb, conn):
        from agent.review_authority import decision_token
        run_id = _worker_run_id(tid)
        ok, detail = kb.request_changes(
            conn, tid, reason=reason, expected_run_id=run_id,
            review_decision_capability=decision_token(tid, run_id))
        _check(ok, f"could not request changes for {tid}: {detail or 'invalid review state'}")
        landed = kb.get_task(conn, tid)
        return _ok_landed(
            kb, conn, tid, "ready", implementer=detail,
            continue_work=bool(landed and landed.status == "running"),
            next_action=("Owner takeover: finish this same run. Read kanban_show.owner_workflow; "
                         "provide the finished file and criterion evidence, not another maker handoff."
                         if landed and landed.status == "running" else None))


@_kanban_handler("kanban_heartbeat")
def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal liveness: extend the claim TTL AND record a heartbeat event.
    Without the claim half, a worker blocked in one long tool call would still
    be reclaimed by ``release_stale_claims``."""
    tid = _worker_guard("kanban_heartbeat", args)
    from hermes_cli import kanban_db_dispatch as kbd
    with _board(args.get("board")) as (kb, conn):
        # The dispatcher pins HERMES_KANBAN_CLAIM_LOCK at spawn; the default
        # claimer covers locally-driven workers that bypassed the dispatcher.
        kb.heartbeat_claim(conn, tid, claimer=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"))
        ok = kbd.heartbeat_worker(
            conn, tid, note=args.get("note"), expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not heartbeat {tid} (unknown id or not running)")
        return _ok(task_id=tid)


@_kanban_handler("kanban_comment")
def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    _reject_delegated_child_mutation("kanban_comment")
    tid = args.get("task_id")
    if not tid:
        # A comment writes to the board, so a bare call must stay an error — but
        # point at kanban_list, not an env var a chat caller cannot set (#91431).
        raise _Reject(
            "task_id is required: comments are per-task and this session has no "
            "dispatcher-assigned task to default to. Discover task ids with "
            "kanban_list, or pass the task id you mean.")
    body = _redact(_require_text(args, "body"))
    # Author comes from the worker's runtime identity (``_persisted_identity``), never
    # caller args: comments are injected into future workers' system prompts, so an
    # args["author"] override could forge a directive from ``hermes-system``.
    # Cross-task commenting stays unrestricted — it is the handoff channel between tasks.
    # Comments are injected into the next worker's system prompt by ``build_worker_context`` as
    # ``**{author}** (timestamp): {body}`` — accepting an ``args["author"]`` override let a worker forge a
    # comment from an authoritative-looking name like ``hermes-system`` and poison the future-worker context
    # with what reads as a system directive. See #19713.
    author = _persisted_identity()
    try:
        from hermes_cli.board_owner_manage import require_owner
        member = require_owner()
        if member is not None:
            author = member['profile']
    except PermissionError:
        pass
    with _board(args.get("board")) as (kb, conn):
        cid = kb.add_comment(conn, tid, author=author, body=str(body))
        if tid == os.environ.get("HERMES_KANBAN_TASK"):
            try:
                from hermes_cli.retry_course_correction import record_change_note
                if record_change_note(conn, tid, str(body), author):
                    return _ok(task_id=tid, comment_id=cid, change_note_recorded=True)
            except Exception:
                logger.debug("retry change note record failed", exc_info=True)
        return _ok(task_id=tid, comment_id=cid)


def _store_attachment(board, tid, filename, data, content_type) -> str:
    """Store via ``kanban_db.store_attachment_bytes`` (shared size cap, per-task
    dir, metadata row) so agent, dashboard, and CLI surfaces stay in lockstep."""
    with _board(board) as (kb, conn):
        att_id = kb.store_attachment_bytes(
            conn, tid, str(filename), data,
            content_type=content_type, uploaded_by="agent", board=board)
        return _ok(task_id=tid, attachment_id=att_id, size=len(data))


@_kanban_handler("kanban_attach")
def _handle_attach(args: dict, **kw) -> str:
    """Attach inline bytes or a file from the admitted task workspace."""
    from gateway.josh_board_authority import admitted_board
    board = admitted_board(args.get("board"))
    tid = _worker_guard("kanban_attach", args)
    if args.get("file_path") is not None:
        _check(args.get("content_base64") is None,
               "file_path and content_base64 are mutually exclusive")
        from tools.task_attachment_read import store_workspace_attachment
        with _board(board) as (kb, conn):
            return _ok(**store_workspace_attachment(
                kb, conn, tid, args["file_path"], args.get("filename"), args.get("content_type")))
    filename = _require_text(args, "filename")
    content_b64 = _require_text(args, "content_base64")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Reject(f"content_base64 is not valid base64: {e}")
    return _store_attachment(board, tid, filename, data, args.get("content_type"))


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) capped at ``max_bytes`` -> ``(data, content_type)``.
    Every hop is SSRF-checked (redirects followed manually) so a model-controlled URL, or a
    public host 302ing, cannot reach loopback/private/cloud-metadata ranges. ``ValueError``
    for bad scheme, blocked target, too many redirects, or a body over the cap (checked
    while streaming, so nothing oversize is buffered)."""
    from urllib.parse import urljoin, urlparse
    import httpx
    from tools.url_safety import is_safe_url
    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}")
        chunks: list[bytes] = []
        total = 0
        with httpx.stream("GET", current_url, headers={"User-Agent": "hermes-kanban/attach"},
                          timeout=30, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


@_kanban_handler("kanban_attach_url")
def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from an http(s) URL (shared size cap)."""
    from gateway.josh_board_authority import admitted_board
    board = admitted_board(args.get("board"))
    from hermes_cli import kanban_db as kb
    tid = _worker_guard("kanban_attach_url", args)
    url = str(_require_text(args, "url")).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip() or "download"
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    return _store_attachment(
        board, tid, filename, data, args.get("content_type") or fetched_ct)


@_kanban_handler("kanban_attachments")
def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _require_task_id(args)
    with _board(args.get("board")) as (kb, conn):
        _existing_task(kb, conn, tid)
        if args.get("attachment_id") is not None:
            from tools.task_attachment_read import read_attachment
            return json.dumps(read_attachment(kb, conn, tid, args["attachment_id"]))
        return json.dumps({
            "ok": True, "task_id": tid,
            "attachments": [
                _fields(a, _ATTACHMENT_FIELDS) for a in kb.list_attachments(conn, tid)]})


def _persisted_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return a session id only when it is present in this profile's state.db."""
    if not session_id:
        return None
    try:
        from hermes_state import SessionDB
        from hermes_constants import get_hermes_home

        state = SessionDB(db_path=get_hermes_home() / "state.db", read_only=True)
    except Exception:  # state.db may not exist for a CLI/dashboard invocation
        logger.debug("Could not open state.db to verify Kanban provenance", exc_info=True)
        return None
    try:
        return session_id if state.get_session(session_id) else None
    finally:
        state.close()


@_kanban_handler("kanban_create")
def _handle_create(args: dict, **kw) -> str:
    """Create a (child) task; orchestrator workers use this to fan out."""
    _reject_delegated_child_mutation("kanban_create")
    title = _require_text(args, "title")
    assignee = args.get("assignee")
    _check(assignee, "assignee is required — name the profile that should execute this "
                     "task (the dispatcher will only spawn tasks with an assignee)")
    # t_64ae1aaf: a nonexistent assignee (e.g. legacy "margot") is silently
    # never dispatched; route agent-fixable work to a real profile instead.
    try:
        from hermes_cli.profiles import profile_exists as _pe
        from hermes_constants import get_hermes_home as _ghh
        _root = _ghh()
        _root = _root.parent if _root.parent.name == "profiles" else _root
        _pdir = _root / "profiles"
        _populated = _pdir.is_dir() and any(x.is_dir() for x in _pdir.iterdir())
        _known = _pe(str(assignee)) or not _populated
    except Exception:
        _known = True
    _check(_known, f"assignee {assignee!r} is not an existing profile and would never be "
                   "dispatched; assign an existing agent owner/operator profile instead")
    # Workspace sharing is always explicit: omitted fields mean a fresh scratch workspace
    # even for a dispatcher-spawned creator (reusing the parent's path would let a child
    # mutate review evidence or race its checkout). Project identity is the one safe thing
    # to inherit implicitly (the DB turns it into a fresh per-task worktree).
    workspace_kind, workspace_path = args.get("workspace_kind"), args.get("workspace_path")
    # See #67567. ``project=""`` is an explicit "no project" (no ``or`` collapse, #106342).
    project_id = args["project"] if "project" in args else args.get("project_id")
    project_source_task_id = None
    triage, skills, goal_mode = (
        _parse_bool_arg(args, "triage"), _coerce_str_list(args.get("skills"), "skills", "skill names"),
        _parse_bool_arg(args, "goal_mode"))
    capability_contract = args.get("capability_contract")
    _check(capability_contract is None or isinstance(capability_contract, dict),
           f"capability_contract must be an object, got {type(capability_contract).__name__}")
    body = args.get("body")
    model_override, provider_override = args.get("model"), args.get("provider")
    _check(model_override or not provider_override, "'provider' requires 'model' to be set as well")
    parents = _coerce_str_list(args.get("parents") or [], "parents", "task ids", strip=True)
    with _board(args.get("board")) as (kb, conn):
        from gateway.session_context import get_session_env, get_bound_session_env
        from tools.async_delegation import _current_origin_session_id
        self_tid = (os.environ.get("HERMES_KANBAN_TASK")
                    if _is_dispatcher_owned_worker() else None)
        self_task = kb.get_task(conn, self_tid) if self_tid else None
        # The worker/API runtime may be transient; the owning task's origin is durable.
        # The ambient id is the request-scoped ContextVar binding, not the process-global
        # os.environ: in a multi-session gateway the env holds the LAST agent built, and an
        # id that never reached its ``sessions`` row is provenance pointing at nothing.
        session_id = (_persisted_session_id(args.get("session_id"))
                      or (self_task.session_id if self_task else None)
                      or _persisted_session_id(_current_origin_session_id())
                      or _persisted_session_id(get_bound_session_env("HERMES_SESSION_ID"))
                      or _persisted_session_id(get_session_env("HERMES_SESSION_ID", "")))
        if project_id is None and workspace_kind is None and workspace_path is None:
            if self_task is not None and self_task.project_id:
                project_id, project_source_task_id = self_task.project_id, self_task.id
        auth_contract, auth_error = _authorization_contract_for_child(
            kb, conn, self_task_id=self_tid, parents=parents,
            requested=args.get("authorization_continuity"))
        _check(auth_error is None, auth_error)
        if auth_contract is not None:
            body = kb.append_authorization_continuity_to_body(body, auth_contract)
        from gateway.native_task_routing import compose
        body = compose(conn, title=str(title).strip(), body=body, assignee=str(assignee),
                       parents=parents, session_id=session_id)
        new_tid = kb.create_task(
            conn, title=str(title).strip(), body=body, assignee=str(assignee),
            parents=tuple(parents), tenant=args.get("tenant") or os.environ.get("HERMES_TENANT"),
            priority=_opt_int(args.get("priority"), 0),
            workspace_kind=workspace_kind, workspace_path=workspace_path, project_id=project_id,
            # Board-project inheritance must read the board this call opened, not the
            # session's current board.
            board=args.get("board"),
            project_source_task_id=project_source_task_id, triage=triage,
            creator_task_id=self_tid,
            idempotency_key=args.get("idempotency_key"),
            max_runtime_seconds=_opt_int(args.get("max_runtime_seconds")), skills=skills,
            model_override=model_override, provider_override=provider_override,
            goal_mode=goal_mode, goal_max_turns=_opt_int(args.get("goal_max_turns")),
            completion_contract=args.get("completion_contract"),
            capability_contract=capability_contract,
            initial_status=str(args.get("initial_status") or "running"),
            created_by=_persisted_identity(), session_id=session_id)
        new_task = kb.get_task(conn, new_tid)
        from gateway.native_task_receipts import record_created
        task_receipt_recorded = record_created(conn, new_task)
        landed = _fields(new_task, _CREATED_FIELDS)
        landed.update(capability_status=new_task.capability_status if new_task else None,
                      capability_projection=json.loads(new_task.capability_projection)
                      if new_task and new_task.capability_projection else None)
        wait = [e for e in kb.list_events(conn, new_tid) if e.kind == "dependency_wait"]
        gate = {"gated": True, "gated_by": wait[-1].payload["parent"]} if wait else {"gated": False}
        return _ok(task_id=new_tid, **landed, **gate,
                   subscribed=_maybe_auto_subscribe(conn, new_tid),
                   task_receipt_recorded=task_receipt_recorded)


def _live_tui_session_key(session_key: str, profile: Optional[str]) -> str:
    """Re-resolve a TUI session key at subscribe time: the inherited ``HERMES_SESSION_KEY``
    can name a session already superseded by a compaction fork, and a subscription bound to
    the dead key silently drops every later completion notification (#110068). Maps the key
    to its continuation tip via the session store's lineage walk. Best-effort, fail-open:
    any error (or an unresolvable key) returns *session_key* unchanged."""
    db_path = None
    if profile and profile != "default":
        try:
            from pathlib import Path
            from hermes_cli.profiles import get_profile_dir, profile_exists
            if profile_exists(profile):
                db_path = Path(get_profile_dir(profile)) / "state.db"
        except Exception:
            db_path = None
    try:
        from hermes_state_registry import acquire, release_or_close
        db = acquire(db_path)
        try:
            return db.resolve_resume_session_id(session_key) or session_key
        finally:
            release_or_close(db)
    except Exception:
        return session_key


def _resolve_notify_target() -> Optional[dict[str, Any]]:
    """``kanban_db.add_notify_sub`` kwargs for the calling session, or None (CLI/cron/tests).
    Gateway sessions: ``HERMES_SESSION_PLATFORM``/``CHAT_ID`` ContextVars. TUI/desktop:
    those are cleared but the subprocess inherits ``HERMES_SESSION_KEY`` -> ``platform="tui"``
    for the TUI poller. ``HERMES_SESSION_ID`` is deliberately NOT a fallback: it is set for
    every CLI/ACP invocation and would auto-subscribe every CLI run."""
    from gateway.session_context import get_session_env as env
    platform, chat_id = env("HERMES_SESSION_PLATFORM", ""), env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        session_key = env("HERMES_SESSION_KEY", "") or os.environ.get("HERMES_SESSION_KEY", "")
        if not session_key:
            return None
        platform, chat_id = "tui", session_key
    chat_type = env("HERMES_SESSION_CHAT_TYPE", "") or None
    thread_id = env("HERMES_SESSION_THREAD_ID", "") or None
    message_id = env("HERMES_SESSION_MESSAGE_ID", "") or ""
    notifier_profile = env("HERMES_SESSION_PROFILE", "")
    if not notifier_profile:
        from hermes_cli.profiles import current_profile_name
        notifier_profile = current_profile_name("default")
    if platform == "tui":
        # The inherited key can be stale after a compaction fork (#110068): bind the
        # subscription to the live continuation tip, not the key the process started with.
        chat_id = _live_tui_session_key(chat_id, notifier_profile)
    delivery_metadata: dict[str, Any] = {
        k: v for k, v in (
            ("thread_id", thread_id), ("chat_type", chat_type),
            ("scope_id", env("HERMES_SESSION_SCOPE_ID", "")),
            ("parent_chat_id", env("HERMES_SESSION_PARENT_CHAT_ID", "")),
        ) if v}
    from gateway.session_context import get_bound_session_env
    bound_source_key = get_bound_session_env("HERMES_SESSION_KEY")
    if bound_source_key:
        delivery_metadata["session_key"] = bound_source_key
    if (platform.lower() == "telegram" and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}):
        delivery_metadata["telegram_dm_topic_reply_fallback"] = True
        if str(thread_id) not in {"", "1"}:
            delivery_metadata["direct_messages_topic_id"] = str(thread_id)
        if message_id:
            delivery_metadata["telegram_reply_to_message_id"] = str(message_id)
    return dict(
        platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
        user_id=env("HERMES_SESSION_USER_ID", "") or None,
        user_id_alt=env("HERMES_SESSION_USER_ID_ALT", "") or None,
        notifier_profile=notifier_profile,
        delivery_mode="notify+wake" if platform != "tui" else None,
        delivery_metadata=delivery_metadata or None)


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Subscribe the calling session to completion/block events; True iff a row was
    written (surfaced as ``subscribed`` so an orchestrator can fall back to explicit
    ``kanban_notify-subscribe``). Gated by ``kanban.auto_subscribe_on_create`` (default
    True). Failures are logged and swallowed: bookkeeping must never fail kanban_create."""
    try:
        if not cfg_get(load_config(), "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        pass  # unreadable config keeps the user-friendly default (True)
    target = None
    try:
        # Native creation already committed the exact current return target.
        if conn.execute("SELECT 1 FROM task_return_intents WHERE task_id=?", (task_id,)).fetchone():
            return True
        target = _resolve_notify_target()
        if target is None:
            return False  # CLI / cron / test — no persistent channel
        from hermes_cli import kanban_db_notify as _kbn
        # Inheritance and explicit subscriptions already encode the delivery policy.
        # Auto-subscribe must not turn a passive destination into an agent wake.
        if any(sub["platform"] == target["platform"] and sub["chat_id"] == target["chat_id"]
               and (sub["thread_id"] or "") == (target["thread_id"] or "")
               for sub in _kbn.list_notify_subs(conn, task_id)):
            return True
        _kbn.add_notify_sub(conn, task_id=task_id, **target)
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, target["platform"] if target else "", bool(target and target["chat_id"]))
        return False


@_kanban_handler("kanban_unblock")
def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task to ready, or todo while parents remain open."""
    _reject_delegated_child_mutation("kanban_unblock")
    _require_orchestrator_tool("kanban_unblock")
    tid = args.get("task_id")
    _check(tid, "task_id is required")
    tid = str(tid)
    _enforce_worker_task_ownership(tid)
    with _board(args.get("board")) as (kb, conn):
        if args.get('recovery') is not None:
            from hermes_cli.plain_advisory import recover_idle_task
            recovery = dict(args['recovery'])
            # Identity comes from this orchestrator session, not the payload.
            recovery['actor'] = _persisted_identity()
            return _ok(**recover_idle_task(conn, str(tid), **recovery))
        ok = kb.unblock_task(conn, str(tid))
        if not ok:
            task = kb.get_task(conn, str(tid))
            if task and task.status == 'triage':
                routes = _owner_board_routes(kb, conn, task, args.get("board"))
                if routes and routes.get('native'):
                    return tool_error(
                        'Use kanban_manage action=work with this task_id and the current work instruction in reason. It handles initial triage, stopped work and review routing internally. Do not repeat ordinary unblock or create a duplicate cleanup task.',
                        code='triage_resolution_required', status='triage', task_id=str(tid),
                        board_administration=routes)
                from hermes_cli.plain_advisory import snapshot_digest
                return tool_error('Task is in triage, not ordinary blocked state. Use the board_administration command templates for supported same-card continuation; do not repeat ordinary unblock or create another cleanup task. Inspect the latest block and all remaining holds. '
                    'If the specific action was already approved, do not ask again: verify successful execution and current capability. '
                    'For an idle plain capability/transient task, supply recovery with expected_snapshot_digest, '
                    'evidence_ref, evidence_digest and reason. Evidence JSON must identify task_id and resolved_block_event_id, '
                    'authorization_verified=true with authorization_ref, execution_succeeded=true with execution_ref, '
                    'capability_verified=true and unresolved_holds=[]. Denied/unexecuted actions, other unresolved holds and '
                    'owner-cell reviews require their existing supported route; no blind status flip or reset.',
                    code='triage_resolution_required', status='triage', task_id=str(tid),
                    snapshot_digest=snapshot_digest(conn, str(tid)),
                    board_administration=_owner_board_routes(kb, conn, task, args.get("board")))
            return tool_error(f"could not unblock {tid} (not blocked or unknown)")
        return _ok(task_id=tid, **_fields(kb.get_task(conn, tid), ("status",)))


@_kanban_handler("kanban_link")
def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact (cycles/self-links/running
    children → ValueError). A worker linking its OWN running card proves ownership
    with its run id so the dependency-block handoff still works."""
    _reject_delegated_child_mutation("kanban_link")
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    _check(parent_id and child_id, "both parent_id and child_id are required")
    with _board(args.get("board")) as (kb, conn):
        gated = kb.link_tasks(
            conn, parent_id=parent_id, child_id=child_id,
            expected_child_run_id=_worker_run_id(str(child_id)))
        return _ok(parent_id=parent_id, child_id=child_id, gated=gated,
                   **({"gated_by": parent_id} if gated else {}))




# Custom contracts extend the upstream schema catalog without mutating its shared objects.
from copy import deepcopy
from tools.kanban_tools_schemas import _DESC_TASK_ID_DEFAULT

KANBAN_SHOW_SCHEMA = deepcopy(KANBAN_SHOW_SCHEMA)
KANBAN_SHOW_SCHEMA['description'] = ('Read current task state once — title, body, assignee, parent task handoffs, your prior attempts '
 'on this task if any, comments, and recent events. Use this to (re)orient yourself before '
 'starting work, especially on retries. The response includes a current preparation and bounded '
 'history; use comments_before_id or events_before_id to read older relevant discussion and '
 'include_event_payloads for complete preparation audit payloads. For the admitted independent '
 'reviewer, child_execution contains compact native child model/count evidence. Unavailable does '
 'not mean zero children or success; ended is not a success verdict. Use recorded fields instead '
 'of session_search solely to re-prove them; still independently inspect the actual deliverable.')
KANBAN_SHOW_SCHEMA['parameters']['properties']['history_limit'] = {'type': 'integer',
 'minimum': 1,
 'maximum': 20,
 'description': 'Comments/events per page: default 3 for the current prepared worker, otherwise 8.'}
KANBAN_SHOW_SCHEMA['parameters']['properties']['comments_before_id'] = {'type': 'integer',
 'minimum': 1,
 'description': 'Read older comments on this same task using next_comments_before_id.'}
KANBAN_SHOW_SCHEMA['parameters']['properties']['events_before_id'] = {'type': 'integer',
 'minimum': 1,
 'description': 'Read older events on this same task using next_events_before_id.'}
KANBAN_SHOW_SCHEMA['parameters']['properties']['include_event_payloads'] = {'type': 'boolean',
 'description': 'Return full preparation audit payloads on the selected event page; default false '
                'avoids repeated briefs. Holds and review findings are never compacted.'}

KANBAN_COMPLETE_SCHEMA = deepcopy(KANBAN_COMPLETE_SCHEMA)
KANBAN_COMPLETE_SCHEMA['parameters']['properties']['metadata']['description'] = ('Free-form dict of structured facts about this attempt — {"changed_files": [...], "tests_run": '
 '12, "findings": [...]}. To carry prior Chris authorization through child work, include '
 'authorization_continuity with authorized=true, scope, allowed_actions, blocked_actions, '
 'security_scope/audience_scope, and any explicit_holds or requires_new_decision. It tells '
 'downstream workers to keep going only inside the scoped authorization; it does not release '
 'operation approvals, deploy/restart holds, external send approvals, or security/audience '
 'boundaries.')
KANBAN_COMPLETE_SCHEMA['parameters']['properties']['metadata']['properties'] = {'owner_cell_verdict': {'type': 'object',
                        'description': 'Required for native final owner/reviewer acceptance. Copy '
                                       'the CURRENT state version, artifact ref and digest from '
                                       'kanban_show.owner_cell after inspecting that exact file. '
                                       'Review authority comes from your claimed review run, not '
                                       'this metadata.',
                        'properties': {'state_version': {'type': 'integer'},
                                       'artifact_ref': {'type': 'string'},
                                       'artifact_digest': {'type': 'string'},
                                       'verdict': {'type': 'string', 'enum': ['PASS']},
                                       'defects': {'type': 'array', 'items': {'type': 'string'}}},
                        'required': ['state_version',
                                     'artifact_ref',
                                     'artifact_digest',
                                     'verdict',
                                     'defects']},
 'owner_acceptance_checks': {'type': 'array',
                             'description': 'For department-owned final review: every exact '
                                            'acceptance criterion from '
                                            'kanban_show.owner_workflow.brief, with passed=true '
                                            'and concrete evidence from inspecting the final '
                                            'artifact.',
                             'items': {'type': 'object',
                                       'properties': {'criterion': {'type': 'string'},
                                                      'passed': {'type': 'boolean'},
                                                      'evidence': {'type': 'string'}},
                                       'required': ['criterion', 'passed', 'evidence']}}}

KANBAN_BLOCK_SCHEMA = deepcopy(KANBAN_BLOCK_SCHEMA)
KANBAN_BLOCK_SCHEMA['parameters']['properties']['authorization_checked'] = {'type': 'boolean',
 'description': 'For needs_input on authorized work: true only after checking existing task and '
                'parent authorization.'}
KANBAN_BLOCK_SCHEMA['parameters']['properties']['new_decision'] = {'type': 'string',
 'description': 'The exact new decision needed beyond existing authorization; required with '
                'needs_input on authorized work.'}

KANBAN_HANDOFF_SCHEMA = {
    "name": "kanban_handoff",
    "description": "Accountable owner only: delegate a worthwhile bounded job to its maker, request required advisory, or choose action=owner to finish an ordinary task yourself in the same run. Owner mode needs summary and acceptance_criteria, not a separate brief. Independent/specialist review stays required unless a configured explicit user Fable waiver is recorded with action=advisory_waiver for this exact artifact. Maker handoff checks readiness. After exhausting corrections, the current owner-reviewer may use diagnosed_rework once with a hashed workspace JSON diagnosis (root_cause, source_evidence, changed_method, verification), summary and acceptance_criteria; then request changes on the same card. No review or counter is reset.",
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string", "description": _DESC_TASK_ID_DEFAULT},
        "board": KANBAN_SHOW_SCHEMA["parameters"]["properties"]["board"],
        "summary": {"type": "string"},
        "action": {"type": "string", "enum": ["maker", "advisory", "owner", "advisory_waiver", "diagnosed_rework"], "default": "maker"},
        "waiver_decision": {"type": "string", "enum": ["waive", "revoke"], "description": "For advisory_waiver only: follow the user's explicit decision for this exact task. Never infer consent from low risk."},
        "authorization_ref": {"type": "string", "maxLength": 500, "description": "Source message or recorded decision reference for the explicit task-specific user waiver."},
        "authorization_text": {"type": "string", "maxLength": 1000, "description": "Exact explicit user waiver/revocation instruction; owner attestation, not a claimed Fable PASS. No secrets."},
        "profile": {"type": "string", "description": "For advisory: a required or configured conditional profile listed in kanban_show.owner_workflow.available_advisory_reviewers. A requested conditional review must finish before acceptance. For advisory_waiver: fable-review only."},
        "packet": {"type": "string", "maxLength": 12000, "description": "For advisory: compact redacted text, no identifiers, secrets or local paths."},
        "confirm_manual": {"type": "boolean", "description": "For Fable only: true only with existing explicit Chris authorization."},
        "brief_ref": {"type": "string", "description": "Exact brief path in this task workspace."},
        "brief_digest": {"type": "string", "description": "Actual SHA-256 of the brief."},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "requirements": {"type": "object", "description": "Required existing toolsets, commands, read_paths, write_paths, mcp, terminal_network, localhost_server, browser_session. These declarations grant no new permission."},
        "repair": {"type": "object", "description": "Required after a blocker: the diagnosed cause, actual change, verification performed and prevention of recurrence.",
                   "properties": {k: {"type": "string"} for k in ("cause", "change", "verification", "prevention")},
                   "required": ["cause", "change", "verification", "prevention"]},
    }, "required": ["summary"], "anyOf": [{"required": ["brief_ref", "brief_digest", "acceptance_criteria", "requirements"]}, {"properties": {"action": {"const": "advisory"}}, "required": ["action", "profile", "packet"]}, {"properties": {"action": {"const": "owner"}}, "required": ["action", "acceptance_criteria"]}, {"properties": {"action": {"const": "diagnosed_rework"}}, "required": ["action", "brief_ref", "brief_digest", "acceptance_criteria"]}, {"properties": {"action": {"const": "advisory_waiver"}}, "required": ["action", "profile", "waiver_decision", "confirm_manual", "authorization_ref", "authorization_text"]}]},
}

KANBAN_REQUEST_REVIEW_SCHEMA = deepcopy(KANBAN_REQUEST_REVIEW_SCHEMA)
KANBAN_REQUEST_REVIEW_SCHEMA['description'] = ('Hand the task off for review: implementation, self-review, and verification are complete and you '
 "want a human (or reviewer) to look before it is marked done. Moves the task to the 'review' "
 'column and notifies the subscriber. Unlike ``kanban_block`` this is NOT a blocker — it never '
 'counts toward unblock-loop detection, so a task can cycle through review across follow-ups '
 'without ever being falsely escalated to triage. Use this instead of blocking with a free-form '
 "'review-required:' reason. For reviewed owner-cell tasks, first read kanban_show: reviewer must "
 'equal owner_cell.required_reviewer_profile, and metadata.owner_cell_artifact must contain the '
 'verified ref and digest. A successful handoff ends this worker run. A brief is not a finished '
 'deliverable.')
KANBAN_REQUEST_REVIEW_SCHEMA['parameters']['properties']['reviewer']['description'] = ('Required for reviewed owner-cell tasks: use owner_cell.required_reviewer_profile from '
 'kanban_show. Otherwise optional. Reassigns the task before review dispatch.')
KANBAN_REQUEST_REVIEW_SCHEMA['parameters']['properties']['metadata']['description'] = ('Optional structured handoff facts for the reviewer, such as changed_files, tests_run, commit, or '
 'decisions. authorization_continuity is accepted here with the same bounded semantics as '
 'kanban_complete so review follow-ups can keep prior Chris authorization visible without '
 'bypassing approval gates.')
KANBAN_REQUEST_REVIEW_SCHEMA['parameters']['properties']['metadata']['properties'] = {'owner_cell_artifact': {'type': 'object',
                         'description': 'Required for reviewed owner-cell tasks. Bind the exact '
                                        'artifact being reviewed; obtain its actual digest before '
                                        'handoff.',
                         'properties': {'ref': {'type': 'string',
                                                'description': 'Exact artifact path/reference.'},
                                        'digest': {'type': 'string',
                                                   'description': 'Verified artifact digest, such '
                                                                  'as its SHA-256 checksum.'}},
                         'required': ['ref', 'digest'],
                         'additionalProperties': False}}

KANBAN_REBIND_REVIEW_ARTIFACT_SCHEMA = {
    "name": "kanban_rebind_review_artifact",
    "description": (
        "Current native reviewer only: refresh an obsolete evidence digest at the same "
        "already-scoped artifact reference after verifying the replacement. Read current "
        "binding and state_version from kanban_show.owner_cell. Supply exact old and new "
        "bindings and reason. This closes your claim WITHOUT approval; a fresh reviewer "
        "run must inspect and approve the new artifact. Does not clear prerequisites or "
        "change access, owner, review gates, or content. If owner preparation is needed, "
        "its completion means delivery of the verified input packet, never performance "
        "of this reviewer-only operation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": _DESC_TASK_ID_DEFAULT},
            "board": KANBAN_SHOW_SCHEMA["parameters"]["properties"]["board"],
            "state_version": {"type": "integer"},
            **{name: {"type": "string"} for name in (
                "expected_artifact_ref", "expected_artifact_digest",
                "artifact_ref", "artifact_digest", "reason",
            )},
        },
        "required": ["state_version", "expected_artifact_ref", "expected_artifact_digest",
                     "artifact_ref", "artifact_digest", "reason"],
    },
}

KANBAN_ATTACH_SCHEMA = deepcopy(KANBAN_ATTACH_SCHEMA)
KANBAN_ATTACH_SCHEMA['description'] = ('Attach an existing file using file_path inside your admitted task workspace (max 1 MB); prefer '
 'this for generated reports, never copy their bytes into chat. Alternatively pass bytes inline '
 'with content_base64 and filename (max 25 MB). Use for genuine file artifacts the next worker or '
 'a human should be able to download — generated reports, images, exports. The file is stored as a '
 "real attachment (not a comment link) under the task's attachments dir, capped at 25 MB. Prefer "
 'kanban_attach_url when you only have a URL.')
KANBAN_ATTACH_SCHEMA['parameters']['properties']['file_path'] = {'type': 'string',
 'description': "Existing file within this native task's admitted workspace; relative or absolute. "
                'Mutually exclusive with content_base64. Filename defaults to leaf name. Max 1 MB.'}
KANBAN_ATTACH_SCHEMA['parameters']['required'] = []

KANBAN_ATTACHMENTS_SCHEMA = deepcopy(KANBAN_ATTACHMENTS_SCHEMA)
KANBAN_ATTACHMENTS_SCHEMA['description'] = ('List the files attached to a task: id, filename, content_type, size, uploader and storage path. '
 "A native task worker can supply attachment_id to inspect its own task's durable attachment "
 'through verified bytes: SHA-256, size and UTF-8 text up to 16 KiB (binary/larger text returns '
 'metadata/hash). Reads reject files above 1 MiB; no outside-workspace file permission is needed.')
KANBAN_ATTACHMENTS_SCHEMA['parameters']['properties']['attachment_id'] = {'type': 'integer',
 'minimum': 1,
 'description': 'Optional exact durable attachment ID; byte inspection requires a live native '
                'admission for this same task.'}

KANBAN_CREATE_SCHEMA = deepcopy(KANBAN_CREATE_SCHEMA)
KANBAN_CREATE_SCHEMA['parameters']['properties']['body']['description'] = ('Opening post: full spec, acceptance criteria, links. The assigned worker reads this as part of '
 'its context. Include a worker-requirements JSON fenced block for the current phase: toolsets, '
 'commands, read_paths, write_paths (arrays); mcp (server to operation-name arrays); '
 'terminal_network, localhost_server, browser_session (booleans). Derive these yourself from the '
 'task, do not ask the user to supply tools again. Put every required input/read file in '
 'read_paths even if the owner must stage it first; put expected output or artifact paths in '
 'write_paths so they are checked for writable parents, not existing content. Name exact staged '
 'input paths and service operations. Requirements request checks and never grant access.')
KANBAN_CREATE_SCHEMA['parameters']['properties']['authorization_continuity'] = {'type': 'object',
 'description': 'Optional scoped authorization handoff for this child. Use only when Chris has '
                'already authorized the outcome chain and this child stays within that scope. '
                'Include authorized=true, scope, allowed_actions, blocked_actions, '
                'security_scope/audience_scope, and explicit_holds or requires_new_decision when '
                'present. If omitted, a dispatcher worker creating a child with its own task id in '
                "parents inherits the current task's active authorization continuity when safe. A "
                'child that expands scope, audience, security/access, deploy/restart authority, '
                'external sends, or explicit holds needs a concrete new Chris decision instead.'}
KANBAN_CREATE_SCHEMA['parameters']['properties']['capability_contract'] = {'type': 'object',
 'description': 'Trusted first-class preflight contract, stored separately from the task body. '
                'Must declare schema_version=1, required/forbidden capabilities, workspace/data '
                'realm, network mode, browser/native/MCP/tool/auth needs, side-effect and approval '
                'gates, required skills, and a unique scope_id. Model-authored approval claims do '
                'not grant authority. Task-scoped localhost projection is disabled; local-server '
                'work must route to an existing unrestricted lane.'}

KANBAN_MANAGE_SCHEMA = {
    "name": "kanban_manage",
    "description": "Manage existing work. On repeated failures inspect kanban_show.instruction_recovery, correct the conflicting internal instruction or source, and submit the verified changed plan; do not just requeue. Prefer action=work with task_id and a short current instruction in reason to pick up authorized work using its assigned worker and normal tools. State and recovery evidence are resolved internally; no exact approval phrase or detailed preparation is required. An optional plan updates a changed brief/tools/budget. Use action=block with kind=needs_input, capability or transient and a concrete reason to put an idle unfinished card, including triage, on hold without starting work. No snapshot is required; the controller resolves it. This records a wait and does not grant external permissions. Other actions support reversible cleanup, precise dependency repair and review recovery. Preserve real waits, privacy, action restrictions, budgets and review findings. Do not repeatedly restart unchanged failed worker runs. A recovery attempt limits worker starts, not individual tool calls. Within the same run and existing budget, correct routine tool errors or try equivalent already-authorized tools and sources. Do not add a stop-after-first-error or one-shot capture restriction unless the user explicitly requires it. Preserve explicit user stops, action restrictions and privacy.",
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string"},
        "board": KANBAN_SHOW_SCHEMA["parameters"]["properties"]["board"],
        "action": {"type": "string", "description": "follow_up attaches passive blocker follow-ups to this authenticated conversation without changing the card or starting a worker. work/block also attach the current conversation after success.", "enum": ["follow_up", "work", "block", "prepare", "archive", "restore", "start_triage", "resume_triage", "unblock", "unlink_dependency", "recover_review", "repair_review_reference"]},
        "expected_state": {"type": "string", "description": "Not needed for work or block. Other actions require expected_state from the latest kanban_show native fields; reread after a state change."},
        "reason": {"type": "string", "description": "For work: preserve the user's actual instruction in plain language; do not invent a one-shot tool limit or stop-after-first-error condition. A one-run recovery limit still permits authorized alternatives within that run. Otherwise the verified correction or retirement reason. Existing external-action restrictions remain in force."},
        "kind": {"type": "string", "enum": ["needs_input", "capability", "transient"], "description": "Required for block: record a real missing input/decision, missing capability, or temporary failure. Include what resolves it in reason. Real prerequisite links remain unchanged."},
        "parent_id": {"type": "string", "description": "For unlink_dependency only, the specifically obsolete prerequisite."},
        "plan": {"type": "object", "description": "Optional for work, required for prepare. Use only when the existing brief, worker, tools or budget need updating. Ordinary work uses the current task and profile defaults. For idle ordinary maker work, {assignee: installed-worker} changes only the eligible worker while preserving its existing brief, requirements and budget. For an idle ordinary native review, work accepts only {assignee: installed-reviewer} to repair its reviewer while retaining maker evidence; it does not change bound owner-cell/private routes.", "properties": {
            "brief": {"type": "string", "maxLength": 6000},
            "acceptance": {"type": "array", "items": {"type": "string"}},
            "assignee": {"type": "string", "description": "Optional eligible existing worker. On an idle ordinary native review, work accepts an assignee-only plan to repair the reviewer. Bound private/owner-cell routes cannot be swapped."},
            "skills": {"type": "array", "items": {"type": "string"}},
            "requirements": {"type": "object", "description": "Existing worker-requirements fields: toolsets,commands,read_paths,write_paths,mcp,terminal_network,localhost_server,browser_session. Requirements select/check existing access; they never grant permissions."},
            "budget": {"type": "object", "properties": {
                "max_runs_per_day": {"type": "integer", "minimum": 1, "maximum": 4},
                "max_attempts_per_input": {"type": "integer", "enum": [1]},
                "worker_max_turns": {"type": "integer", "minimum": 12, "maximum": 64},
                "worker_run_seconds": {"type": "integer", "minimum": 60, "maximum": 1800},
                "worker_total_tokens": {"type": "integer", "minimum": 32000, "maximum": 2000000},
                "worker_request_tokens": {"type": "integer", "minimum": 8000, "maximum": 128000}}, "required": ["max_runs_per_day", "max_attempts_per_input", "worker_max_turns", "worker_run_seconds", "worker_total_tokens", "worker_request_tokens"], "additionalProperties": False}},
            "anyOf": [{"required": ["brief", "acceptance", "requirements", "budget"]}, {"required": ["assignee"], "maxProperties": 1}], "additionalProperties": False},
        "dry_run": {"type": "boolean", "description": "For prepare, triage/review recovery and repair_review_reference, preview first then apply once with false."},
        "evidence": {"type": "object", "properties": {
            "expected_stop_event_id": {"type": "integer"},
            "authorization_comment_id": {"type": "integer"},
            "expected_latest_comment_id": {"type": "integer"},
            "expected_task_sha256": {"type": "string"},
            "expected_goal_sha256": {"type": "string"},
            "expected_artifact_ref": {"type": "string"},
            "expected_artifact_digest": {"type": "string"},
            "artifact_ref": {"type": "string", "description": "For repair_review_reference only: exact original regular file within the existing task workspace; unchanged bound digest, no symlinks."},
            "evidence_comment_ids": {"type": "array", "items": {"type": "integer"}},
        }, "additionalProperties": False,
        "description": "For start_triage: authorization_comment_id from this same new card. For resume_triage: stop, same-card authorization comment after stop, latest comment and task SHA. For recover_review: stop, latest comment, task SHA, goal SHA and same-card bound-reviewer comment IDs; omit authorization_comment_id. For repair_review_reference: expected_goal_sha256, expected_artifact_ref and expected_artifact_digest from native inspection, plus artifact_ref of the unchanged original file; no other evidence fields."},
    }, "required": ["task_id", "action", "reason"], "additionalProperties": False},
}

KANBAN_UNBLOCK_SCHEMA = deepcopy(KANBAN_UNBLOCK_SCHEMA)
KANBAN_UNBLOCK_SCHEMA['parameters']['properties']['recovery'] = {'type': 'object',
 'description': 'Optional verified-resolution path for idle plain blocked/triage capability or '
                'transient work; no review migration, approval grant or counter reset. Inspect '
                'current holds and successful execution first. Evidence JSON fields are returned '
                'by the triage guidance.',
 'properties': {'expected_snapshot_digest': {'type': 'string'},
                'evidence_ref': {'type': 'string'},
                'evidence_digest': {'type': 'string'},
                'reason': {'type': 'string'}},
 'required': ['expected_snapshot_digest', 'evidence_ref', 'evidence_digest', 'reason'],
 'additionalProperties': False}

# --- Registration (order preserved: it is the order tools appear in the schema) ---

# kanban_list / kanban_unblock route the board and are hidden from task workers.
_ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock"})
_TOOLS = (
    ("kanban_show", KANBAN_SHOW_SCHEMA, _handle_show, "📋"),
    ("kanban_list", KANBAN_LIST_SCHEMA, _handle_list, "📋"),
    ("kanban_complete", KANBAN_COMPLETE_SCHEMA, _handle_complete, "✔"),
    ("kanban_block", KANBAN_BLOCK_SCHEMA, _handle_block, "⏸"),
    ("kanban_schedule", KANBAN_SCHEDULE_SCHEMA, _handle_schedule, "⏰"),


    ("kanban_handoff", KANBAN_HANDOFF_SCHEMA, _handle_owner_handoff, "↪"),
    ("kanban_request_review", KANBAN_REQUEST_REVIEW_SCHEMA, _handle_request_review, "👀"),
    ("kanban_request_changes", KANBAN_REQUEST_CHANGES_SCHEMA, _handle_request_changes, "↩"),
    ("kanban_heartbeat", KANBAN_HEARTBEAT_SCHEMA, _handle_heartbeat, "💓"),
    ("kanban_comment", KANBAN_COMMENT_SCHEMA, _handle_comment, "💬"),
    ("kanban_attach", KANBAN_ATTACH_SCHEMA, _handle_attach, "📎"),
    ("kanban_attach_url", KANBAN_ATTACH_URL_SCHEMA, _handle_attach_url, "📎"),
    ("kanban_attachments", KANBAN_ATTACHMENTS_SCHEMA, _handle_attachments, "📎"),
    ("kanban_create", KANBAN_CREATE_SCHEMA, _handle_create, "➕"),
    ("kanban_unblock", KANBAN_UNBLOCK_SCHEMA, _handle_unblock, "▶"),
    ("kanban_link", KANBAN_LINK_SCHEMA, _handle_link, "🔗"),
    ("kanban_rebind_review_artifact", KANBAN_REBIND_REVIEW_ARTIFACT_SCHEMA,
     _handle_rebind_review_artifact, "👀"))

for _name, _sch, _handler, _emoji in _TOOLS:
    _gate = _check_kanban_orchestrator_mode if _name in _ORCHESTRATOR_TOOLS else _check_kanban_mode
    registry.register(name=_name, toolset="kanban", schema=_sch, handler=_handler, emoji=_emoji,
                      check_fn=_gate)

registry.register(name="kanban_manage", toolset="kanban", schema=KANBAN_MANAGE_SCHEMA,
                  handler=_handle_manage, check_fn=_check_board_owner_mode, emoji="📋")

# The same caller-bound handlers are referenced by the existing pilot and the
# normal Kanban catalog. Register once so shared registry membership cannot
# change according to whichever member constructed a schema most recently.
from gateway.member_board_tools import register as _register_member_board_tools
_register_member_board_tools("josh_status_pilot")
from gateway.owner_delegation import register as _register_owner_delegation
_register_owner_delegation()

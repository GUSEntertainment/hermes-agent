"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _is_delegated_child_context() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context

        return is_delegated_child_context()
    except Exception:
        return False


def _is_dispatcher_owned_worker() -> bool:
    """False for delegate_task children AND for cron jobs fired in-process from
    a worker — i.e. whenever HERMES_KANBAN_* is present but not ours."""
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return True


def _reject_delegated_child_mutation(tool_name: str) -> Optional[str]:
    """Deny Kanban mutations from delegate_task children.

    A delegate_task child runs in the same process as its parent, so stale or
    inherited HERMES_KANBAN_* env vars are not proof of dispatcher ownership.
    The child may summarize findings to its parent, but it must not complete,
    block, heartbeat, comment, create, link, or unblock board tasks directly.
    """
    if not _is_delegated_child_context():
        return None
    return tool_error(
        f"{tool_name} refused: delegate_task child agents are not Kanban "
        "run owners. Return findings to the parent agent; the dispatcher "
        "worker or an explicitly configured Kanban orchestrator must perform "
        "board mutations."
    )


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return False
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    bound = os.environ.get("HERMES_KANBAN_TASK")
    if bound and os.environ.get("HERMES_PROFILE") in {"claude-review", "fable-review"} and arg and arg != bound:
        raise PermissionError("Advisory workers can access only their own redacted packet task")
    if arg:
        return arg
    if _is_delegated_child_context():
        return None
    if not _is_dispatcher_owned_worker():
        # A cron job fired in-process from a worker must never inherit the
        # worker's task id as an implicit default.
        return None
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


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


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    if (os.environ.get("HERMES_KANBAN_TASK") and os.environ.get("HERMES_PROFILE") in {"claude-review", "fable-review"}
            and board and board != os.environ.get("HERMES_KANBAN_BOARD")):
        raise PermissionError("Advisory workers cannot switch boards")
    from gateway.josh_board_authority import admitted_board, _execution
    from gateway.member_worker_policy import authenticated_private_policy
    from hermes_cli.board_owner_manage import require_owner, member_board, _validate_member_connection
    member = None
    # Interactive API members must not inherit LEGACY Connor board access or
    # a process-global DB override. Proven private workers keep task admission.
    if _execution.get() is not None and authenticated_private_policy() is None:
        member = require_owner()
    if member is not None:
        from hermes_cli import kanban_db as kb
        board = member_board(member, board)
        conn = kb.connect(board=board)
        try:
            _validate_member_connection(conn, member)
            if require_owner() != member:
                raise PermissionError('Member enrollment changed while opening board')
        except Exception:
            conn.close()
            raise
        return kb, conn
    board = admitted_board(board)
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """True when an auxiliary client is configured for the goal judge.

    ``judge_goal`` is fail-open at the source: when no auxiliary model can
    be reached it returns a ``"continue"`` verdict that is indistinguishable
    from a real "not done yet" judgment. The completion gate must not treat
    that as a rejection, or an unconfigured/degraded auxiliary model would
    wedge every ``goal_mode`` worker (it could never close its own task).

    So we probe availability first and only enforce the gate when a judge is
    actually reachable. This mirrors the same client lookup ``judge_goal``
    performs internally.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


def _goal_mode_handoff_rejection(
    task, evidence: str, *, phase: str = "completion",
) -> Optional[str]:
    """Judge final completion; review entry retains its native checks."""
    if phase not in {"completion", "review"}:
        raise ValueError("unknown goal handoff phase")
    if phase == "review":
        return None  # Deterministic native checks govern entry to review.
    if not task or not task.goal_mode or not _goal_judge_available():
        return None
    from hermes_cli.kanban_judgment import handoff_goal
    goal = handoff_goal(task, phase=phase)
    verdict = "done"
    reason = ""
    try:
        verdict, reason, _, _, _ = judge_goal(
            goal=goal,
            last_response=evidence.strip(),
        )
    except Exception as judge_exc:
        # Keep the existing fail-open semantics: an unavailable/broken
        # auxiliary judge must not permanently wedge goal-mode work.
        logger.warning(
            "goal judge check failed, allowing lifecycle handoff: %s",
            judge_exc,
            exc_info=True,
        )
    return reason if verdict != "done" else None


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


# Live operator-note injection: poll the worker's task for new comments and
# fold them into the running agent via the OUT-OF-BAND steer channel, so a user
# can "talk to" a running kanban task without the block → comment → unblock
# dance (or a restart). Rate-limited on its own (tighter than the 60s heartbeat
# so notes land within a few seconds), watermarked per task id.
_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0
_comment_poll_last_attempt: float = 0.0
# task_id -> highest comment id already seen (seeded on first poll so history
# already present in build_worker_context isn't re-injected).
_comment_watermark: dict[str, int] = {}


def inject_new_comments_from_env(agent: Any) -> bool:
    """Fold new operator comments on the current worker's task into ``agent``.

    Best-effort and self-gating: no-op unless this process is a kanban worker
    (``HERMES_KANBAN_TASK`` set) and ``agent`` exposes ``steer``. Returns True
    if a steer was injected, else False. Never raises into the agent loop.

    The first poll only *seeds* the watermark to the newest existing comment —
    those are already in the worker's context — so only comments added after
    the run started are injected. The worker's own authored comments (matched
    by ``HERMES_PROFILE``) are skipped to avoid echoing itself.
    """
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid or agent is None or not hasattr(agent, "steer"):
        return False
    global _comment_poll_last_attempt
    import time as _time
    now = _time.monotonic()
    if (now - _comment_poll_last_attempt) < _COMMENT_POLL_MIN_INTERVAL_SECONDS:
        return False
    _comment_poll_last_attempt = now

    seen = _comment_watermark.get(tid)
    try:
        kb, conn = _connect()
        try:
            rows = kb.list_comments_after(conn, tid, after_id=seen or 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        logger.debug("comment-inject: bridge failed", exc_info=True)
        return False

    if seen is None:
        # First poll for this task: seed past the existing thread, inject nothing.
        _comment_watermark[tid] = max((c.id for c in rows), default=0)
        return False
    if not rows:
        return False

    # Advance the watermark past everything we just read (including our own
    # notes) so nothing is re-injected next poll.
    _comment_watermark[tid] = max(c.id for c in rows)

    own = (os.environ.get("HERMES_PROFILE") or "").strip()
    fresh = [c for c in rows if (c.author or "").strip() != own and (c.body or "").strip()]
    if not fresh:
        return False

    lines = [f"- {c.author or 'operator'}: {c.body.strip()}" for c in fresh]
    note = (
        "New note"
        + ("s" if len(fresh) > 1 else "")
        + " on your kanban task from the operator (delivered mid-run). "
        + "Take it into account for the work you're doing right now:\n"
        + "\n".join(lines)
    )
    try:
        return bool(agent.steer(note))
    except Exception:
        logger.debug("comment-inject: steer failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "provider_override": task.provider_override,
        "capability_status": task.capability_status,
        "capability_projection": json.loads(task.capability_projection) if task.capability_projection else None,
        "capability_checked_at": task.capability_checked_at,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _owner_board_routes(kb, conn, task, board=None):
    """Expose supported control paths without changing session authority."""
    from hermes_cli.board_owner_manage import require_owner, member_board, _validate_member_connection
    try:
        member = require_owner()
    except PermissionError:
        member = None
    if member is None and not _check_kanban_orchestrator_mode():
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
        if member is None and not _profile_has_kanban_toolset():
            raise PermissionError('The owner profile must enable its existing kanban toolset')
        board = member_board(member, args.get('board')) if member is not None else admitted_board(args.get('board'))
        payload = dict(args, board=board)
        return json.dumps(_invoke_board_controller(payload))
    except Exception as exc:
        return tool_error(f'kanban_manage: {exc}')


def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    from gateway.member_worker_policy import authenticated_private_policy
    private_policy = authenticated_private_policy()
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
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

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                    "provider_override": t.provider_override,
                    "capability_status": t.capability_status,
                    "capability_projection": json.loads(t.capability_projection) if t.capability_projection else None,
                    "capability_checked_at": t.capability_checked_at,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata_available": bool(r.metadata),
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            from hermes_cli.owner_workflow import describe as describe_workflow
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
                "board_administration": _owner_board_routes(kb, conn, task, board),
                "task": _task_dict(task),
                "owner_cell": kb.owner_cell_read_state(conn, tid),
                "owner_workflow": describe_workflow(conn, tid),
                "parents": parents,
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
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    delegated_err = _reject_delegated_child_mutation("kanban_complete")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata, auth_error = _normalize_authorization_metadata(metadata)
    if auth_error:
        return tool_error(auth_error)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            rejection = _goal_mode_handoff_rejection(
                task,
                (summary or result or "").strip(),
            )
            if rejection is not None:
                return tool_error(
                    f"Goal completion rejected by judge: {rejection}. "
                    f"To proceed, either: (1) provide explicit acceptance "
                    f"evidence in your summary matching the task's criteria, "
                    f"or (2) create continuation tasks with parents=[{tid}] "
                    f"and keep this task alive."
                )

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
                        kb, task_id=tid, board=board, artifacts=paths,
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
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=run_id,
                    review_decision_capability=decision_token(tid, run_id),
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            from hermes_cli.owner_workflow import describe as describe_workflow
            return _ok(
                owner_workflow=describe_workflow(conn, tid),task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    delegated_err = _reject_delegated_child_mutation("kanban_block")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete
        # judge gate in #38367). kanban_block is a second exit path out of
        # the goal loop — run_kanban_goal_loop() treats ANY `blocked` status
        # as terminal, identically to `done`, regardless of kind. Without
        # this, a worker that learns kanban_complete is gated can just call
        # kanban_block(reason="anything") to escape the loop instead.
        # Restrict goal_mode tasks to the kinds that represent a genuine
        # external blocker the worker cannot resolve itself; `capability`
        # and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        try:
            reason = kb.authorized_block_reason(
                conn, tid, kind=kind, reason=reason,
                authorization_checked=args.get("authorization_checked"),
                new_decision=args.get("new_decision"),
            )
        except ValueError as exc:
            conn.close()
            return tool_error(str(exc))
        if (
            task
            and task.goal_mode
            and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS
        ):
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


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


def _handle_owner_handoff(args: dict, **kw) -> str:
    delegated = _reject_delegated_child_mutation("kanban_handoff")
    if delegated:
        return delegated
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required")
    ownership = _enforce_worker_task_ownership(tid)
    if ownership:
        return ownership
    from hermes_cli.owner_workflow import handoff
    from hermes_cli.kanban_db import WorkerAdmissionError
    try:
        kb, conn = _connect(board=args.get("board"))
        try:
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
        finally:
            conn.close()
    except WorkerAdmissionError as exc:
        return tool_error("Maker readiness failed; the owner still holds this task. Correct the stated requirements or approved setup, then retry. If network/account/path access is owner-only, use the owner's existing authorized tools to retrieve and stage non-secret same-scope inputs inside this workspace, then hand off offline processing. Do not copy credentials or broaden the maker's permissions.",
                          code="maker_not_ready", issues=(exc.readiness or {}).get("issues", []))
    except (ValueError, OSError) as exc:
        return tool_error(str(exc))


def _handle_request_review(args: dict, **kw) -> str:
    """Move implementation into the first-class review phase."""
    delegated_err = _reject_delegated_child_mutation("kanban_request_review")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    if not summary or not str(summary).strip():
        return tool_error(
            "summary is required — describe what was implemented and how it "
            "was verified so the reviewer has context"
        )
    summary = redact_sensitive_text(str(summary), force=True)
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    if metadata is not None:
        metadata_json = redact_sensitive_text(json.dumps(metadata), force=True)
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError:
            return tool_error("metadata could not be safely serialized")
    metadata, auth_error = _normalize_authorization_metadata(metadata)
    if auth_error:
        return tool_error(auth_error)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    reviewer = args.get("reviewer") or None
    if reviewer:
        # Model-supplied free text stored durably on the event payload —
        # redact like summary / kanban_block's reason.
        reviewer = redact_sensitive_text(str(reviewer), force=True)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            input_error = _review_handoff_input_error(conn, tid, reviewer, metadata)
            if input_error is not None:
                return input_error
            # Review entry is not final completion. The native transition
            # validates the live maker, required reviewer and bound artifact;
            # that reviewer owns assessment of the evidence and remaining work.
            with kb.write_txn(conn):
                admission = conn.execute("SELECT worker_admission_contract FROM tasks WHERE id=?", (tid,)).fetchone()
                if admission and admission[0] and _worker_run_id(tid) is not None:
                    from hermes_cli.child_execution_receipts import capture
                    capture(conn, tid, server_session_id=kw.get("session_id"))
                ok, fail_reason = kb.request_review(
                    conn, tid,
                    summary=summary,
                    metadata=metadata,
                    reviewer=reviewer,
                    expected_run_id=_worker_run_id(tid),
                    with_reason=True,
                    _allow_nested=True,
                )
                if not ok:
                    raise ValueError("could not request review: " + (fail_reason or "transition refused"))
            if not ok:
                detail = fail_reason or "unknown id or not in running/ready"
                return tool_error(
                    f"could not request review for {tid}: {detail}"
                )
            run = kb.latest_run(conn, tid)
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "review",
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_review: {e}")
    except Exception as e:
        logger.exception("kanban_request_review failed")
        return tool_error(f"kanban_request_review: {e}")


def _handle_rebind_review_artifact(args: dict, **kw) -> str:
    """Evidence-only reviewer handoff; authority never comes from tool arguments."""
    delegated_err = _reject_delegated_child_mutation("kanban_rebind_review_artifact")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    try:
        kb, conn = _connect(board=args.get("board"))
        try:
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
        finally:
            conn.close()
    except (ValueError, PermissionError) as exc:
        return tool_error(f"kanban_rebind_review_artifact: {exc}")
    except Exception:
        logger.exception("kanban_rebind_review_artifact failed")
        return tool_error("kanban_rebind_review_artifact failed; state unchanged")


def _handle_request_changes(args: dict, **kw) -> str:
    """Return a reviewer-owned running task to its implementer."""
    delegated_err = _reject_delegated_child_mutation("kanban_request_changes")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — describe the changes needed")
    reason = redact_sensitive_text(str(reason), force=True)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            from agent.review_authority import decision_token

            run_id = _worker_run_id(tid)
            ok, detail = kb.request_changes(
                conn,
                tid,
                reason=reason,
                expected_run_id=run_id,
                review_decision_capability=decision_token(tid, run_id),
            )
            if not ok:
                return tool_error(
                    f"could not request changes for {tid}: {detail or 'invalid review state'}"
                )
            landed = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "ready",
                implementer=detail,
                continue_work=bool(landed and landed.status == "running"),
                next_action=("Owner takeover: finish this same run. Read kanban_show.owner_workflow; "
                             "provide the finished file and criterion evidence, not another maker handoff."
                             if landed and landed.status == "running" else None),
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_changes: {e}")
    except Exception as e:
        logger.exception("kanban_request_changes failed")
        return tool_error(f"kanban_request_changes: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    delegated_err = _reject_delegated_child_mutation("kanban_heartbeat")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    delegated_err = _reject_delegated_child_mutation("kanban_comment")
    if delegated_err:
        return delegated_err
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    try:
        from hermes_cli.board_owner_manage import require_owner
        member = require_owner()
        if member is not None:
            author = member['profile']
    except PermissionError:
        pass
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task.

    Mirrors the dashboard's upload endpoint for the agent surface: decode
    the payload, enforce the shared size cap, write it under the per-task
    attachments dir, and record the metadata row — all via
    ``kanban_db.store_attachment_bytes`` so the three surfaces stay in lockstep.
    """
    from gateway.josh_board_authority import admitted_board
    try:
        board = admitted_board(args.get("board"))
    except PermissionError as e:
        return tool_error(str(e))
    from hermes_cli import kanban_db as kb

    delegated_err = _reject_delegated_child_mutation("kanban_attach")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    if args.get("file_path") is not None:
        if args.get("content_base64") is not None:
            return tool_error("file_path and content_base64 are mutually exclusive")
        try:
            from tools.task_attachment_read import store_workspace_attachment
            _, conn = _connect(board=board)
            try:
                return _ok(**store_workspace_attachment(kb, conn, tid, args["file_path"], args.get("filename"), args.get("content_type")))
            finally:
                conn.close()
        except Exception as e:
            return tool_error(f"kanban_attach: {e}")
    filename = args.get("filename")
    if not filename or not str(filename).strip():
        return tool_error("filename is required")
    content_b64 = args.get("content_base64")
    if not content_b64 or not str(content_b64).strip():
        return tool_error("content_base64 is required")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        return tool_error(f"content_base64 is not valid base64: {e}")
    content_type = args.get("content_type")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach: {e}")
    except Exception as e:
        logger.exception("kanban_attach failed")
        return tool_error(f"kanban_attach: {e}")


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) with SSRF guarding, capped at ``max_bytes``.

    Every hop — the initial URL and each redirect target — is validated with
    ``tools.url_safety.is_safe_url`` before it is fetched, so a
    model-controlled URL (or a public host 302ing to one) cannot reach
    loopback, private/CGNAT ranges, or cloud metadata endpoints. Redirects
    are followed manually (``follow_redirects=False``) so each Location is
    re-checked, mirroring ``tools.skills_hub._guarded_http_get``.

    Returns ``(data, content_type)``. Raises ``ValueError`` for a non-http(s)
    scheme, an SSRF-blocked target, too many redirects, or a body that
    overruns the cap (the caller maps it to a clean tool error). Reads in
    chunks so an oversize response is rejected without buffering the whole
    thing.
    """
    from urllib.parse import urljoin, urlparse

    import httpx

    from tools.url_safety import is_safe_url

    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only http/https are allowed"
            )
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}"
            )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            current_url,
            headers={"User-Agent": "hermes-kanban/attach"},
            timeout=30,
            follow_redirects=False,
        ) as resp:
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
                    raise ValueError(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from a URL.

    The agent passes a URL; Hermes downloads it (with the shared size cap)
    and stores it as a real attachment. Useful when the agent has a link
    rather than the bytes. Only http/https URLs are accepted.
    """
    from gateway.josh_board_authority import admitted_board
    try:
        board = admitted_board(args.get("board"))
    except PermissionError as e:
        return tool_error(str(e))
    from hermes_cli import kanban_db as kb

    delegated_err = _reject_delegated_child_mutation("kanban_attach_url")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    url = args.get("url")
    if not url or not str(url).strip():
        return tool_error("url is required")
    url = str(url).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        leaf = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
        filename = leaf or "download"
    content_type = args.get("content_type")
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type or fetched_ct,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach_url: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url failed")
        return tool_error(f"kanban_attach_url: {e}")


def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if kb.get_task(conn, tid) is None:
                return tool_error(f"task {tid} not found")
            if args.get("attachment_id") is not None:
                from tools.task_attachment_read import read_attachment
                return json.dumps(read_attachment(kb, conn, tid, args["attachment_id"]))
            atts = kb.list_attachments(conn, tid)
            return json.dumps({
                "ok": True,
                "task_id": tid,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size,
                        "uploaded_by": a.uploaded_by,
                        "stored_path": a.stored_path,
                        "created_at": a.created_at,
                    }
                    for a in atts
                ],
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_attachments: {e}")
    except Exception as e:
        logger.exception("kanban_attachments failed")
        return tool_error(f"kanban_attachments: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    delegated_err = _reject_delegated_child_mutation("kanban_create")
    if delegated_err:
        return delegated_err
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    # Prefer the request-scoped api_server origin binding: HERMES_SESSION_ID
    # is clobbered with a subagent's internal id whenever a child agent is
    # constructed in-process (agent_init calls set_current_session_id), which
    # would stamp — and later wake — the wrong session.
    from tools.async_delegation import _current_origin_session_id
    from gateway.session_context import get_bound_session_env

    session_id = (
        args.get("session_id")
        or _current_origin_session_id()
        or get_bound_session_env("HERMES_SESSION_ID")
        or os.environ.get("HERMES_SESSION_ID")
    )
    priority = args.get("priority")
    # Resolve workspace. Workspace sharing is always explicit: omitted fields
    # mean a fresh scratch workspace, even when a dispatcher-spawned worker
    # creates the task. Reusing a parent's literal path would let a child
    # mutate review evidence or race the parent's checkout (#67567).
    #
    # Project identity is the one safe context to inherit implicitly. The DB
    # resolves a project-linked scratch request into a fresh per-task worktree,
    # preserving the repository/branch convention without sharing a checkout.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    project_source_task_id = None
    _inherit_project = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    capability_contract = args.get("capability_contract")
    if capability_contract is not None and not isinstance(capability_contract, dict):
        return tool_error(
            f"capability_contract must be an object, got {type(capability_contract).__name__}"
        )
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    model_override = args.get("model")
    provider_override = args.get("provider")
    if provider_override and not model_override:
        return tool_error("'provider' requires 'model' to be set as well")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    parents = [str(p).strip() for p in parents if str(p).strip()]
    board = args.get("board")
    requested_authorization = args.get("authorization_continuity")
    try:
        kb, conn = _connect(board=board)
        try:
            auth_contract, auth_error = _authorization_contract_for_child(
                kb,
                conn,
                self_task_id=os.environ.get("HERMES_KANBAN_TASK"),
                parents=parents,
                requested=requested_authorization,
            )
            if auth_error:
                return tool_error(auth_error)
            if auth_contract is not None:
                body = kb.append_authorization_continuity_to_body(body, auth_contract)
            # A project link is safe to inherit because ``create_task`` turns
            # it into a fresh per-task worktree. Never inherit the parent's
            # literal workspace kind/path; directory sharing must be explicit.
            if _inherit_project and project_id is None:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.project_id:
                        project_id = _self_task.project_id
                        project_source_task_id = _self_task.id
            from gateway.native_task_routing import compose
            body = compose(conn, title=str(title).strip(), body=body, assignee=str(assignee), parents=parents, session_id=session_id)
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                project_source_task_id=project_source_task_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                model_override=model_override,
                provider_override=provider_override,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                capability_contract=capability_contract,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            from gateway.native_task_receipts import record_created
            task_receipt_recorded = record_created(conn, new_task)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
                capability_status=new_task.capability_status if new_task else None,
                capability_projection=json.loads(new_task.capability_projection) if new_task and new_task.capability_projection else None,
                workspace_kind=new_task.workspace_kind if new_task else None,
                workspace_path=new_task.workspace_path if new_task else None,
                project_id=new_task.project_id if new_task else None,
                subscribed=subscribed,
                task_receipt_recorded=task_receipt_recorded,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``,
      ``HERMES_SESSION_CHAT_ID``, and ``HERMES_SESSION_CHAT_TYPE`` are set in
      ContextVars by the messaging gateway before agent dispatch. The
      notification poller already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    # Atomic native creation already committed the exact current return target.
    # Never overwrite its metadata/cursor with this legacy best-effort helper.
    if conn.execute("SELECT 1 FROM task_return_intents WHERE task_id=?", (task_id,)).fetchone():
        return True

    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        is_gateway_session = platform != "tui"
        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or None
        delivery_mode = "notify+wake" if is_gateway_session else None
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        user_id_alt = get_session_env("HERMES_SESSION_USER_ID_ALT", "") or None
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "") or ""
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )
        if not notifier_profile:
            try:
                from hermes_cli.profiles import get_active_profile_name
                notifier_profile = get_active_profile_name() or "default"
            except Exception:
                notifier_profile = "default"
        delivery_metadata: dict[str, Any] = {}
        if thread_id:
            delivery_metadata["thread_id"] = thread_id
        if chat_type:
            delivery_metadata["chat_type"] = chat_type
        if (
            platform.lower() == "telegram"
            and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}
        ):
            delivery_metadata["telegram_dm_topic_reply_fallback"] = True
            if str(thread_id) not in {"", "1"}:
                delivery_metadata["direct_messages_topic_id"] = str(thread_id)
            if message_id:
                delivery_metadata["telegram_reply_to_message_id"] = str(message_id)

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id, user_id_alt=user_id_alt,
            chat_type=chat_type,
            notifier_profile=notifier_profile,
            delivery_mode=delivery_mode,
            delivery_metadata=delivery_metadata or None,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task to ready, or todo while parents remain open."""
    delegated_err = _reject_delegated_child_mutation("kanban_unblock")
    if delegated_err:
        return delegated_err
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if args.get('recovery') is not None:
                from hermes_cli.plain_advisory import recover_idle_task
                recovery = dict(args['recovery'])
                # Identity comes from this orchestrator session, not the payload.
                recovery['actor'] = os.environ.get('HERMES_PROFILE') or 'default'
                return _ok(**recover_idle_task(conn, str(tid), **recovery))
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                task = kb.get_task(conn, str(tid))
                if task and task.status == 'triage':
                    routes = _owner_board_routes(kb, conn, task, board)
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
                        board_administration=_owner_board_routes(kb, conn, task, board))
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            task = kb.get_task(conn, str(tid))
            return _ok(task_id=str(tid), status=task.status if task else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    delegated_err = _reject_delegated_child_mutation("kanban_link")
    if delegated_err:
        return delegated_err
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read current task state once — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "current preparation and bounded history; use comments_before_id or events_before_id to read older relevant discussion and include_event_payloads for complete preparation audit payloads. For the admitted independent reviewer, "
        "child_execution contains compact native child model/count evidence. "
        "Unavailable does not mean zero children or success; ended is not a "
        "success verdict. Use recorded fields instead of session_search solely "
        "to re-prove them; still independently inspect the actual deliverable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
            "history_limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Comments/events per page: default 3 for the current prepared worker, otherwise 8."},
            "comments_before_id": {"type": "integer", "minimum": 1, "description": "Read older comments on this same task using next_comments_before_id."},
            "events_before_id": {"type": "integer", "minimum": 1, "description": "Read older events on this same task using next_events_before_id."},
            "include_event_payloads": {"type": "boolean", "description": "Return full preparation audit payloads on the selected event page; default false avoids repeated briefs. Holds and review findings are never compacted."},
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. To carry prior Chris authorization "
                    "through child work, include authorization_continuity with "
                    "authorized=true, scope, allowed_actions, blocked_actions, "
                    "security_scope/audience_scope, and any explicit_holds or "
                    "requires_new_decision. It tells downstream workers to keep "
                    "going only inside the scoped authorization; it does not "
                    "release operation approvals, deploy/restart holds, external "
                    "send approvals, or security/audience boundaries."
                ),
                "properties": {
                    "owner_cell_verdict": {
                        "type": "object",
                        "description": "Required for native final owner/reviewer acceptance. Copy the CURRENT state version, artifact ref and digest from kanban_show.owner_cell after inspecting that exact file. Review authority comes from your claimed review run, not this metadata.",
                        "properties": {
                            "state_version": {"type": "integer"},
                            "artifact_ref": {"type": "string"},
                            "artifact_digest": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["PASS"]},
                            "defects": {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["state_version", "artifact_ref", "artifact_digest", "verdict", "defects"]
                    },
                    "owner_acceptance_checks": {
                        "type": "array",
                        "description": "For department-owned final review: every exact acceptance criterion from kanban_show.owner_workflow.brief, with passed=true and concrete evidence from inspecting the final artifact.",
                        "items": {"type": "object", "properties": {
                            "criterion": {"type": "string"},
                            "passed": {"type": "boolean"},
                            "evidence": {"type": "string"}
                        }, "required": ["criterion", "passed", "evidence"]}
                    }
                },
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "``reason`` is shown to the human on the board. If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "authorization_checked": {"type": "boolean", "description": "For needs_input on authorized work: true only after checking existing task and parent authorization."},
            "new_decision": {"type": "string", "description": "The exact new decision needed beyond existing authorization; required with needs_input on authorized work."},
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HANDOFF_SCHEMA = {
    "name": "kanban_handoff",
    "description": "Accountable owner only: delegate a worthwhile bounded job to its maker, request required advisory, or choose action=owner to finish an ordinary task yourself in the same run. Owner mode needs summary and acceptance_criteria, not a separate brief. Independent/specialist review stays required unless a configured explicit user Fable waiver is recorded with action=advisory_waiver for this exact artifact. Maker handoff checks readiness. After exhausting corrections, the current owner-reviewer may use diagnosed_rework once with a hashed workspace JSON diagnosis (root_cause, source_evidence, changed_method, verification), summary and acceptance_criteria; then request changes on the same card. No review or counter is reset.",
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string", "description": _DESC_TASK_ID_DEFAULT},
        "board": _board_schema_prop(),
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

KANBAN_REQUEST_REVIEW_SCHEMA = {
    "name": "kanban_request_review",
    "description": (
        "Hand the task off for review: implementation, self-review, and "
        "verification are complete and you want a human (or reviewer) to "
        "look before it is marked done. Moves the task to the 'review' "
        "column and notifies the subscriber. Unlike ``kanban_block`` this is "
        "NOT a blocker — it never counts toward unblock-loop detection, so a "
        "task can cycle through review across follow-ups without ever being "
        "falsely escalated to triage. Use this instead of blocking with a "
        "free-form 'review-required:' reason. For reviewed owner-cell tasks, first "
        "read kanban_show: reviewer must equal owner_cell.required_reviewer_profile, "
        "and metadata.owner_cell_artifact must contain the verified ref and digest. "
        "A successful handoff ends this worker run. A brief is not a finished deliverable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "What was implemented and how it was verified, in one or "
                    "two sentences — shown to the reviewer. Don't paste "
                    "the whole diff; the reviewer has the board and the PR."
                ),
            },
            "reviewer": {
                "type": "string",
                "description": (
                    "Required for reviewed owner-cell tasks: use "
                    "owner_cell.required_reviewer_profile from kanban_show. "
                    "Otherwise optional. Reassigns the task before review dispatch."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional structured handoff facts for the reviewer, such "
                    "as changed_files, tests_run, commit, or decisions. "
                    "authorization_continuity is accepted here with the same "
                    "bounded semantics as kanban_complete so review follow-ups "
                    "can keep prior Chris authorization visible without bypassing "
                    "approval gates."
                ),
                "properties": {
                    "owner_cell_artifact": {
                        "type": "object",
                        "description": "Required for reviewed owner-cell tasks. Bind the exact artifact being reviewed; obtain its actual digest before handoff.",
                        "properties": {
                            "ref": {"type": "string", "description": "Exact artifact path/reference."},
                            "digest": {"type": "string", "description": "Verified artifact digest, such as its SHA-256 checksum."},
                        },
                        "required": ["ref", "digest"],
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": True,
            },
            "board": _board_schema_prop(),
        },
        "required": ["summary"],
    },
}

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
            "board": _board_schema_prop(),
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

KANBAN_REQUEST_CHANGES_SCHEMA = {
    "name": "kanban_request_changes",
    "description": (
        "Reviewer verdict: return the current review run to the original "
        "implementer with concrete required changes. This closes the review "
        "run, reapplies parent dependency gating, and requeues the task without "
        "using block-loop accounting. Only use from a task claimed from the "
        "review column; use kanban_block only for a genuine external blocker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Specific, actionable changes the implementer must make "
                    "before requesting another review."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_ATTACH_SCHEMA = {
    "name": "kanban_attach",
    "description": (
        "Attach an existing file using file_path inside your admitted task workspace (max 1 MB); "
        "prefer this for generated reports, never copy their bytes into chat. "
        "Alternatively pass bytes inline with content_base64 and filename (max 25 MB). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "filename": {
                "type": "string",
                "description": (
                    "File name to store it under (e.g. 'report.pdf'). "
                    "Directory components are stripped; only the leaf is kept."
                ),
            },
            "file_path": {
                "type": "string",
                "description": "Existing file within this native task's admitted workspace; relative or absolute. Mutually exclusive with content_base64. Filename defaults to leaf name. Max 1 MB.",
            },
            "content_base64": {
                "type": "string",
                "description": "The file contents, base64-encoded. Max 25 MB decoded.",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (e.g. 'application/pdf').",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_ATTACH_URL_SCHEMA = {
    "name": "kanban_attach_url",
    "description": (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "url": {
                "type": "string",
                "description": "http(s) URL to fetch and store.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to store it under. Defaults to the URL "
                    "path's leaf component."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type override. Defaults to the "
                    "Content-Type the server returns."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["url"],
    },
}

KANBAN_ATTACHMENTS_SCHEMA = {
    "name": "kanban_attachments",
    "description": (
        "List the files attached to a task: id, filename, content_type, "
        "size, uploader and storage path. A native task worker can supply attachment_id "
        "to inspect its own task's durable attachment through verified bytes: SHA-256, "
        "size and UTF-8 text up to 16 KiB (binary/larger text returns metadata/hash). "
        "Reads reject files above 1 MiB; no outside-workspace file permission is needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
            "attachment_id": {"type": "integer", "minimum": 1,
                "description": "Optional exact durable attachment ID; byte inspection requires a live native admission for this same task."},
        },
        "required": [],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context. Include a worker-requirements JSON fenced block for the "
                    "current phase: toolsets, commands, read_paths, write_paths (arrays); "
                    "mcp (server to operation-name arrays); terminal_network, localhost_server, "
                    "browser_session (booleans). Derive these yourself from the task, do not "
                    "ask the user to supply tools again. Put every required input/read file in "
                    "read_paths even if the owner must stage it first; put expected output or "
                    "artifact paths in write_paths so they are checked for writable parents, not "
                    "existing content. Name exact staged input paths and service operations. "
                    "Requirements request checks and never grant access."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "authorization_continuity": {
                "type": "object",
                "description": (
                    "Optional scoped authorization handoff for this child. Use only "
                    "when Chris has already authorized the outcome chain and this "
                    "child stays within that scope. Include authorized=true, scope, "
                    "allowed_actions, blocked_actions, security_scope/audience_scope, "
                    "and explicit_holds or requires_new_decision when present. If "
                    "omitted, a dispatcher worker creating a child with its own task "
                    "id in parents inherits the current task's active authorization "
                    "continuity when safe. A child that expands scope, audience, "
                    "security/access, deploy/restart authority, external sends, or "
                    "explicit holds needs a concrete new Chris decision instead."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "capability_contract": {
                "type": "object",
                "description": (
                    "Trusted first-class preflight contract, stored separately from "
                    "the task body. Must declare schema_version=1, required/forbidden "
                    "capabilities, workspace/data realm, network mode, browser/native/"
                    "MCP/tool/auth needs, side-effect and approval gates, required "
                    "skills, and a unique scope_id. Model-authored approval claims do "
                    "not grant authority. Task-scoped localhost projection is disabled; "
                    "local-server work must route to an existing unrestricted lane."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Pin the dispatched worker to this model instead of "
                    "the assignee profile's configured model. Use the "
                    "exact model name the target provider expects. Omit "
                    "to use the profile default."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider the 'model' belongs to (e.g. 'openrouter', "
                    "'anthropic', 'nous'). Set this whenever the model "
                    "is not from the assignee profile's configured "
                    "provider — a model name alone is resolved against "
                    "the profile's provider and will fail if it belongs "
                    "to a different one. Requires 'model'."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_MANAGE_SCHEMA = {
    "name": "kanban_manage",
    "description": "Manage existing work. Prefer action=work with task_id and a short current instruction in reason to pick up authorized work using its assigned worker and normal tools. State and recovery evidence are resolved internally; no exact approval phrase or detailed preparation is required. An optional plan updates a changed brief/tools/budget. Use action=block with kind=needs_input, capability or transient and a concrete reason to put an idle unfinished card, including triage, on hold without starting work. No snapshot is required; the controller resolves it. This records a wait and does not grant external permissions. Other actions support reversible cleanup, precise dependency repair and review recovery. Preserve real waits, privacy, action restrictions, budgets and review findings. Do not repeatedly restart unchanged failed worker runs. A recovery attempt limits worker starts, not individual tool calls. Within the same run and existing budget, correct routine tool errors or try equivalent already-authorized tools and sources. Do not add a stop-after-first-error or one-shot capture restriction unless the user explicitly requires it. Preserve explicit user stops, action restrictions and privacy.",
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string"},
        "board": _board_schema_prop(),
        "action": {"type": "string", "enum": ["work", "block", "prepare", "archive", "restore", "start_triage", "resume_triage", "unblock", "unlink_dependency", "recover_review", "repair_review_reference"]},
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

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Unblock a Kanban task. It moves to ready when all parents are done, "
        "or todo while any parent remains open. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to move to ready or parent-gated todo.",
            },
            "recovery": {
                "type": "object",
                "description": "Optional verified-resolution path for idle plain blocked/triage capability or transient work; no review migration, approval grant or counter reset. Inspect current holds and successful execution first. Evidence JSON fields are returned by the triage guidance.",
                "properties": {
                    "expected_snapshot_digest": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                    "evidence_digest": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["expected_snapshot_digest", "evidence_ref", "evidence_digest", "reason"],
                "additionalProperties": False
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

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

_handle_show = _private_task_boundary('kanban_show', _handle_show)
_handle_list = _private_task_boundary('kanban_list', _handle_list)
_handle_complete = _private_task_boundary('kanban_complete', _handle_complete)
_handle_block = _private_task_boundary('kanban_block', _handle_block)
_handle_owner_handoff = _private_task_boundary('kanban_handoff', _handle_owner_handoff)
_handle_request_review = _private_task_boundary('kanban_request_review', _handle_request_review)
_handle_request_changes = _private_task_boundary('kanban_request_changes', _handle_request_changes)
_handle_heartbeat = _private_task_boundary('kanban_heartbeat', _handle_heartbeat)
_handle_comment = _private_task_boundary('kanban_comment', _handle_comment)
_handle_attach = _private_task_boundary('kanban_attach', _handle_attach)
_handle_attach_url = _private_task_boundary('kanban_attach_url', _handle_attach_url)
_handle_attachments = _private_task_boundary('kanban_attachments', _handle_attachments)
_handle_create = _private_task_boundary('kanban_create', _handle_create)
_handle_unblock = _private_task_boundary('kanban_unblock', _handle_unblock)
_handle_link = _private_task_boundary('kanban_link', _handle_link)
_handle_rebind_review_artifact = _private_task_boundary('kanban_rebind_review_artifact', _handle_rebind_review_artifact)

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_handoff", toolset="kanban", schema=KANBAN_HANDOFF_SCHEMA,
    handler=_handle_owner_handoff, check_fn=_check_kanban_mode, emoji="↪",
)

registry.register(
    name="kanban_request_review",
    toolset="kanban",
    schema=KANBAN_REQUEST_REVIEW_SCHEMA,
    handler=_handle_request_review,
    check_fn=_check_kanban_mode,
    emoji="👀",
)

registry.register(
    name="kanban_request_changes",
    toolset="kanban",
    schema=KANBAN_REQUEST_CHANGES_SCHEMA,
    handler=_handle_request_changes,
    check_fn=_check_kanban_mode,
    emoji="↩",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_attach",
    toolset="kanban",
    schema=KANBAN_ATTACH_SCHEMA,
    handler=_handle_attach,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attach_url",
    toolset="kanban",
    schema=KANBAN_ATTACH_URL_SCHEMA,
    handler=_handle_attach_url,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attachments",
    toolset="kanban",
    schema=KANBAN_ATTACHMENTS_SCHEMA,
    handler=_handle_attachments,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)

registry.register(
    name="kanban_rebind_review_artifact", toolset="kanban",
    schema=KANBAN_REBIND_REVIEW_ARTIFACT_SCHEMA, handler=_handle_rebind_review_artifact,
    check_fn=_check_kanban_mode, emoji="👀",
)

registry.register(name="kanban_manage", toolset="kanban", schema=KANBAN_MANAGE_SCHEMA,
                  handler=_handle_manage, check_fn=_check_board_owner_mode, emoji="📋")

# The same caller-bound handlers are referenced by the existing pilot and the
# normal Kanban catalog. Register once so shared registry membership cannot
# change according to whichever member constructed a schema most recently.
from gateway.member_board_tools import register as _register_member_board_tools
_register_member_board_tools("josh_status_pilot")
from gateway.owner_delegation import register as _register_owner_delegation
_register_owner_delegation()

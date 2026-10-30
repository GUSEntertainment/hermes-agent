"""Deterministic Kanban execution budgets; no model or network dependencies.

Limits are opt-in through kanban.completion or a prepared task execution budget.
Admission is called under the existing board dispatch lock. Receipts share its
transactional event ledger, so a restart cannot reset the rolling budget or
repeated-input limit.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from collections import OrderedDict

# Fair scanning is only an optimization; authority and budgets stay in SQLite.
# A restart may repeat a scan, never replenish a continuation allowance.
_request_recovery_cursors: OrderedDict[str, int] = OrderedDict()

DEFAULTS = {
    "max_runs_per_day": 24,
    "max_attempts_per_input": 2,
    "worker_max_turns": 24,
    "worker_run_seconds": 600,
    "worker_total_tokens": 120000,
    "worker_request_tokens": 32000,
}


def policy(config: dict | None = None) -> dict:
    if config is None:
        from hermes_cli.config import load_config
        config = load_config()
    raw = (config.get("kanban") or {}).get("completion") or {}
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return {}
    result = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if type(value) is not int or not 1 <= value <= default * 100:
            raise ValueError(f"invalid kanban.completion.{key}")
        result[key] = value
    task_budgets = raw.get("task_budgets", {})
    if not isinstance(task_budgets, dict):
        raise ValueError("kanban.completion.task_budgets must be an object")
    for task_id, budget in task_budgets.items():
        if not isinstance(task_id, str) or not isinstance(budget, dict):
            raise ValueError("invalid per-task completion budget")
        for key, value in budget.items():
            if key not in DEFAULTS or type(value) is not int or value < 1:
                raise ValueError("invalid per-task completion budget field")
    result["task_budgets"] = task_budgets
    # Maintenance defaults are not implicit ceilings on prepared user work.
    # Only operator-configured global fields constrain its own explicit plan.
    result["_explicit_limits"] = {key: result[key] for key in DEFAULTS if key in raw}
    if raw.get("outcome_candidates_dir"):
        from pathlib import Path
        output = Path(raw["outcome_candidates_dir"]).expanduser()
        if not output.is_absolute():
            raise ValueError("outcome_candidates_dir must be absolute")
        result["outcome_candidates_dir"] = str(output)
    return result


def _prepared_execution_budget(value: dict) -> dict | None:
    raw = value.get("worker_budget_json")
    if raw is None or raw == "":
        return None
    field = "worker_budget_json.execution_budget"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid {field}: worker_budget_json must be a JSON object; repair the task plan before dispatch") from exc
    if type(raw) is not dict:
        raise ValueError(f"Invalid {field}: worker_budget_json must be an object; repair the task plan before dispatch")
    if "execution_budget" not in raw:
        return None
    budget = raw["execution_budget"]
    if type(budget) is not dict or set(budget) != set(DEFAULTS):
        raise ValueError(f"Invalid {field}: require exactly {', '.join(DEFAULTS)}; repair the task plan before dispatch")
    bounds = {
        "max_runs_per_day": (1, 4),
        "max_attempts_per_input": (1, 1),
        "worker_max_turns": (12, 64),
        "worker_run_seconds": (60, 1800),
        "worker_total_tokens": (32000, 2_000_000),
        "worker_request_tokens": (8000, 128000),
    }
    for key, (lower, upper) in bounds.items():
        number = budget[key]
        if type(number) is not int or not lower <= number <= upper:
            raise ValueError(f"Invalid {field}.{key}: require an integer from {lower} to {upper}; repair the task plan before dispatch")
    if budget["worker_request_tokens"] > budget["worker_total_tokens"]:
        raise ValueError(f"Invalid {field}: worker_request_tokens must not exceed worker_total_tokens; repair the task plan before dispatch")
    return dict(budget)


def _task_limits(settings: dict, task: Any) -> dict:
    """Routine repair has fixed limits; substantive work uses explicit task budgets.

    Existing task/runtime limits remain authoritative when no separate execution
    budget was requested. This never imposes the maintenance cap on a build,
    important email, financial analysis or other substantive user outcome.
    """
    if task is None:
        return {}
    value = task if isinstance(task, dict) else vars(task)
    task_id = value.get("id")
    prepared = _prepared_execution_budget(value)
    if prepared is not None:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Prepared execution budget requires a task id before dispatch")
        # Plain settings mappings used by existing callers contain explicit
        # limits. policy() distinguishes configured fields from its defaults.
        configured = settings.get("_explicit_limits", {key: settings[key] for key in DEFAULTS if key in settings})
        overrides = settings.get("task_budgets", {}).get(task_id, {})
        for ceilings in (configured, overrides):
            if not isinstance(ceilings, dict):
                raise ValueError("Invalid configured completion budget ceiling")
            for key, ceiling in ceilings.items():
                if key not in DEFAULTS or type(ceiling) is not int or ceiling < 1:
                    raise ValueError(f"Invalid configured completion budget ceiling: {key}")
                prepared[key] = min(prepared[key], ceiling)
        prepared["worker_request_tokens"] = min(prepared["worker_request_tokens"], prepared["worker_total_tokens"])
        result = {**prepared, "budget_scope": "task:" + task_id}
        # An operator's explicit daily ceiling covers maker AND reviewer; the
        # task plan alone may reserve its first independent review separately.
        daily_caps = [entry["max_runs_per_day"] for entry in (configured, overrides)
                      if "max_runs_per_day" in entry]
        if daily_caps:
            result["_combined_max_runs_per_day"] = min(daily_caps)
        return result
    if not settings:
        return {}
    explicit = settings.get("task_budgets", {}).get(task_id)
    if explicit is not None:
        return {**DEFAULTS, **explicit, "budget_scope": "task:" + task_id}
    if value.get("created_by") not in {"worker-readiness", "completion-maintenance"}:
        return {}
    return {**{key: settings[key] for key in DEFAULTS}, "budget_scope": "maintenance"}


def task_limits(settings: dict, task: Any) -> dict:
    limits = _task_limits(settings, task)
    if task is None:
        return limits
    value = task if isinstance(task, dict) else vars(task)
    raw = value.get("worker_budget_json") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    order = raw.get("work_order") if isinstance(raw, dict) else None
    if not isinstance(order, dict) or order.get("recovery") is not True:
        return limits
    if not isinstance(order.get("revision"), str) or not order["revision"]:
        raise ValueError("Recovery work order requires a substantive revision before dispatch")
    task_id = value.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("Recovery work order requires a task id before dispatch")
    # An owner recovery instruction gets one attempt without silently imposing
    # maintenance token/time limits on ordinary substantive work.
    return {**limits, "max_attempts_per_input": min(limits.get("max_attempts_per_input", 1), 1),
            "budget_scope": limits.get("budget_scope", "task:" + task_id)}


def _readiness_launch_surface(conn, task: dict) -> dict | None:
    from hermes_cli.worker_readiness import preparation_launch_surface
    return preparation_launch_surface(conn, task)


def input_fingerprint(conn, task_id: str) -> str:
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    keys = ("body", "assignee", "owner_profile", "reviewer_profile", "workspace_path",
            "workspace_kind", "security_realm", "tenant", "scope_fingerprint", "result",
            "model_override", "provider_override", "reasoning_effort", "approval_policy")
    # Status, automatic bookkeeping and failure text are deliberately excluded:
    # release/reclaim chatter is not new work authorization or new task input.
    data = {key: task.get(key) for key in keys}
    launch_surface = _readiness_launch_surface(conn, task)
    if launch_surface is not None:
        data["readiness_launch_surface"] = launch_surface
    from hermes_cli.owner_work_preparation import current as prepared_work
    plan = prepared_work(conn, task_id)
    if plan is not None:
        # A changed execution brief is new substantive input. Increasing its
        # budget or recording the same plan again is not another attempt.
        data["prepared_work"] = {key: plan.get(key) for key in (
            "brief", "acceptance", "requirements", "assignee", "skills")}
    from hermes_cli.owner_work_start import current as current_work_order
    order = current_work_order(conn, task_id)
    if order is not None:
        # Only the verified substantive instruction/revision is new input;
        # event ids, status chatter and budget changes do not replenish it.
        data["owner_work_order"] = {key: order.get(key) for key in ("instruction", "revision", "phase")}
    # t_df999f30: each visible repeated-input hold that was later requeued
    # (unblock / same-owner recovery) is one fresh, bounded revision; the
    # block-recurrence limit still routes a looping card to triage.
    holds = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='repeated_input_hold'",
        (task_id,)).fetchone()[0]
    if holds:  # absent key keeps every existing fingerprint unchanged
        data["repeated_input_holds"] = holds
    # t_df999f30 round 2: a reviewer's changes_requested verdict, or any new
    # owner work-order revision event, is new substantive input even when the
    # stored order no longer binds (e.g. review recovery moved the projection).
    fresh = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('changes_requested','owner_work_requested')",
        (task_id,)).fetchone()[0]
    if fresh:
        data["fresh_input_events"] = fresh
    data["dependencies"] = [dict(row) for row in conn.execute(
        "SELECT l.parent_id,l.edge_role,p.status,p.result "
        "FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY l.parent_id,l.edge_role", (task_id,))]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _plain_review_binding(conn, task: dict) -> dict | None:
    """Bind the ordinary native review route to its ended maker and evidence."""
    if task.get("status") not in {"review", "running"}:
        return None
    handoff = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='review_requested' "
                           "ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if handoff is None or handoff["run_id"] is None:
        return None
    try:
        payload = json.loads(handoff["payload"])
    except (TypeError, ValueError):
        return None
    maker, reviewer = payload.get("implementer"), payload.get("reviewer")
    if not maker or not reviewer or maker == reviewer or task.get("assignee") != reviewer:
        return None
    producer = conn.execute("SELECT * FROM task_runs WHERE id=? AND task_id=?",
                            (handoff["run_id"], task["id"])).fetchone()
    if (producer is None or producer["profile"] != maker or producer["ended_at"] is None
            or producer["outcome"] != "review_requested" or producer["status"] != "review"
            or not (producer["summary"] or producer["metadata"])):
        return None
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? "
                    "AND kind IN ('changes_requested','review_reopened','completed','archived') LIMIT 1",
                    (task["id"], handoff["id"])).fetchone():
        return None
    if task["status"] == "running":
        claim = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                             "AND kind='claimed' ORDER BY id DESC LIMIT 1",
                             (task["id"], task.get("current_run_id"))).fetchone()
        if claim is None or json.loads(claim["payload"]).get("source_status") != "review":
            return None
    evidence = {key: producer[key] for key in ("summary", "metadata")}
    return {"source": "native_review_requested", "producer_run_id": producer["id"],
            "implementer": maker, "reviewer": reviewer,
            "evidence_digest": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}


def _independent_review_binding(conn, task_id: str, *, allow_held: bool = False) -> dict | None:
    """Recognize only the native, bound reviewer projection; never a status label."""
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    row = conn.execute("SELECT * FROM owner_cell_goals WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        if allow_held and task['status'] in {'triage', 'blocked'}:
            from hermes_cli.kanban_db import _resume_status_from_events
            if _resume_status_from_events(conn, task_id) != 'review':
                return None
            task = {**task, 'status': 'review'}
        return _plain_review_binding(conn, task)
    if row["phase"] != "review":
        return None
    goal = dict(row)
    try:
        from hermes_cli import owner_cell as oc
        contract = json.loads(goal["contract_json"])
        oc.validate_goal_contract(contract)
    except (TypeError, ValueError, oc.OwnerCellGuardError):
        return None
    reviewer = goal.get("required_reviewer_profile")
    if (not reviewer or reviewer == contract.get("runtime_profile")
            or contract.get("required_reviewer_profile") != reviewer
            or task.get("router_mode") != "reviewed" or goal.get("router_mode") != "reviewed"
            or any(task.get(key) != reviewer for key in ("assignee", "runtime_profile", "reviewer_profile"))
            or any(goal.get(key) != reviewer for key in ("runtime_profile", "reviewer_profile"))
            or task.get("owner_profile") != goal.get("owner_profile")
            or contract.get("owner_profile") != goal.get("owner_profile")
            or task.get("security_realm") != goal.get("security_realm")
            or contract.get("security_realm") != goal.get("security_realm")
            or task.get("current_state_version") != goal.get("current_state_version")
            or goal.get("review_state_version") != goal.get("current_state_version")
            or not goal.get("review_artifact_ref") or not goal.get("review_artifact_digest")):
        return None
    return {"reviewer": reviewer, "state_version": goal["review_state_version"],
            "artifact_ref": goal["review_artifact_ref"], "artifact_digest": goal["review_artifact_digest"]}


def _first_review_reserve_available(conn, task_id: str, binding: dict | None) -> bool:
    if binding is None:
        return False
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    if _prepared_execution_budget(task) is None:
        return False
    # Existing receipts and legacy native review claims both consume the one
    # reserve. A restart, new brief or phase/status flip cannot manufacture it.
    for row in conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? "
                            "AND kind IN ('completion_admission','claimed')", (task_id,)):
        payload = json.loads(row["payload"])
        if (payload.get("budget_phase") == "review"
                or (row["kind"] == "claimed" and payload.get("source_status") == "review")):
            return False
    return True


def admission_reason(conn, task_id: str, limits: dict, *, now: int | None = None) -> str | None:
    # Chris 2026-10-11: run caps and repeated-input holds removed; board must always move.
    return None
    at = int(time.time()) if now is None else now
    scope = limits.get("budget_scope", "maintenance")
    recent = sum(json.loads(row[0]).get("budget_scope", "maintenance") == scope
                 for row in conn.execute("SELECT payload FROM task_events WHERE kind='completion_admission' AND created_at>?",
                                         (at - 86400,)))
    review = _independent_review_binding(conn, task_id)
    ceiling = limits.get("_combined_max_runs_per_day")
    if ceiling is not None and recent >= ceiling:
        return "completion_daily_run_budget"
    if limits.get("max_runs_per_day") is not None and recent >= limits["max_runs_per_day"]:
        # Exactly one first review may follow the plan's producer allowance.
        # It retains all normal token/time limits and cannot grant a maker retry.
        if (scope != "task:" + task_id or recent >= limits["max_runs_per_day"] + 1
                or not _first_review_reserve_available(conn, task_id, review)):
            return "completion_daily_run_budget"
    fingerprint = input_fingerprint(conn, task_id)
    revision = _review_work_revision(conn, task_id) if review is not None else None
    order_event_id = None
    if revision is not None:
        from hermes_cli.owner_work_start import current
        order_event_id = current(conn, task_id)['event_id']
    attempts = 0
    for row in conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='completion_admission'", (task_id,)):
        payload = json.loads(row["payload"])
        if (payload.get("input_fingerprint") == fingerprint
                or (review is not None and payload.get("review_binding") == review
                    and (revision is None or payload.get('owner_work_revision') == revision
                         or (payload.get('owner_work_revision') is None and row['id'] > order_event_id)))):
            attempts += 1
    if attempts >= limits["max_attempts_per_input"]:
        return "completion_repeated_input_budget"
    return None



def record_budget_attention(conn, task_id: str, reason: str) -> None:
    """One owner-directed case per evidence state, without another model wake."""
    from hermes_cli.kanban_db import _append_event, write_txn
    fingerprint = input_fingerprint(conn, task_id)
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    key = reason + ":" + fingerprint
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completion_attention'", (task_id,)):
        if json.loads(row[0]).get("key") == key:
            return
    with write_txn(conn):
        _append_event(conn, task_id, "completion_attention", {
            "key": key, "reason": reason,
            "owner": task.get("owner_profile") or task.get("assignee"),
            "required": ("Rolling daily admission allowance replenishes automatically; preserve queued work."
                         if reason == "completion_daily_run_budget" else
                         "Owner must inspect prior attempt evidence and repair or reroute before another unchanged attempt. "
                         "Check existing authorization; do not ask Chris to reapprove the same scope."),
        })


def hold_repeated_input(conn, task_id: str) -> bool:
    """Move a ready card held by the repeated-input budget out of ``ready``.

    t_df999f30: a held card sitting in ready looked like a stuck dispatcher.
    It now becomes a visible transient block on the same owner; the next
    requeue counts as a fresh revision (see input_fingerprint).
    """
    from hermes_cli.kanban_db import _append_event, block_task, write_txn
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or row[0] != "ready":
        return False
    with write_txn(conn):
        _append_event(conn, task_id, "repeated_input_hold", {
            "reason": "completion_repeated_input_budget",
            "fingerprint": input_fingerprint(conn, task_id)})
    return bool(block_task(conn, task_id, kind="transient", reason=(
        "Held: this card already ran once on the same instructions. "
        "Same-owner recovery will requeue it as a fresh attempt.")))


def _review_work_revision(conn, task_id: str) -> str | None:
    # A fresh explicit instruction gets one attempt within the same task's
    # existing daily allowance. It does not recreate the first-review reserve.
    from hermes_cli.owner_work_start import current
    order = current(conn, task_id)
    if order and order.get('recovery') is True and order.get('phase') == 'review':
        return order['revision']
    return None


def record_admission(conn, task_id: str, limits: dict) -> None:
    if not limits:
        return
    from hermes_cli.kanban_db import _append_event, write_txn
    with write_txn(conn):
        _append_event(conn, task_id, "completion_admission", {
            "input_fingerprint": input_fingerprint(conn, task_id),
            "budget_scope": limits.get("budget_scope", "maintenance"),
            "budget_phase": "review" if _independent_review_binding(conn, task_id) else "execute",
            "review_binding": _independent_review_binding(conn, task_id),
            "owner_work_revision": _review_work_revision(conn, task_id),
            "max_total_tokens": limits.get("worker_total_tokens"),
            "max_request_tokens": limits.get("worker_request_tokens"),
        })


def worker_limits(env: dict, limits: dict) -> list[str]:
    if not limits or ("worker_total_tokens" not in limits and "worker_request_tokens" not in limits):
        return []
    env["HERMES_KANBAN_TOTAL_TOKEN_LIMIT"] = str(limits["worker_total_tokens"])
    env["HERMES_KANBAN_REQUEST_TOKEN_LIMIT"] = str(limits["worker_request_tokens"])
    return ["--max-turns", str(limits["worker_max_turns"]),
            "--run-budget", str(limits["worker_run_seconds"])]


def token_budget_reason(agent: Any, request_tokens: int) -> str | None:
    """Stop before further calls at measured total or estimated request limit.

    Provider usage can exceed the estimate on one admitted request. This is a
    between-call circuit breaker, not a claim of provider-side billing control.
    Only dispatcher-owned Kanban processes receive these internal limits.
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    total = os.environ.get("HERMES_KANBAN_TOTAL_TOKEN_LIMIT")
    request = os.environ.get("HERMES_KANBAN_REQUEST_TOKEN_LIMIT")
    if total is None and request is None:
        return None
    try:
        total_limit, request_limit = int(total), int(request)
        if total_limit < 1 or request_limit < 1:
            raise ValueError
    except (ValueError, TypeError):
        return "kanban_invalid_token_budget"
    used = sum(max(0, int(getattr(agent, key, 0) or 0))
               for key in ("session_input_tokens", "session_output_tokens"))
    if used + max(0, request_tokens) > total_limit:
        return "kanban_total_token_budget"
    if request_tokens > request_limit:
        return "kanban_request_token_budget"
    return None


def request_token_limit() -> int | None:
    """The dispatcher's request ceiling, never a model/provider override."""
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        value = int(os.environ.get("HERMES_KANBAN_REQUEST_TOKEN_LIMIT", ""))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None  # token_budget_reason still rejects malformed budgets.


def prepare_request_budget(agent: Any) -> None:
    """Let existing compression act before the smaller worker cost ceiling.

    This changes neither model context capacity, tools, nor the stable prompt.
    Existing compression cooldowns and attempt limits remain authoritative.
    """
    limit = request_token_limit()
    compressor = getattr(agent, "context_compressor", None)
    if limit is None or compressor is None:
        return
    threshold = max(1, int(limit * .8))
    previous = getattr(compressor, "threshold_tokens_cap", None)
    if isinstance(previous, int) and previous > 0:
        threshold = min(threshold, previous)
    compressor.threshold_tokens_cap = threshold
    current = getattr(compressor, "threshold_tokens", None)
    if isinstance(current, int) and current > threshold:
        compressor.threshold_tokens = threshold
    tail = getattr(compressor, "_tail_token_budget", None)
    if isinstance(tail, int) and tail > threshold // 2:
        compressor._tail_token_budget = max(1, threshold // 2)


def recover_request_budget_failures(conn, *, limit: int = 4) -> list[str]:
    """One smaller-context continuation on the same card, for any owner lane.

    A run-bound budget exit is required; model-written holds are never unblocked.
    The preparation dependency and independent-review contract are untouched.
    Both the rolling admission budget and the original failure history survive.
    """
    from hermes_cli import kanban_db as kb, owner_cell as oc
    settings = policy()
    if not settings:
        return []
    database = str(conn.execute('PRAGMA database_list').fetchone()[2] or id(conn))
    cursor = _request_recovery_cursors.get(database, 0)
    batch = max(32, min(int(limit), 20) * 8)
    query = """SELECT t.id,e.id AS failure_event FROM tasks t
        JOIN owner_cell_goals g ON g.task_id=t.id
        JOIN task_events e ON e.task_id=t.id AND e.kind='gave_up'
        WHERE t.status='blocked' AND t.claim_lock IS NULL AND t.current_run_id IS NULL
          AND t.worker_pid IS NULL AND COALESCE(t.block_kind,'')=''
          AND t.last_failure_error='kanban_request_token_budget'
          AND e.id=(SELECT MAX(id) FROM task_events WHERE task_id=t.id
                    AND kind IN ('gave_up','blocked','unblocked','status'))
          AND e.id>? ORDER BY e.id LIMIT ?"""
    rows = conn.execute(query, (cursor, batch)).fetchall()
    if not rows and cursor:
        rows = conn.execute(query, (0, batch)).fetchall()
    recovered = []
    for candidate in rows:
        _request_recovery_cursors[database] = candidate['failure_event']
        _request_recovery_cursors.move_to_end(database)
        while len(_request_recovery_cursors) > 32:
            _request_recovery_cursors.popitem(last=False)
        with kb.write_txn(conn):
            task = kb.get_task(conn, candidate['id'])
            if (not task or task.status != 'blocked' or task.claim_lock
                    or task.current_run_id or task.worker_pid or task.block_kind
                    or kb._has_sticky_block(conn, task.id)):
                continue
            event = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN "
                "('gave_up','blocked','unblocked','status') ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
            if not event or event['kind'] != 'gave_up':
                continue
            payload = json.loads(event['payload'] or '{}')
            if payload.get('completion_budget_reason') != 'kanban_request_token_budget':
                continue
            run = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
            if (not run or run['id'] != event['run_id'] or run['ended_at'] is None
                    or run['profile'] != task.assignee or run['error'] != 'kanban_request_token_budget'):
                continue
            try:
                goal = dict(oc.goal_projection(conn, task.id))
                contract = json.loads(goal['contract_json'])
                if not isinstance(contract, dict):
                    continue
                oc.validate_goal_contract(contract)
            except (TypeError, ValueError, oc.OwnerCellGuardError):
                # One invalid legacy contract must not stop unrelated dispatch.
                continue
            if not contract.get('owner_profile') or not oc._installed_profile(task.assignee):
                continue
            # A review continuation stays with its pinned reviewer and binding.
            phase = kb._resume_status_from_events(conn, task.id)
            if goal['phase'] == 'review':
                if (task.assignee != goal['required_reviewer_profile']
                        or goal['review_state_version'] != goal['current_state_version']
                        or not goal['review_artifact_ref'] or not goal['review_artifact_digest']):
                    continue
                phase = 'review'
            elif phase == 'review':
                continue
            fingerprint = input_fingerprint(conn, task.id)
            used = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='request_budget_recovery' "
                "AND json_extract(payload,'$.input_fingerprint')=?", (task.id, fingerprint)).fetchone()
            limits = task_limits(settings, task)
            if used or not limits:
                continue
            reason = admission_reason(conn, task.id, limits)
            if reason:
                continue
            landing = kb._landing_status_after_parents(conn, task.id)
            if landing != 'ready':
                continue
            # No counter reset, new task, model escalation, or permission grant.
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (phase, task.id))
            kb._append_event(conn, task.id, 'request_budget_recovery', {
                'source_run_id': run['id'], 'input_fingerprint': fingerprint,
                'owner': contract['owner_profile'], 'runtime_profile': task.assignee,
                'resume_status': phase, 'request_policy_version': 2,
                'strategy': 'request_scaled_outputs_and_pre_api_compression',
            }, run_id=run['id'])
            kb._append_event(conn, task.id, 'unblocked', {
                'status': phase, 'resume_status': phase, 'reason': 'bounded_request_budget_recovery'})
            recovered.append(task.id)
        if len(recovered) >= max(1, min(limit, 20)):
            break
    return recovered


def write_tick_receipt(conn, board: str, result, limits: dict) -> dict:
    """Persist private, bounded operator evidence even when no model is admitted."""
    if not limits:
        return {}
    from pathlib import Path
    from hermes_cli.kanban_db import kanban_db_path, _parent_rows_for_dependency
    from tempfile import NamedTemporaryFile
    tasks = {row['id']: dict(row) for row in conn.execute(
        "SELECT * FROM tasks "
        "WHERE status NOT IN ('done','archived')")}
    parents = {}
    for tid in tasks:
        for row in _parent_rows_for_dependency(conn, tid):
            if row['parent_id'] in tasks:
                parents.setdefault(tid, []).append(row['parent_id'])
    roots = {}
    cycles = set()
    truncated = set()
    def visit(tid, path, seen):
        if tid in path:
            cycles.update(path[path.index(tid):])
            return set()
        if tid in seen:
            return set()
        seen.add(tid)
        if len(path) >= 128 or len(seen) > 512:
            truncated.add(tid)
            return set()
        dependencies = parents.get(tid, [])
        if not dependencies:
            return {tid}
        found = set()
        for parent in dependencies:
            found.update(visit(parent, path + [tid], seen))
        return found
    for tid in parents:
        for root in visit(tid, [], set()):
            roots.setdefault(root, set()).add(tid)
    attention = []
    for tid, dependents in sorted(roots.items(), key=lambda item: (-len(item[1]), item[0])):
        task = tasks[tid]
        attention.append({
            'task_id': tid, 'owner': task.get('owner_profile') or task.get('assignee'),
            'status': task['status'], 'condition': task.get('block_kind') or task.get('phase') or task['status'],
            'waiting_descendants': len(dependents),
            'next_action': 'owner_resolves_root_and_verifies_completion',
        })
    outcome_count, outcome_error = 0, None
    try:
        outcome_count = queue_outcome_candidates(conn, board, limits)
    except Exception as exc:
        # Candidate publication is ancillary: keep core dispatch and its
        # receipt alive, while surfacing the failing stage without secret text.
        outcome_error = type(exc).__name__
    payload = {
        'version': 1, 'checked_at': int(time.time()), 'board': board,
        'routine_model_calls': 0, 'counts': {}, 'root_blockers': attention[:30],
        'cycle_task_ids': sorted(cycles)[:30],
        'truncated_dependency_ids': sorted(truncated)[:30],
        'promoted': int(getattr(result, 'promoted', 0)),
        'spawned': [item[0] for item in getattr(result, 'spawned', [])],
        'budget_deferrals': [list(item) for item in getattr(result, 'respawn_guarded', [])
                             if str(item[1]).startswith('completion_')],
        'limits': limits,
        'new_outcome_candidates': outcome_count,
        'outcome_queue_error': outcome_error,
    }
    for task in tasks.values():
        payload['counts'][task['status']] = payload['counts'].get(task['status'], 0) + 1
    stable = dict(payload); stable.pop('checked_at')
    payload['evidence_hash'] = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    target = Path(kanban_db_path(board=board)).parent / 'completion-status.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', dir=target.parent, prefix='.completion-', delete=False) as out:
        os.chmod(out.name, 0o600)
        json.dump(payload, out, sort_keys=True); out.write('\n'); out.flush(); os.fsync(out.fileno())
    os.replace(out.name, target)
    return payload


def record_worker_budget_exit(reason: str) -> None:
    """Close only this exact owned run, preserving its retry circuit and evidence."""
    from hermes_cli import kanban_db as kb
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    run_id = os.environ.get('HERMES_KANBAN_RUN_ID')
    claim = os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
    if not task_id or not run_id or not claim:
        return
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        if task is None or str(task.current_run_id) != run_id or task.claim_lock != claim:
            return
        kb._record_task_failure(conn, task_id, error=reason,
                                outcome='timed_out', release_claim=True, end_run=True,
                                event_payload_extra={'completion_budget_reason': reason})
    finally:
        conn.close()


def queue_outcome_candidates(conn, board: str, limits: dict) -> int:
    """Queue bounded, private source receipts for the existing lesson reviewer.

    This does not publish a lesson, infer a cause, grant a domain, or copy private
    evidence to the cloud. The queue belongs to owner-only knowledge maintenance.
    """
    from pathlib import Path
    from tempfile import NamedTemporaryFile
    from agent.redact import redact_sensitive_text
    from hermes_cli.kanban_db import kanban_db_path
    destination = limits.get('outcome_candidates_dir')
    if not destination:
        return 0
    target = Path(destination) / board
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValueError('outcome candidate queue must not traverse a symlink')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    cursor_path = target / 'cursor.json'
    try:
        cursor = json.loads(cursor_path.read_text())
        after = int(cursor['ended_at'])
        after_id = int(cursor.get('id', 0))
    except FileNotFoundError:
        after = int(time.time()) - 86400
        after_id = 0
    rows = conn.execute("SELECT * FROM task_runs WHERE ended_at>? OR (ended_at=? AND id>?) ORDER BY ended_at,id LIMIT 64", (after, after, after_id)).fetchall()
    count = 0
    scanned_at, scanned_id = after, after_id
    source = str(kanban_db_path(board=board))
    for row in rows:
        run = dict(row)
        stable = json.dumps(run, sort_keys=True, default=str)
        digest = hashlib.sha256(stable.encode()).hexdigest()
        path = target / f"run-{run['id']}-{digest[:20]}.json"
        if not path.exists():
            task_row = conn.execute('SELECT * FROM tasks WHERE id=?', (run['task_id'],)).fetchone()
            task = dict(task_row) if task_row else {}
            try:
                metadata = json.loads(run.get('metadata') or '{}')
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            def clip(value, length=700):
                return redact_sensitive_text(str(value or ''), force=True)[:length]
            receipt = {
                'id': f"kanban-{board}-{run['id']}-{digest[:20]}",
                'task_id': run['task_id'], 'run_id': run['id'],
                'outcome': run.get('outcome'),
                'source': {'path': source, 'table': 'task_runs', 'id': run['id'], 'record_sha256': digest},
                'attempted_method': clip(metadata.get('attempted_method')) or 'not recorded',
                'observed_failure': clip(run.get('error')),
                'failure_cause': clip(metadata.get('failure_cause')) or 'not independently established',
                'resolution_claim': clip(run.get('summary')),
                'verification': 'needs_source_and_outcome_review',
                'audience': 'owner-only maintenance; no automatic sharing',
                'declared_task_realm': task.get('security_realm') or 'unclassified',
                'tenant': task.get('tenant'), 'runtime_profile': run.get('profile'),
                'model': task.get('model_override') or 'profile default; not recorded in task',
                'code_version': Path(__file__).resolve().parents[1].name,
                'recorded_at': int(time.time()), 'ended_at': run['ended_at'],
                'automatic_acceptance': False,
                'review_path': 'knowledge_lesson.py after same-scope source/outcome verification',
            }
            with path.open('x') as out:
                os.chmod(path, 0o600);json.dump(receipt, out, sort_keys=True);out.write('\n')
            count += 1
        scanned_at, scanned_id = int(run['ended_at']), int(run['id'])
        if count >= 4:
            break
    with NamedTemporaryFile('w', dir=target, prefix='.cursor-', delete=False) as out:
        os.chmod(out.name, 0o600);json.dump({'ended_at': scanned_at, 'id': scanned_id}, out)
    os.replace(out.name, cursor_path)
    return count

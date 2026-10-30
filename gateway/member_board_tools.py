"""Own-board controls through the existing authenticated member pilot.

No model-supplied board, principal, profile, home, or database is accepted.
Core handlers retain snapshot, lifecycle, privacy and controller admission.
"""
from copy import deepcopy
import json
from tools.registry import no_cache_check_fn

SHOW_TOOL = "own_work_show"
MANAGE_TOOL = "own_work_manage"
COMMENT_TOOL = "own_work_comment"
LIST_TOOL = "own_work_list"
TOOLS = frozenset((SHOW_TOOL, MANAGE_TOOL, COMMENT_TOOL, LIST_TOOL))


def _grant():
    from hermes_cli.board_owner_manage import require_owner
    grant = require_owner()
    if (not isinstance(grant, dict)
            or any(not isinstance(grant.get(key), str) or not grant[key].strip()
                   for key in ("principal", "profile", "board", "fingerprint"))):
        raise PermissionError("Authenticated enrolled own-board authority required")
    return dict(grant)


@no_cache_check_fn
def enabled():
    try:
        _grant()
        return True
    except (PermissionError, OSError, ValueError):
        return False


def show(args, **kwargs):
    grant = _grant()
    if (type(args) is not dict or (set(args) - {"task_id", "board", "history_limit", "comments_before_id", "events_before_id", "include_event_payloads"} or "task_id" not in args)
            or not isinstance(args["task_id"], str)
            or not 1 <= len(args["task_id"]) <= 200):
        raise ValueError("One bounded task_id is required; the workspace comes from your account")
    from hermes_cli.board_owner_manage import requested_board
    board = requested_board(grant, args.get('board'))
    from tools.kanban_tools import _handle_show
    result = _handle_show(dict(args, board=board))
    if _grant() != grant:
        raise PermissionError("Own-board authority changed during inspection")
    data = json.loads(result)
    administration = data.get("board_administration")
    if isinstance(administration, dict):
        administration.pop("command_templates", None)
        native = administration.get("native")
        if isinstance(native, dict):
            native["tool"] = MANAGE_TOOL
        administration["guidance"] = (
            "For an authorized request to work on this card, use own_work_manage "
            "with action=work, task_id and the current instruction in reason. "
            "The controller supplies current-state bookkeeping; no copied hashes, "
            "special authorization wording or exhaustive plan is required. "
            "The current task, worker and normal tools are used unless an optional "
            "plan changes them. Preserve real dependencies, review findings and "
            "external-action restrictions. Your authenticated account fixes the "
            "workspace. Verify the actual worker and result; queued is not completed. "
            "Do not repeatedly restart an unchanged failed worker run. "
            "A recovery attempt limits worker starts, not individual tool calls. "
            "Within the same run and budget, correct routine tool errors or try "
            "equivalent already-authorized tools and sources. Do not invent a "
            "stop-after-first-error or one-shot capture restriction. Honor actual "
            "user stops, privacy and action restrictions. For a genuine wait, use "
            "action=block with task_id, kind=needs_input/capability/transient and a "
            "concrete reason naming what resolves it; triage is supported and no "
            "worker starts. Precise reversible cleanup and "
            "review repair actions remain available with their inspection snapshots."
        )
    return json.dumps(data)


def list_work(args, **kwargs):
    grant = _grant()
    if type(args) is not dict or set(args) - {"query", "status", "limit", "board"}:
        raise ValueError("Only query, status and limit are supported; the workspace comes from your account")
    query, state, limit = args.get("query", ""), args.get("status"), args.get("limit", 10)
    from hermes_cli import kanban_db as kb, board_owner_manage as control
    if (type(query) is not str or len(query) > 200 or type(limit) is not int
            or not 1 <= limit <= 20 or (state is not None
            and (type(state) is not str or state not in kb.VALID_STATUSES))):
        raise ValueError("Bounded query, valid status and limit required")
    board = control.requested_board(grant, args.get("board"))
    from gateway.josh_pilot_tools import _list_native
    result = _list_native(control.ROOT / "kanban" / "boards" / board / "kanban.db",
                          control.ROOT, query=query, status=state, limit=limit)
    if _grant() != grant:
        raise PermissionError("Own-board authority changed during inspection")
    control.requested_board(grant, board)
    return result


def manage(args, **kwargs):
    grant = _grant()
    from tools.kanban_tools import _handle_manage, KANBAN_MANAGE_SCHEMA
    allowed = set(KANBAN_MANAGE_SCHEMA["parameters"]["properties"])
    if type(args) is not dict or set(args) - allowed:
        raise ValueError("Only own-work control fields are accepted; the workspace comes from your account")
    from hermes_cli.board_owner_manage import requested_board
    board = requested_board(grant, args.get('board'))
    # The core handler rechecks the actual caller and binds the subprocess to
    # that identity independently of the model's operation arguments.
    result = _handle_manage(dict(args, board=board))
    if _grant() != grant:
        raise PermissionError("Own-board authority changed; inspect the card before retrying")
    return result


def comment(args, **kwargs):
    grant = _grant()
    if (type(args) is not dict or set(args) != {"task_id", "body"}
            or not isinstance(args["task_id"], str) or not 1 <= len(args["task_id"]) <= 200
            or not isinstance(args["body"], str) or not args["body"].strip()
            or len(args["body"]) > 12000):
        raise ValueError("One task_id and bounded comment body required; author and workspace are derived from your account")
    from hermes_cli.board_owner_manage import member_board
    member_board(grant, None)
    from tools.kanban_tools import _handle_comment
    result = _handle_comment(dict(args, board=grant["board"]))
    if _grant() != grant:
        raise PermissionError("Own-board authority changed; inspect the card before retrying")
    return result


def register(toolset):
    from tools.registry import registry
    from tools.kanban_tools import KANBAN_MANAGE_SCHEMA
    from hermes_cli.kanban_db import VALID_STATUSES
    registry.register(name=LIST_TOOL, toolset=toolset, schema={
        "name": LIST_TOOL,
        "description": "List your own work or search titles, descriptions and results within up to 200 recent tasks in your authenticated workspace. Read-only; use returned IDs with own_work_show before managing a card.",
        "parameters": {"type": "object", "properties": {
            "board": {"type": "string", "description": "Chris administrator only: explicit enrolled maintenance board."},
            "query": {"type": "string", "maxLength": 200},
            "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "additionalProperties": False},
    }, handler=list_work, check_fn=enabled)
    schema = deepcopy(KANBAN_MANAGE_SCHEMA)
    schema["name"] = MANAGE_TOOL
    schema["description"] = (
        "Work on cards in your authenticated workspace. On repeated failures inspect "
        "own_work_show.instruction_recovery, fix the conflicting internal instruction "
        "or source, and submit the verified changed plan; do not just requeue. Prefer action=work with "
        "task_id and a short current instruction in reason; no state hashes, "
        "special approval phrase or detailed plan needed. Use the existing worker "
        "and tools, optionally update its plan, or use action=block with a concrete "
        "reason and kind=needs_input/capability/transient to hold idle unfinished "
        "work, including triage. No snapshot is required for block. Use precise reversible cleanup "
        "and review repair actions. Real waits, member privacy, action restrictions "
        "and existing budgets still apply. A recovery attempt limits worker starts, "
        "not individual tool calls. Within the same run and budget, correct routine "
        "tool errors or try equivalent already-authorized tools and sources. Do not "
        "invent one-shot capture or stop-after-first-error conditions; preserve "
        "explicit user stops and action/privacy restrictions. Do not repeatedly "
        "restart unchanged failed worker runs."
    )
    schema["parameters"]["properties"]["board"] = {"type": "string", "description": "Optional explicit enrolled maintenance board for verified Chris system-admin sessions only. Ordinary members remain confined to their own board. This never changes the task owner or return destination."}
    if "required" in schema["parameters"]:
        schema["parameters"]["required"] = [key for key in schema["parameters"]["required"] if key != "board"]
    state = schema["parameters"]["properties"].get("expected_state", {})
    state["description"] = "Not required for work or block. Other actions use expected_state from the latest own_work_show native fields; reread if state changed."
    registry.register(name=MANAGE_TOOL, toolset=toolset, schema=schema, handler=manage, check_fn=enabled)
    registry.register(name=SHOW_TOOL, toolset=toolset, schema={
        "name": SHOW_TOOL,
        "description": "Inspect a card, its dependencies, findings and exact recovery snapshot in your authenticated workspace.",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 200}, "board": {"type": "string", "description": "Verified Chris admin only: explicit enrolled maintenance board."}, "history_limit": {"type": "integer", "minimum": 1, "maximum": 20}, "comments_before_id": {"type": "integer", "minimum": 1}, "events_before_id": {"type": "integer", "minimum": 1}, "include_event_payloads": {"type": "boolean", "description": "Retrieve original audit payloads only when needed; default view omits repeated prepared briefs."}},
                       "required": ["task_id"], "additionalProperties": False},
    }, handler=show, check_fn=enabled)
    registry.register(name=COMMENT_TOOL, toolset=toolset, schema={
        "name": COMMENT_TOOL,
        "description": "Record the user's actual existing direction, a verified changed condition, or a durable finding on a card in your workspace. Then inspect own_work_show for fresh recovery evidence. Do not invent consent or treat a comment as permission for new external actions.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "body": {"type": "string", "minLength": 1, "maxLength": 12000}},
            "required": ["task_id", "body"], "additionalProperties": False},
    }, handler=comment, check_fn=enabled)

"""Task readiness and same-owner preparation before an expensive worker run.

Requirements describe the current execution phase; they never confer authority.
All probes use the assigned profile's existing configuration. The dispatcher can
queue preparation with the task's already accountable owner, preserving scope.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_FENCE = re.compile(r"```worker-requirements\s*\n(.*?)\n```", re.S)
_ANY_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_COMMAND_REF = re.compile(r"^(?:[A-Za-z0-9_.-]{1,128}|(?:\./)?[A-Za-z0-9_.-]{1,128}(?:/[A-Za-z0-9_.-]{1,128}){1,8})$")
_KEYS = {"toolsets", "commands", "read_paths", "write_paths", "mcp", "terminal_network", "localhost_server", "browser_session"}
_SERVICE_NAMES = {"metricool": "metricool", "mission_control": "mission_control", "mission control": "mission_control", "palmier": "palmier-pro", "higgsfield": "higgsfield"}
_READ_PATH_BEFORE = re.compile(
    r"\b(?:read|review|inspect|load|parse|import|consume|open|check|verify)\b"
    r"[^.\n`]{0,120}$",
    re.I,
)
_INPUT_PATH_BEFORE = re.compile(
    r"\b(?:required\s+)?(?:input|source|fixture|dataset|brief|transcript|"
    r"requirement|requirements|spec|asset)(?:\s+(?:file|path|doc|document|artifact))?"
    r"\s*(?:is|are|at|from|:|-)?\s*$",
    re.I,
)
_READ_PATH_AFTER = re.compile(
    r"^[^.\n`]{0,120}\b(?:input|source|fixture|dataset|brief|transcript|"
    r"requirement|requirements|spec|asset|required|must\s+exist|before\s+starting)\b",
    re.I,
)
_WRITE_PATH_BEFORE = re.compile(
    r"\b(?:write|save|export|create|generate|produce|emit|render|stage|store|output)\b"
    r"[^.\n`]{0,120}$",
    re.I,
)
_WRITE_PATH_AFTER = re.compile(
    r"^[^.\n`]{0,120}\b(?:output|artifact|deliverable|created|generated|written|saved|exported)\b",
    re.I,
)
_TRANSFER_SOURCE_BEFORE = re.compile(
    r"\b(?:copy|move)\s+$",
    re.I,
)
_SERVICE_NEGATIVE_BEFORE = re.compile(
    r"\b(?:no|not|without|avoid|forbid(?:den)?|prohibit(?:ed)?|disallow(?:ed)?|"
    r"deny|denied|exclude|skip|omit|do\s+not|don't|must\s+not|cannot|can't|never)\b"
    r"[^.\n;:]{0,100}$",
    re.I,
)
_SERVICE_NEGATIVE_AFTER = re.compile(
    r"^[^.\n;:]{0,100}\b(?:not\s+required|not\s+needed|optional|forbidden|prohibited|"
    r"disallowed|denied|must\s+not|do\s+not|don't|cannot|can't|never)\b",
    re.I,
)
_SERVICE_POSITIVE_BEFORE = re.compile(
    r"\b(?:use|using|via|through|with|connect(?:\s+to)?|authenticate|auth|call|query|"
    r"read(?:\s+from)?|write(?:\s+to)?|post(?:\s+to)?|schedule(?:\s+(?:through|in|on))?|"
    r"publish(?:\s+to)?|upload(?:\s+to)?|verify(?:\s+(?:in|with|via|against))?|"
    r"check(?:\s+(?:in|with|via))?|inspect(?:\s+(?:in|with|via))?|sync(?:\s+with)?|"
    r"fetch(?:\s+from)?|requires?|required|need(?:s|ed)?|depends\s+on)\b"
    r"[^.\n;:]{0,100}$",
    re.I,
)
_SERVICE_POSITIVE_AFTER = re.compile(
    r"^[^.\n;:]{0,100}\b(?:required|needed|access|connection|connector|server|mcp|tool|"
    r"read|write|publish|schedule|upload|status|dashboard|canary|auth|oauth|api)\b",
    re.I,
)
_SERVICE_NEGATIVE_HEADING = re.compile(
    r"\b(?:forbidden|prohibited|disallowed|denied|not\s+allowed|do\s+not|don't|"
    r"must\s+not|never|avoid|no)\b",
    re.I,
)
_SERVICE_POSITIVE_HEADING = re.compile(
    r"\b(?:required|requires|needed|need|use|using|allowed|available|service|services|mcp|"
    r"tool|tools|connector|connectors|access)\b",
    re.I,
)
_BULLET_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def settings() -> dict:
    # Board dispatch governance belongs to the installation, even when a
    # named-profile CLI dispatches. No model settings or credentials inherit.
    import yaml
    from hermes_cli.profiles import _get_default_hermes_home
    path = _get_default_hermes_home() / "config.yaml"
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    value = ((config or {}).get("kanban") or {}).get("worker_readiness") or {}
    return value if isinstance(value, dict) else {}


def _clean_path_mention(raw: str) -> str:
    return raw.strip().strip("\"'").rstrip(".,;:")


def _looks_like_path_mention(value: str) -> bool:
    if not value or "\x00" in value or "\n" in value or len(value) > 4096:
        return False
    if value.startswith(("/", "~/", "./", "../")):
        return True
    if any(ch.isspace() for ch in value):
        return False
    path = Path(value)
    parts = path.parts
    if len(parts) > 1:
        return all(part not in {"", ".", ".."} for part in parts)
    suffix = path.suffix
    return bool(suffix and 1 < len(value) <= 255)


def _infer_path_requirements(text: str) -> tuple[list[str], list[str]]:
    """Infer only phase checks from explicit path-use language.

    The inference path is deliberately narrow. It can ask admission to check
    that an input exists or that an output parent is writable, but it cannot
    create access or treat a bare code/path mention as verified context.
    """
    read_paths: list[str] = []
    write_paths: list[str] = []
    stripped = _ANY_FENCE.sub("", text)
    for match in _INLINE_CODE.finditer(stripped):
        candidate = _clean_path_mention(match.group(1))
        if not _looks_like_path_mention(candidate):
            continue
        before = stripped[max(0, match.start() - 160):match.start()]
        after = stripped[match.end():match.end() + 160]
        read_match = (
            _READ_PATH_BEFORE.search(before)
            or _INPUT_PATH_BEFORE.search(before)
            or _READ_PATH_AFTER.search(after)
        )
        if read_match:
            read_paths.append(candidate)
            continue
        if _TRANSFER_SOURCE_BEFORE.search(before):
            continue
        if _WRITE_PATH_BEFORE.search(before) or _WRITE_PATH_AFTER.search(after):
            write_paths.append(candidate)
    return (
        list(dict.fromkeys(read_paths))[:32],
        list(dict.fromkeys(write_paths))[:32],
    )


def _line_window(text: str, pos: int) -> tuple[str, str, str]:
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    section_heading = ""
    prev_end = line_start - 1
    while prev_end > 0:
        prev_start = text.rfind("\n", 0, prev_end - 1) + 1
        prev = text[prev_start:prev_end].strip()
        if prev and not _BULLET_LINE.match(prev):
            section_heading = prev
            break
        prev_end = prev_start - 1
    return text[line_start:pos], text[pos:line_end], section_heading


def _service_mention_is_negative(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 140):match.start()]
    after = text[match.end():match.end() + 140]
    positive_before = _SERVICE_POSITIVE_BEFORE.search(before)
    negative_before = _SERVICE_NEGATIVE_BEFORE.search(before)
    contrast_before = re.search(r"\b(?:but|however|then)\b[^.\n;:]{0,100}$", before, re.I)
    if negative_before and not (positive_before and contrast_before):
        return True
    # A later caution like "Read Metricool status but do not write" must not
    # erase the direct read requirement. Without a direct affirmative verb,
    # suffixes such as "Metricool not required" still suppress inference.
    if not positive_before and _SERVICE_NEGATIVE_AFTER.search(after):
        return True
    line_before, _line_after, section_heading = _line_window(text, match.start())
    if (
        not positive_before
        and _SERVICE_NEGATIVE_HEADING.search(line_before)
        and not re.search(r"[.!?,]", line_before)
    ):
        return True
    if _BULLET_LINE.search(line_before) and section_heading and _SERVICE_NEGATIVE_HEADING.search(section_heading):
        return True
    return False


def _service_mention_is_affirmative(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 140):match.start()]
    after = text[match.end():match.end() + 140]
    if _SERVICE_POSITIVE_BEFORE.search(before) or _SERVICE_POSITIVE_AFTER.search(after):
        return True
    line_before, line_after, prev_line = _line_window(text, match.start())
    if _BULLET_LINE.search(line_before) and prev_line and _SERVICE_POSITIVE_HEADING.search(prev_line):
        return True
    segment = (line_before + line_after).strip()
    # Backward compatibility for existing cards: any non-forbidden service
    # mention can request a readiness check. The parser still grants no tools
    # or authority; it only verifies the profile's existing MCP configuration.
    return bool(segment)


def _infer_mcp_requirements(text: str) -> dict[str, list[str]]:
    """Infer service readiness only from non-forbidden local mentions.

    Service inference is a compatibility layer for older cards that mention a
    connector in plain prose. Each occurrence is judged in its sentence/list
    scope: an explicitly forbidden occurrence is ignored, while a separate
    affirmative occurrence for the same service still requires the existing MCP
    server. Explicit worker-requirements fences remain authoritative.
    """
    stripped = _ANY_FENCE.sub("", text)
    result: dict[str, list[str]] = {}
    for name, server in _SERVICE_NAMES.items():
        pattern = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.I)
        for match in pattern.finditer(stripped):
            if _service_mention_is_negative(stripped, match):
                continue
            if _service_mention_is_affirmative(stripped, match):
                result.setdefault(server, [])
                break
    return result


def requirements(title: str, body: str) -> dict:
    """Explicit phase contract wins; inference can only request checks, not grants."""
    blocks = _FENCE.findall(body or "")
    if len(blocks) > 1:
        raise ValueError("one worker-requirements block is allowed")
    if blocks:
        value = json.loads(blocks[0])
        if not isinstance(value, dict) or set(value) - _KEYS:
            raise ValueError("invalid worker requirements")
        source = "declared"
    else:
        text = f"{title}\n{body}"
        # Compatibility for existing cards. The owner can narrow an offline
        # phase explicitly when a service is discussed but not actually used.
        value = {"mcp": _infer_mcp_requirements(text)}
        read_paths, write_paths = _infer_path_requirements(text)
        value["read_paths"] = read_paths
        value["write_paths"] = write_paths
        source = "inferred"
    result: dict[str, Any] = {"source": source}
    for key in ("toolsets", "commands", "read_paths", "write_paths"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 64 or any(not isinstance(x, str) or not x or len(x) > 4096 for x in items):
            raise ValueError(f"invalid {key}")
        if key == "toolsets" and any(not _NAME.fullmatch(x) for x in items):
            raise ValueError(f"invalid {key}")
        if key == "commands" and any(not _COMMAND_REF.fullmatch(x) for x in items):
            raise ValueError(f"invalid {key}")
        result[key] = sorted(set(items))
    mcp = value.get("mcp", {})
    if not isinstance(mcp, dict) or len(mcp) > 12:
        raise ValueError("invalid mcp requirements")
    for server, names in mcp.items():
        if not isinstance(server, str) or not _NAME.fullmatch(server) or not isinstance(names, list) or len(names) > 64 or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in names):
            raise ValueError("invalid mcp requirements")
    result["mcp"] = {s: sorted(set(n)) for s, n in sorted(mcp.items())}
    for key in ("terminal_network", "localhost_server", "browser_session"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"invalid {key}")
        result[key] = value.get(key, False)
    return result


def preparation_launch_surface(conn, task: dict) -> dict | None:
    """Select repair prerequisites from the bound original, never grant tools.

    Spawn intersects this request with existing admission. Share the selection
    with completion fingerprints so changed execution input earns one bounded
    opportunity, while status changes and repeated retries do not.
    """
    if task.get("created_by") != "worker-readiness":
        return None
    try:
        declared = requirements(task.get("title") or "", task.get("body") or "")
        if set(declared["toolsets"]) != {"file", "kanban"}:
            return None
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='readiness_preparation_for' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        original_id = json.loads(event[0]).get("task_id") if event else None
        if not isinstance(original_id, str) or not original_id:
            return None
        row = conn.execute(
            "SELECT t.title,t.body,t.tenant,t.owner_profile FROM tasks t "
            "JOIN task_links l ON l.child_id=t.id "
            "WHERE l.parent_id=? AND t.id=? AND l.edge_role='dependency'",
            (task["id"], original_id),
        ).fetchone()
        if row is None or row[2] != task.get("tenant") or row[3] != task.get("owner_profile"):
            return None
        needed = requirements(row[0] or "", row[1] or "")
        requested = {"file", "kanban", *needed["toolsets"], *needed["mcp"]}
        if needed["commands"] or needed["terminal_network"] or needed["localhost_server"]:
            requested.add("terminal")
        if needed["browser_session"]:
            requested.add("browser")
        # Existing delegated cards predate the structured requirements fence.
        if needed["source"] == "inferred":
            from hermes_cli.owner_cell import _routing_metadata
            routing = _routing_metadata(row[1])
            package = routing.get("delegation_package") or {}
            allowed = (package.get("workspace_and_tools") or {}).get("allowed_toolsets", [])
            if not isinstance(allowed, list) or len(allowed) > 64 or any(
                not isinstance(name, str) or not _NAME.fullmatch(name) for name in allowed
            ):
                return None
            requested.update(allowed)
        return {
            "policy_version": 3,
            "toolsets": sorted(requested),
            "tool_output_policy": "readiness-show-bounded-v1",
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def probe(config: dict, needed: dict, workspace: str, toolsets: list[str]) -> dict:
    """Run inside the exact-profile validator subprocess; never invoke a mutation."""
    issues, checked = [], []
    def fail(code: str, resource: str) -> None:
        issues.append({"code": code, "resource": resource})
    security = config.get("security") or {}
    confinement = security.get("workspace_confinement") or {}
    confined = confinement.get("enabled") is True
    root = Path(workspace).expanduser().resolve()
    read_roots = [root, *(Path(p).expanduser().resolve() for p in confinement.get("read_only_roots", []))]
    for name in needed["toolsets"]:
        if name not in toolsets:
            fail("missing_toolset", name)
        else:
            checked.append("toolset:" + name)
    for command in needed["commands"]:
        command_path = Path(command).expanduser()
        if command_path.is_absolute() or len(command_path.parts) > 1:
            candidate = command_path if command_path.is_absolute() else root / command_path
            try:
                found_path = candidate.resolve()
            except OSError:
                found_path = candidate.absolute()
            if confined and not _inside(found_path, read_roots):
                fail("command_outside_workspace", command)
            elif not found_path.is_file() or not os.access(found_path, os.X_OK):
                fail("missing_command", command)
            else:
                checked.append("command:" + command)
            continue
        found = shutil.which(command)
        if not found:
            fail("missing_command", command)
        elif confined:
            # Runtime binaries outside the actual sandbox's runtime read roots
            # are not made usable merely by having a PATH entry.
            from tools.workspace_confinement import _MACOS_RUNTIME_READ_ROOTS, _runtime_read_roots
            roots = [*read_roots, *(Path(p).resolve() for p in _MACOS_RUNTIME_READ_ROOTS), *_runtime_read_roots()]
            if os.sys.platform == "darwin" and not _inside(Path(found).resolve(), roots):
                fail("command_outside_workspace", command)
            else:
                checked.append("command:" + command)
        else:
            checked.append("command:" + command)
    for key, access in (("read_paths", os.R_OK), ("write_paths", os.W_OK)):
        for raw in needed[key]:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            roots = read_roots if key == "read_paths" else [root]
            if confined and not _inside(path, roots):
                fail("path_outside_workspace", raw)
                continue
            target = path if path.exists() else path.parent
            # Scratch root is materialized only after a successful claim.
            planned_root = key == "write_paths" and target == root and not root.exists()
            if key == "read_paths" and not path.exists():
                fail("missing_input", raw)
            elif not planned_root and not os.access(target, access):
                fail("path_not_accessible", raw)
            else:
                checked.append(key + ":" + raw)
    if needed["terminal_network"] and confined:
        fail("terminal_network_denied", "terminal")
    if needed["localhost_server"]:
        if confined:
            fail("localhost_listen_denied", "terminal")
        else:
            import socket
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    sock.listen(1)
                checked.append("localhost:bind+listen")
            except OSError:
                fail("localhost_listen_unavailable", "127.0.0.1")
    if needed["browser_session"]:
        # An MCP schema or browser executable cannot prove the right account is
        # signed in. The owner performs the authenticated phase and passes its
        # result back, or narrows the worker phase after a verified readback.
        fail("authenticated_browser_phase_required", "browser")
    servers = config.get("mcp_servers") or {}
    selected = {}
    for server in needed["mcp"]:
        cfg = servers.get(server)
        if not isinstance(cfg, dict) or cfg.get("enabled", True) is False or server not in toolsets:
            fail("missing_mcp_server", server)
        else:
            selected[server] = {**cfg, "lazy": False, "connect_timeout": 5}
    if selected:
        from tools import mcp_tool
        try:
            names = set(mcp_tool.register_mcp_servers(selected))
            status = {r["name"]: r for r in mcp_tool.get_mcp_status()}
            for server in selected:
                if not status.get(server, {}).get("connected"):
                    fail("mcp_connection_or_auth_unavailable", server)
                    continue
                required = needed["mcp"][server]
                missing = [name for name in required if mcp_tool.mcp_prefixed_tool_name(server, name) not in names]
                if missing:
                    for name in missing:
                        fail("missing_mcp_operation", server + ":" + name)
                else:
                    checked.append("mcp_connected:" + server)
        finally:
            mcp_tool.shutdown_mcp_servers()
    return {"version": 1, "requirements": needed, "checked": checked, "issues": issues}


def _task_body_without_preparation_receipts(conn, task_id: str, body: str) -> str:
    """Remove only exact trailing receipts the kernel previously appended."""
    text = _FENCE.sub("", body or "").rstrip()
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_prepared' ORDER BY id DESC",
        (task_id,),
    ):
        evidence = json.loads(row[0]).get("evidence")
        if not isinstance(evidence, str):
            break
        suffix = "\n\n## Readiness preparation\n" + evidence.strip()
        if not text.endswith(suffix):
            break
        text = text[:-len(suffix)].rstrip()
    return text.strip()


def _queue_preparation(conn, task, lane: str, report: dict, *, board=None) -> bool:
    """One durable same-owner prerequisite; atomic with parking the original.

    It is a prerequisite of the original, never its dependent. Completing it
    causes normal dependency promotion and a fresh admission, not a bypass.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import profile_exists, resolve_profile_env
    cfg = settings()
    if cfg.get("enabled") is not True or not report.get("issues"):
        return False
    with kb.write_txn(conn, allow_nested=True):
        current = kb.get_task(conn, task.id)
        if current is None or current.status != lane or current.claim_lock or kb._task_admission_snapshot(current) != kb._task_admission_snapshot(task):
            return False
        # Preparation cards cannot recursively create preparation cards.
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='readiness_preparation_for'", (task.id,)).fetchone():
            return False
        owner = conn.execute("SELECT owner_profile FROM tasks WHERE id=?", (task.id,)).fetchone()[0]
        if not owner or not profile_exists(owner) or kb._worker_profile_uses_root_home(owner):
            return False
        goal = conn.execute("SELECT contract_json FROM owner_cell_goals WHERE task_id=?", (task.id,)).fetchone()
        if not goal:
            return False
        contract = json.loads(goal[0])
        if not contract.get("security_realm"):
            return False
        digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
        try:
            profile_fingerprint = kb._profile_config_digest(resolve_profile_env(task.assignee))
        except (OSError, ValueError, TypeError):
            profile_fingerprint = None
        # Repeated identical repair attempts are bounded; a new task scope,
        # concrete readiness issue or profile revision is new evidence. The
        # preparation's own requirements fence is bookkeeping, not progress.
        fingerprint = hashlib.sha256(json.dumps({
            "body": _task_body_without_preparation_receipts(conn, task.id, task.body or ""),
            "title": task.title, "workspace": task.workspace_path,
            "profile": task.assignee, "report_digest": digest,
            "profile_config": profile_fingerprint,
        }, sort_keys=True).encode()).hexdigest()
        history = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_queued'",
            (task.id,),
        )]
        count = len(history)
        same = sum(item.get("repair_fingerprint", fingerprint) == fingerprint for item in history)
        if same >= 2:
            return False
        for item in history:
            previous = kb.get_task(conn, item.get("preparation_task", ""))
            if previous and previous.status not in {"done", "archived"}:
                return False
        prep_contract = {**contract, "goal_id": "readiness-" + task.id + "-" + str(count + 1),
            "objective": "Prepare existing authorized access for " + task.id,
            "runtime_profile": owner, "router_mode": "single", "risk_class": "low",
            "worker_budget": {"producers": 1, "reviewers": 0, "extras": 0, "max_depth": 1},
            "targets": [], "required_reviewer_profile": None, "delegation_roles": {"maker": owner, "reviewer": None},
            "callback": {**contract.get("callback", {}), "report_policy": "on_demand"}}
        prep_contract.pop("owner_acceptance", None)
        # The original is parked before the repair is released, so its owning
        # head can safely use the same admitted input directory. An empty
        # unrelated scratch directory cannot repair missing source inputs.
        # This grants no read roots or tools outside the existing task scope.
        workspace = current.workspace_path
        workspace_kwargs = {}
        if workspace:
            path = Path(workspace)
            if not path.is_absolute() or not path.is_dir():
                return False
            workspace_kwargs = {"workspace_kind": "dir", "workspace_path": str(path)}
        body = (
            f"Internal readiness repair for {task.id}; original worker {task.assignee}. "
            "Keep the original task's tenant, security realm, accountable owner, and approval gates. "
            "Use kanban_show to inspect its full spec. The original waits on this prerequisite.\n\n"
            "Resolve routine prerequisites autonomously using already authorized access. Stage necessary "
            "non-secret inputs into its workspace; verify the assigned profile can read them. Use its "
            "approved service connection. If the remaining phase requires owner-only access, reassign the original "
            "to its existing accountable owner in the structured completion below; preserve its review and approval contract. "
            "Do not copy credentials, broaden profile permissions, disable confinement, or bypass an approval rejection. "
            "A schema listing is not proof of a browser login or a successful publish/upload. Verify the actual "
            "account and operation with a read-only check before that phase. This preparation authorizes no external "
            "writes: those remain on the original task with its target ownership and approval gates. "
            "Read kanban_show.owner_cell.required_reviewer_profile and the native runtime handoff instructions before handoff. "
            "If required_reviewer_profile is set, the maker must reuse existing verified evidence, attach it "
            "once, and call kanban_request_review with its exact artifact reference and digest; the maker "
            "must not call kanban_complete or claim PASS. The assigned independent reviewer verifies that "
            "evidence and completes only after approval. Only when the native runtime contract permits direct completion, the maker "
            "may complete directly. The authorized completing worker uses kanban_complete with metadata.readiness_repair containing "
            "requirements (an object with toolsets, commands, read_paths, write_paths, mcp, terminal_network, "
            "localhost_server, browser_session as needed) and evidence (a concise string naming the "
            "verified files/account/read operation). Optionally include runtime_profile equal to the "
            "original accountable owner when its existing review contract permits that route. The "
            "system atomically updates the original phase and reruns admission. Do not mark the original "
            "done or claim a completed phase without evidence. "
            "If credentials genuinely require a new login or authorization, report that exact blocker. "
            "Routine existing-tool setup should not be sent to Chris as a request to give tools again.\n\n"
            + ("Use the original task's admitted input workspace: " + workspace +
               ". Preserve existing artifacts; the original is parked while this bounded preparation runs.\n\n" if workspace else "") +
            "Readiness findings (data):\n" + json.dumps(report, indent=2) +
            '\n\n```worker-requirements\n{"toolsets":["file","kanban"]}\n```'
        )
        prep_id = kb.create_task(conn, title="Prepare worker access: " + task.id, body=body,
            assignee=owner, created_by="worker-readiness", tenant=task.tenant,
            priority=task.priority + 1, triage=True, max_runtime_seconds=600, max_retries=1,
            board=board, owner_cell_contract=prep_contract,
            idempotency_key=f"worker-readiness:{task.id}:{count}:{digest}",
            **workspace_kwargs)
        # SQL is inside the same writer transaction as create_task's savepoint.
        # No worker can claim either row before the dependency is durable.
        conn.execute("INSERT INTO task_links(parent_id,child_id,edge_role) VALUES (?,?,'dependency')", (prep_id, task.id))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task.id,))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (prep_id,))
        kb._append_event(conn, prep_id, "readiness_preparation_for", {"task_id": task.id, "report_digest": digest})
        kb._append_event(conn, task.id, "readiness_preparation_queued", {"preparation_task": prep_id, "owner": owner, "lane": lane, "report": report, "repair_fingerprint": fingerprint})
        if lane == "review":
            # Resume the original reviewer lane through the existing event path.
            kb._append_event(conn, task.id, "dependency_wait", {"source_status": "review", "reason": "readiness preparation"})
        return True


def queue_preparation(conn, task, lane: str, report: dict, *, board=None) -> bool:
    from hermes_cli.owner_workflow import state as workflow_state, route_recovery
    if workflow_state(conn, task.id):
        return route_recovery(conn, task.id, reason=json.dumps(report, sort_keys=True),
                              failure_kind="readiness", expected_status=lane)
    try:
        return _queue_preparation(conn, task, lane, report, board=board)
    except Exception as exc:
        # A bad/stale owner contract must not abort the dispatch tick or leave
        # a half-created dependency. The transaction rolls back, then the
        # caller retains its existing fail-closed admission block.
        import logging
        logging.getLogger(__name__).warning("Readiness preparation unavailable for %s (%s)", task.id, type(exc).__name__)
        return False


def recover_failed_startups(conn, *, board=None, limit: int = 1) -> list[str]:
    """Hand observed circuit-breaker failures to the existing scoped owner.

    This deterministic dispatcher step never executes a model. Each diagnostic
    is consumed once; identical repair findings retain the preparation budget.
    Deliberate operator/approval blocks and unobserved historic crashes remain
    untouched. No private stderr is copied into the preparation or case.
    """
    from hermes_cli import kanban_db as kb
    if settings().get("enabled") is not True:
        return []
    recovered = []
    rows = conn.execute("""
        SELECT e.id AS event_id, e.task_id, e.run_id, e.payload
        FROM task_events e JOIN tasks t ON t.id=e.task_id
        JOIN task_runs r ON r.id=e.run_id AND r.task_id=t.id
        WHERE e.kind='worker_startup_diagnostic' AND t.status='blocked'
          AND t.claim_lock IS NULL AND r.ended_at IS NOT NULL
          AND COALESCE(t.block_kind,'') NOT IN ('needs_input','dependency')
          AND r.id=(SELECT MAX(id) FROM task_runs WHERE task_id=t.id)
          AND r.status IN ('crashed','failed','gave_up')
          AND e.id=(SELECT MAX(id) FROM task_events WHERE task_id=t.id
                    AND kind='worker_startup_diagnostic')
          AND (SELECT kind FROM task_events WHERE task_id=t.id
               AND kind IN ('gave_up','blocked','unblocked','status')
               ORDER BY id DESC LIMIT 1)='gave_up'
          AND COALESCE((SELECT kind FROM task_events WHERE task_id=t.id
               AND kind IN ('blocked','unblocked','capability_preflight_blocked','worktree_preclaim_blocked')
               ORDER BY id DESC LIMIT 1),'unblocked')='unblocked'
          AND NOT EXISTS (SELECT 1 FROM task_events handled
              WHERE handled.task_id=t.id AND handled.kind='owner_startup_repair_case'
                AND json_extract(handled.payload,'$.source_event_id')=e.id)
        ORDER BY e.id LIMIT ?
    """, (max(1, min(int(limit), 8)),)).fetchall()
    for row in rows:
        with kb.write_txn(conn):
            task = kb.get_task(conn, row['task_id'])
            if (not task or task.status != 'blocked' or task.claim_lock
                    or kb._has_sticky_block(conn, task.id)):
                continue
            # Only the failure breaker can authorize this recovery; arbitrary
            # blocked state or a later explicit hold is not startup evidence.
            latest = conn.execute("SELECT kind FROM task_events WHERE task_id=? "
                "AND kind IN ('gave_up','blocked','unblocked','status') ORDER BY id DESC LIMIT 1",
                (task.id,)).fetchone()
            if not latest or latest[0] != 'gave_up':
                continue
            payload = json.loads(row['payload'])
            diagnostic_code = payload.get('diagnostic_code', 'worker_startup_failed')
            if not isinstance(diagnostic_code, str) or not re.fullmatch(r'[a-z0-9_]{1,80}', diagnostic_code):
                diagnostic_code = 'worker_startup_failed'
            report = {'issues': [{'code': diagnostic_code, 'resource': 'assigned_worker_startup'}],
                      'returncode': payload.get('returncode')}
            # A source revision is new evidence. A run/event ID or a raw stderr
            # hash is not: putting either in this report would reset retry caps.
            source = payload.get('runtime_source_fingerprint')
            if isinstance(source, str) and re.fullmatch(r'[a-f0-9]{64}', source):
                report['runtime_source_fingerprint'] = source
            queued = queue_preparation(conn, task, 'blocked', report, board=board)
            owner_row = conn.execute('SELECT owner_profile FROM tasks WHERE id=?', (task.id,)).fetchone()
            owner = owner_row[0] if owner_row else None
            prep = None
            if queued:
                data = conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                    "AND kind='readiness_preparation_queued' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
                prep = json.loads(data[0])['preparation_task']
                recovered.append(task.id)
            kb._append_event(conn, task.id, 'owner_startup_repair_case', {
                'source_event_id': row['event_id'], 'source_run_id': row['run_id'],
                'diagnostic_code': diagnostic_code, 'owner': owner,
                'preparation_task': prep, 'state': 'repair_queued' if queued else 'owner_action_required',
                'next_action': ('Complete the linked scoped preparation, verify startup and resume the retained phase.'
                    if queued else 'The accountable head must inspect the run-bound startup diagnostic, verify or repair the existing owner contract and inputs, then explicitly resume within scope. No new user authorization is implied.'),
                'uncertainty': 'The diagnostic identifies a startup failure; a root cause requires verification.',
            }, run_id=row['run_id'])
    return recovered


def apply_preparation_result(conn, preparation_id: str, metadata: dict | None) -> None:
    """Apply the owner's structured handoff atomically with its completion.

    Called inside complete_task's transaction, after its run/claim checks.
    Ordinary completion metadata can never edit a different task.
    """
    from hermes_cli import kanban_db as kb, owner_cell
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_for' ORDER BY id DESC LIMIT 1", (preparation_id,)).fetchone()
    if not row:
        return
    original_id = json.loads(row[0])["task_id"]
    original = kb.get_task(conn, original_id)
    prep = kb.get_task(conn, preparation_id)
    if not original or original.status != "todo" or original.claim_lock:
        raise ValueError("original task changed while preparation was running")
    linked = conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=? AND edge_role='dependency'", (preparation_id, original_id)).fetchone()
    goal = conn.execute("SELECT contract_json FROM owner_cell_goals WHERE task_id=?", (original_id,)).fetchone()
    if not linked or not goal:
        raise ValueError("preparation lost its authoritative task scope")
    contract = json.loads(goal[0])
    if prep is None or prep.tenant != original.tenant:
        raise ValueError("preparation owner or tenant changed")
    prep_goal = conn.execute("SELECT * FROM owner_cell_goals WHERE task_id=?", (preparation_id,)).fetchone()
    if prep_goal is not None and prep_goal["router_mode"] == "reviewed":
        # The maker's source custody remains unchanged when its preparation
        # is assigned to an independent reviewer. complete_task has already
        # validated the process-bound decision capability and recorded PASS
        # in this same transaction; bind that review to the original scope.
        prep_contract = json.loads(prep_goal["contract_json"])
        prep_scope = conn.execute("SELECT owner_profile,runtime_profile,security_realm FROM tasks WHERE id=?", (preparation_id,)).fetchone()
        original_scope = conn.execute("SELECT security_realm FROM tasks WHERE id=?", (original_id,)).fetchone()
        if (prep_goal["phase"] != "review"
                or prep.assignee != prep_goal["required_reviewer_profile"]
                or prep.assignee != prep_goal["reviewer_profile"]
                or prep_scope["runtime_profile"] != prep.assignee
                or prep_goal["runtime_profile"] != prep.assignee
                or prep_contract.get("owner_profile") != contract["owner_profile"]
                or prep_scope["owner_profile"] != contract["owner_profile"]
                or prep_scope["security_realm"] != original_scope["security_realm"]
                or prep_contract.get("security_realm") != contract.get("security_realm")
                or prep.workspace_path != original.workspace_path
                or prep.workspace_kind != original.workspace_kind
                or prep_contract.get("private_review_policy") != contract.get("private_review_policy")):
            raise ValueError("preparation review changed its original owner scope")
        owner_cell.assert_completion_allowed(conn, preparation_id)
    elif prep.assignee != contract["owner_profile"]:
        raise ValueError("preparation owner or tenant changed")
    repair = (metadata or {}).get("readiness_repair")
    if not isinstance(repair, dict) or set(repair) - {"requirements", "evidence", "runtime_profile"}:
        raise ValueError("complete preparation with metadata.readiness_repair: requirements and evidence")
    evidence = repair.get("evidence")
    if not isinstance(evidence, str) or not 8 <= len(evidence.strip()) <= 4000:
        raise ValueError("preparation requires bounded verification evidence")
    if not isinstance(repair.get("requirements"), dict):
        raise ValueError("preparation requires the remaining phase requirements")
    block = "```worker-requirements\n" + json.dumps(repair["requirements"], sort_keys=True) + "\n```"
    requirements("", block)  # Strict shape validation; grants no authority.
    runtime = repair.get("runtime_profile", original.assignee)
    if runtime != original.assignee:
        # Route only within the original owner contract, never to an arbitrary
        # powerful profile or the reviewer of its own work.
        allowed = {contract["owner_profile"], (contract.get("delegation_roles") or {}).get("maker")}
        if runtime not in allowed or kb._resume_status_from_events(conn, original_id) == "review":
            raise ValueError("runtime change is outside the original execution scope")
        candidate = {**contract, "runtime_profile": runtime}
        owner_cell.validate_goal_contract(candidate)
        owner_cell.resolve_task_runtime_profile(conn, original_id, runtime)
        conn.execute("UPDATE tasks SET assignee=?,runtime_profile=? WHERE id=?", (runtime, runtime, original_id))
        conn.execute("UPDATE owner_cell_goals SET runtime_profile=?,contract_json=? WHERE task_id=?", (runtime, json.dumps(candidate, sort_keys=True), original_id))
        kb._append_event(conn, original_id, "assigned", {"assignee": runtime, "preparation_task": preparation_id})
    body = _FENCE.sub("", original.body or "").rstrip()
    body += "\n\n## Readiness preparation\n" + evidence.strip() + "\n\n" + block
    # A verified, same-owner repair earns a fresh bounded execution attempt.
    # The preparation history still caps repeated unchanged repair claims.
    conn.execute("UPDATE tasks SET body=?, consecutive_failures=0, last_failure_error=NULL WHERE id=?", (body, original_id))
    kb._append_event(conn, original_id, "readiness_prepared", {"preparation_task": preparation_id, "requirements": repair["requirements"], "evidence": evidence.strip()})

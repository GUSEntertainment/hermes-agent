"""Opt-in, principal-scoped app facade over the hosted Discussion worker."""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from gateway import hosted_rooms
from tui_gateway.hosted_room_service import HostedRoomService

_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_services: dict[str, HostedRoomService] = {}
_lock = threading.RLock()


def _db() -> Path:
    return Path(hosted_rooms.default_db_path())


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS app_room_ownership (
        room_id TEXT PRIMARY KEY, principal TEXT NOT NULL, agents_json TEXT NOT NULL,
        turn_limit INTEGER NOT NULL DEFAULT 12, reserved_turns INTEGER NOT NULL DEFAULT 0,
        lifetime_turn_limit INTEGER NOT NULL DEFAULT 48, stopped INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0)""")
    return conn


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError("id must be a canonical UUID")
    return value


def _approval_ready():
    # The local TUI worker inherits process-wide approvals, unlike a signed
    # peer grant. Never expose its tools to an app room under YOLO/off mode.
    from tools.approval import _YOLO_MODE_FROZEN
    from gateway.run import _load_gateway_config
    if _YOLO_MODE_FROZEN:
        return False
    config = _load_gateway_config()
    return str(config.get("approvals", {}).get("mode", "manual")).lower() == "manual"


def _settings(self, request):
    # _check_auth deliberately permits a no-key default listener in tests;
    # this surface must not inherit that legacy bypass.
    if not self._expected_api_key():
        return None, None, web.json_response({"error": "app rooms unavailable"}, status=403)
    error = self._check_auth(request)
    if error is not None:
        return None, None, error
    principal = request.get("hermes.authenticated_principal")
    raw = (self.config.extra or {}).get("app_rooms") if hasattr(self, "config") else None
    if not isinstance(principal, str) or not principal or not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None, None, web.json_response({"error": "app rooms unavailable"}, status=403)
    requester = raw.get("requesters", {}).get(principal) if isinstance(raw.get("requesters"), dict) else None
    if not isinstance(requester, dict) or not isinstance(requester.get("agents"), dict):
        return None, None, web.json_response({"error": "app rooms unavailable"}, status=403)
    if not _approval_ready():
        return None, None, web.json_response({"error": "Groups need a protected approval connection."}, status=503)
    return principal, requester["agents"], None


def _service(self, path):
    with _lock:
        key = str(path)
        if key not in _services:
            import tui_gateway.server  # binds the existing in-process session backend
            from tui_gateway import methods_groups
            service = methods_groups.get_hosted_room_service()
            if service is None:
                service = methods_groups.start_hosted_room_service()
            if service is None or service.db_path != path:
                raise RuntimeError("hosted-room server is not bound")
            _services[key] = service
        return _services[key]


def _owned(path, room_id, principal):
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM app_room_ownership WHERE room_id=? AND principal=?", (room_id, principal)).fetchone()
    if row is None:
        raise LookupError("room not found")
    return row


def _shape(room):
    return {"id": room["room_id"], "name": room["name"],
            "agents": [{"id": m["member_id"], "name": m.get("display_name") or m["handle"]} for m in room["members"]],
            "createdAt": int(room["created_at"] * 1000)}


def _detail(service, path, room_id, principal):
    row = _owned(path, room_id, principal)
    room = hosted_rooms.room_state(path, room_id=room_id)
    events = service._events(room_id)
    messages = []
    for event in events:
        if event["kind"] not in ("message.user", "message.member"):
            continue
        payload = event["payload"]
        messages.append({"id": event["event_id"], "agent": payload.get("member_id") if event["kind"] == "message.member" else None,
                         "text": payload["text"], "createdAt": int(event["created_at"] * 1000)})
    state = service.status(room_id)
    if row["stopped"]:
        status, reason = "stopped", "Stopped by owner."
    elif row["paused"] or row["reserved_turns"] >= row["turn_limit"]:
        status, reason = "paused", "Turn budget reached; owner must resume."
    elif state["blocked"]:
        status, reason = "paused", "A room turn needs attention."
    else:
        status, reason = ("running", None) if state["working"] else ("idle", None)
    result = {"room": _shape(room), "messages": messages, "status": status, "limits": {"usedTurns": row["reserved_turns"], "currentTurnLimit": row["turn_limit"], "lifetimeTurnLimit": row["lifetime_turn_limit"], "canContinue": row["reserved_turns"] < row["lifetime_turn_limit"]}}
    result["approvals"] = [{"member": a["member_id"], "task": a["task_id"], "generation": a["execution_generation"], "request": a["request_id"], "description": str((a.get("approval") or {}).get("command") or (a.get("approval") or {}).get("description") or (a.get("approval") or {}).get("reason") or "This action needs your approval.")} for a in state.get("pending_actions", []) if a.get("kind") == "approval"]
    if reason:
        result["reason"] = reason
    return result


def _routes(self):
    return [("GET", "/v1/app-rooms/agents", lambda r: _handle(self, r, "agents")),
            ("GET", "/v1/app-rooms", lambda r: _handle(self, r, "list")),
            ("POST", "/v1/app-rooms", lambda r: _handle(self, r, "create")),
            ("GET", "/v1/app-rooms/{id}", lambda r: _handle(self, r, "get")),
            ("POST", "/v1/app-rooms/{id}/messages", lambda r: _handle(self, r, "message")),
            ("POST", "/v1/app-rooms/{id}/stop", lambda r: _handle(self, r, "stop")),
            ("POST", "/v1/app-rooms/{id}/resume", lambda r: _handle(self, r, "resume")),
            ("POST", "/v1/app-rooms/{id}/approval", lambda r: _handle(self, r, "approval")),
            *(__import__("gateway.platforms.api_server_app_shared", fromlist=["routes"]).routes(self))]


async def _handle(self, request, action):
    principal, agents, error = _settings(self, request)
    if error is not None:
        return error
    path = _db()
    try:
        if action == "agents":
            return web.json_response({"agents": [{"id": slug, "name": cfg["name"]} for slug, cfg in agents.items()
                                                  if _SLUG.fullmatch(slug) and isinstance(cfg, dict) and isinstance(cfg.get("profile"), str) and isinstance(cfg.get("name"), str)]})
        if action == "list":
            with _connect(path) as conn:
                ids = [r[0] for r in conn.execute("SELECT room_id FROM app_room_ownership WHERE principal=? ORDER BY rowid DESC", (principal,))]
            return web.json_response({"rooms": [_shape(hosted_rooms.room_state(path, room_id=id)) for id in ids]})
        body, error = await self._read_json_body(request) if action != "get" else ({}, None)
        if error is not None:
            return error
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        if action == "create":
            if set(body) != {"id", "name", "agents"}:
                raise ValueError("create requires id, name, agents only")
            if not isinstance(body["name"], str) or not body["name"].strip() or len(body["name"]) > 100:
                raise ValueError("name must be 1-100 characters")
            room_id = _uuid(body["id"])
            slugs = body["agents"]
            if not isinstance(slugs, list) or not 2 <= len(slugs) <= 6 or len(set(slugs)) != len(slugs):
                raise ValueError("choose 2-6 distinct agents")
            members = []
            for slug in slugs:
                if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug not in agents:
                    raise ValueError("agent is not owned")
                cfg = agents[slug]
                if not isinstance(cfg, dict) or not isinstance(cfg.get("profile"), str) or not isinstance(cfg.get("name"), str):
                    raise ValueError("agent configuration is invalid")
                members.append({"member_id": slug, "profile": cfg["profile"], "handle": slug, "display_name": cfg["name"]})
            with _lock:
                with _connect(path) as conn:
                    prior = conn.execute("SELECT principal FROM app_room_ownership WHERE room_id=?", (room_id,)).fetchone()
                    if prior and prior[0] != principal:
                        raise LookupError("room not found")
                    # Preflight ownership before invoking idempotent hosted create.
                    if not prior:
                        try:
                            hosted_rooms.room_state(path, room_id=room_id)
                        except hosted_rooms.RoomNotFoundError:
                            pass
                        else:
                            raise ValueError("room id already exists")
                    service = _service(self, path)
                    room = service.create_room(room_id=room_id, name=body["name"], members=members)
                    import json
                    conn.execute("INSERT OR IGNORE INTO app_room_ownership(room_id,principal,agents_json) VALUES(?,?,?)", (room_id, principal, json.dumps(slugs)))
            return web.json_response({"room": _shape(room)}, status=201)
        room_id = _uuid(request.match_info["id"])
        _owned(path, room_id, principal)
        service = _service(self, path)
        if action in {"stop", "resume"} and body:
            raise ValueError("body must be empty")
        if action == "message":
            if set(body) != {"id", "text"} or not isinstance(body["text"], str) or not body["text"].strip() or len(body["text"]) > 12000:
                raise ValueError("message requires id and text")
            event_id = _uuid(body["id"])
            stop_requested = body["text"].strip().lower().rstrip(".! ") in {"stop", "stop discussion", "stop the discussion"}
            if stop_requested:
                with _connect(path) as conn:
                    conn.execute("UPDATE app_room_ownership SET stopped=1 WHERE room_id=? AND principal=?", (room_id, principal))
            service.send(room_id=room_id, event_id=hosted_rooms.user_event_id(event_id), payload={"text": body["text"], "thread_id": room_id})
            if stop_requested:
                # Fence the stop message too, so resuming cannot discuss it.
                service.stop_room(room_id, cancel_id=f"app-stop:{event_id}")
        elif action == "stop":
            with _connect(path) as conn:
                conn.execute("UPDATE app_room_ownership SET stopped=1 WHERE room_id=? AND principal=?", (room_id, principal))
            service.stop_room(room_id, cancel_id=f"app-stop:{uuid.uuid4().hex}")
        elif action == "approval":
            if set(body) != {"member", "task", "generation", "request", "choice"} or body["choice"] not in {"once", "deny"} or type(body["generation"]) is not int:
                raise ValueError("invalid approval response")
            service.approve_room_task(room_id, member_id=body["member"], task_id=body["task"], execution_generation=body["generation"], request_id=body["request"], choice=body["choice"])
        elif action == "resume":
            with _connect(path) as conn:
                row = conn.execute("SELECT reserved_turns,lifetime_turn_limit FROM app_room_ownership WHERE room_id=? AND principal=?", (room_id, principal)).fetchone()
                if row["reserved_turns"] >= row["lifetime_turn_limit"]:
                    raise ValueError("room lifetime turn budget exhausted")
                conn.execute("UPDATE app_room_ownership SET stopped=0,paused=0,turn_limit=min(reserved_turns+12,lifetime_turn_limit) WHERE room_id=? AND principal=?", (room_id, principal))
            service.wakeup()
        return web.json_response(_detail(service, path, room_id, principal))
    except RuntimeError:
        return web.json_response({"error": "This action is no longer available. Refresh the group."}, status=409)
    except (ValueError, KeyError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except (LookupError, hosted_rooms.RoomNotFoundError):
        return web.json_response({"error": "room not found"}, status=404)

"""Scoped, non-LLM business data endpoints for authenticated app clients.

Identity is derived from the API server's authenticated profile or validated
room grant. Callers may choose only an explicit board slug and a bounded search
query/limit; they may not choose profile, domain, realm, catalog path, or roots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from hermes_cli import kanban_db

_BUSINESS_DATA_VERSION = 1
_BUSINESS_CONTRACT = "gus-business-data-v1"
_CANONICAL_KNOWLEDGE_ACCESS = Path("/Users/Chris/.openclaw/workspace/scripts/knowledge_access.py")
_DEFAULT_BOARD_LIMIT = 100
_DEFAULT_BUSINESS_BOARD_ROOT = Path("/Users/Chris/.hermes/kanban/boards")
_MAX_BOARD_LIMIT = 100
_MAX_SEARCH_LIMIT = 8
_MAX_QUERY_CHARS = 500
_MAX_SNIPPET_CHARS = 700
_MAX_RESPONSE_BYTES = 1024 * 1024
_BOARD_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,96}")
_ALLOWED_KNOWLEDGE_REALMS = ("company",)
_ALLOWED_SOURCE_REALMS = {"company", "reference"}
_ALLOWED_SOURCE_DOMAINS = {"business", "reference"}
_RESERVED_BOARD_SLUGS = {"default", "root", "current", "personal"}
_PRIVATE_SECURITY_REALMS = {"personal", "personal:chris", "personal:connor", "private", "family", "family:chris", "family:shared", "private_owner", "creator_only", "owner:chris", "chris", "connor"}
_PRIVATE_SECURITY_REALM_PREFIXES = ("personal", "family", "private")
_ALLOWED_BOARD_SECURITY_REALMS = {"company", "business", "gus", "founders", "team"}
_TEXT_FIELD_MAX_CHARS = 16_000


@dataclass(frozen=True)
class BoardRule:
    slug: str
    name: str
    tenants: tuple[str, ...]
    security_realms: tuple[str, ...]


@dataclass(frozen=True)
class BusinessScope:
    profile: str
    principal: str
    human: str
    domain: str
    boards: tuple[BoardRule, ...]
    knowledge_realm: str

    def public(self) -> dict[str, Any]:
        return {
            "contract": _BUSINESS_CONTRACT,
            "domain": self.domain,
            "human": self.human,
            "profile": self.profile,
            "principal": self.principal,
            "boards": [rule.slug for rule in self.boards],
            "knowledge_realms": ["company", "reference"],
        }


_APP_PROFILE_RE = re.compile(r"^app-gus-(?P<human>chris|connor)-margot$")


class BusinessDataPolicyError(ValueError):
    code = "business_data_forbidden"
    status = 403


class BusinessDataRequestError(ValueError):
    code = "business_data_bad_request"
    status = 400


def _normalize_profile(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_slug(value: Any) -> str:
    slug = str(value or "").strip()
    if not slug or not _BOARD_SLUG_RE.fullmatch(slug):
        raise BusinessDataRequestError("Invalid business board slug")
    if slug in _RESERVED_BOARD_SLUGS:
        raise BusinessDataPolicyError("Business data scope does not include this board")
    return slug


def _normalize_text_tuple(raw: Any, *, allowed: set[str] | None = None) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BusinessDataPolicyError("Business data policy list is invalid")
    values: list[str] = []
    for item in raw:
        value = str(item or "").strip().lower()
        if not value:
            continue
        if allowed is not None and value not in allowed:
            raise BusinessDataPolicyError("Business data policy contains an unsupported value")
        if value not in values:
            values.append(value)
    return tuple(values)


def _board_rules(raw: Any) -> tuple[BoardRule, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BusinessDataPolicyError("Business data board policy is invalid")
    rules: list[BoardRule] = []
    for item in raw:
        if isinstance(item, str):
            # A bare board slug is ambiguous for mixed boards and therefore not
            # enough authority to expose cards.
            raise BusinessDataPolicyError("Business board policy must declare card filters")
        if not isinstance(item, dict):
            raise BusinessDataPolicyError("Business data board policy is invalid")
        slug = _normalize_slug(item.get("slug"))
        tenants = _normalize_text_tuple(item.get("tenants"))
        realms = _normalize_text_tuple(item.get("security_realms"), allowed=_ALLOWED_BOARD_SECURITY_REALMS)
        if not tenants and not realms:
            raise BusinessDataPolicyError("Business board policy must declare tenant or security realm filters")
        rules.append(
            BoardRule(
                slug=slug,
                name=str(item.get("name") or slug),
                tenants=tenants,
                security_realms=realms,
            )
        )
    return tuple(rules)


def _business_config(self) -> dict[str, Any]:
    extra = getattr(getattr(self, "config", None), "extra", None) or {}
    configured = extra.get("business_data") if isinstance(extra, dict) else None
    return configured if isinstance(configured, dict) else {}


def _policy_from_config(self, profile: str) -> dict[str, Any] | None:
    profiles = _business_config(self).get("profiles")
    if not isinstance(profiles, dict):
        return None
    policy = profiles.get(profile)
    return policy if isinstance(policy, dict) else None


def _builtin_app_policy(profile: str) -> dict[str, Any] | None:
    match = _APP_PROFILE_RE.fullmatch(profile)
    if not match:
        return None
    return {
        "human": match.group("human"),
        "domain": "business",
        "boards": [],
        "knowledge_realm": "company",
    }


def _scope_from_policy(profile: str, principal: str, policy: dict[str, Any]) -> BusinessScope:
    human = str(policy.get("human") or "").strip().lower()
    domain = str(policy.get("domain") or "").strip().lower()
    if human not in {"chris", "connor"} or domain != "business":
        raise BusinessDataPolicyError("Business data scope is not available for this profile")
    realm = str(policy.get("knowledge_realm") or "company").strip().lower()
    if realm not in _ALLOWED_KNOWLEDGE_REALMS:
        raise BusinessDataPolicyError("Business data profile has unsupported knowledge realm")
    return BusinessScope(
        profile=profile,
        principal=principal or profile,
        human=human,
        domain=domain,
        boards=_board_rules(policy.get("boards", ())),
        knowledge_realm=realm,
    )




def _active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        return ""
    return _normalize_profile(profile)


def _request_business_profile(_api_server=None) -> str:
    profile = ""
    if _api_server is not None:
        try:
            profile = _api_server._api_request_profile.get() or ""
        except Exception:
            profile = ""
    profile = _normalize_profile(profile)
    if profile and profile != "default":
        return profile
    active = _active_profile_name()
    return active if active and active not in {"default", "custom"} else "default"


def _business_scope_for_profile(self, profile: str, principal: str) -> BusinessScope:
    profile = _normalize_profile(profile)
    if profile in {"", "default", "root", "personal", "chris-home", "connor-home"}:
        raise BusinessDataPolicyError("Business data scope is not available for this profile")
    policy = _policy_from_config(self, profile)
    if policy is None:
        policy = _builtin_app_policy(profile)
    if not isinstance(policy, dict):
        raise BusinessDataPolicyError("Business data scope is not available for this profile")
    return _scope_from_policy(profile, principal, policy)


def _business_data_capabilities(self, *, _api_server=None) -> dict[str, Any]:
    profile = _request_business_profile(_api_server)
    try:
        scope = _business_scope_for_profile(self, profile, profile)
        public_scope: dict[str, Any] | None = scope.public()
        supported = True
        reason = None
    except BusinessDataPolicyError as exc:
        public_scope = None
        supported = False
        reason = exc.code
    return {
        "supported": supported,
        "version": _BUSINESS_DATA_VERSION,
        "read_only": True,
        "non_llm_authorization": True,
        "enforced": True,
        "scope": public_scope,
        "unavailable_reason": reason,
        "limits": {
            "board_limit_max": _MAX_BOARD_LIMIT,
            "search_limit_max": _MAX_SEARCH_LIMIT,
            "query_chars_max": _MAX_QUERY_CHARS,
            "snippet_chars_max": _MAX_SNIPPET_CHARS,
            "response_bytes_max": _MAX_RESPONSE_BYTES,
        },
        "endpoints": {
            "board": {"method": "GET", "path": "/v1/business/boards/{slug}"},
            "knowledge_search": {
                "method": "POST",
                "path": "/v1/business/knowledge/search",
                "body": {"query": "string <= 500 chars", "limit": "1..8"},
            },
        },
    }


def _json_error(message: str, *, status: int, code: str, _openai_error) -> web.Response:
    return web.json_response(_openai_error(message, code=code), status=status)


def _room_claims_for_business(self, request: web.Request, *, _api_server) -> tuple[str, str] | web.Response:
    _openai_error = _api_server._openai_error
    try:
        claims = self._room_grant_claims(request, permission="status")
        profile = _api_server._api_request_profile.get() or "default"
        from gateway import hosted_rooms

        if (
            claims.get("target_profile") != profile
            or claims.get("target_install_id") != hosted_rooms.local_authority_gateway_id()
        ):
            raise ValueError("room grant target does not match this profile")
        principal = f"room:{str(claims.get('member_id') or '').strip()}"
        return profile, principal
    except Exception:
        return _json_error(
            "Room authorization is invalid or expired.",
            status=401,
            code="invalid_room_grant",
            _openai_error=_openai_error,
        )


def _business_scope_for_request(self, request: web.Request, *, _api_server) -> tuple[BusinessScope | None, web.Response | None]:
    if self._room_grant_token(request):
        return None, _json_error(
            "Business data endpoints require an explicit business-data bearer profile; room status grants are not sufficient.",
            status=403,
            code="business_room_grant_unsupported",
            _openai_error=_api_server._openai_error,
        )
    auth_err = self._check_auth(request)
    if auth_err:
        return None, auth_err
    profile = _request_business_profile(_api_server)
    request_get = getattr(request, "get", None)
    principal = str(request_get("hermes.authenticated_principal") if callable(request_get) else "") or profile
    try:
        return _business_scope_for_profile(self, profile, principal), None
    except BusinessDataPolicyError as exc:
        return None, _json_error(str(exc), status=exc.status, code=exc.code, _openai_error=_api_server._openai_error)


def _parse_limit(raw: Any, *, default: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw))
    except Exception as exc:
        raise BusinessDataRequestError("limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise BusinessDataRequestError(f"limit must be between 1 and {maximum}")
    return value


def _decode_cursor(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    try:
        decoded = base64.urlsafe_b64decode(str(raw).encode() + b"===")
        offset = int(json.loads(decoded.decode()).get("offset"))
    except Exception as exc:
        raise BusinessDataRequestError("Invalid cursor") from exc
    if offset < 0 or offset > 100000:
        raise BusinessDataRequestError("Invalid cursor")
    return offset


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": int(offset)}, separators=(",", ":")).encode()).decode().rstrip("=")


def _board_rule(scope: BusinessScope, slug: str) -> BoardRule:
    for rule in scope.boards:
        if rule.slug == slug:
            return rule
    raise BusinessDataPolicyError("Business data scope does not include this board")


def _path_from_text(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()


def _business_board_roots(self) -> tuple[Path, tuple[Path, ...]]:
    configured = _business_config(self)
    explicit_root = _path_from_text(configured.get("board_root"))
    if explicit_root is None:
        # Preserve ordinary Hermes/pytest behavior. The separate allowlist gate
        # applies only when an operator deliberately decouples business-data
        # reads from the profile's normal kanban home through reviewed config.
        root = kanban_db.boards_root().resolve()
        return root, (root,)

    raw_allowed = configured.get("allowed_board_roots")
    if not isinstance(raw_allowed, (list, tuple)) or not raw_allowed:
        raise BusinessDataPolicyError("Business data board root allowlist is required")

    allowed: list[Path] = []
    for item in raw_allowed:
        allowed_path = _path_from_text(item)
        if allowed_path is None:
            raise BusinessDataPolicyError("Business data board root allowlist is invalid")
        if allowed_path not in allowed:
            allowed.append(allowed_path)

    if explicit_root not in allowed:
        raise BusinessDataPolicyError("Business data board root is not approved")
    return explicit_root, tuple(allowed)


def _board_db_path(slug: str, board_root: Path) -> Path:
    path = (board_root / slug / "kanban.db").resolve()
    expected_parent = (board_root / slug).resolve()
    if path.parent != expected_parent or path.name != "kanban.db":
        raise BusinessDataPolicyError("Business board path is invalid")
    return path


def _connect_existing_board(slug: str, board_root: Path) -> sqlite3.Connection:
    path = _board_db_path(slug, board_root)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("Business board is not available")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _task_source_hash(row: sqlite3.Row) -> str:
    material = {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "assignee": row["assignee"],
        "tenant": row["tenant"] if "tenant" in row.keys() else None,
        "security_realm": row["security_realm"] if "security_realm" in row.keys() else None,
        "result": row["result"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _is_private_security_realm(realm: str) -> bool:
    value = str(realm or "").strip().lower()
    return value in _PRIVATE_SECURITY_REALMS or any(value.startswith(prefix) for prefix in _PRIVATE_SECURITY_REALM_PREFIXES)


def _row_authorized(row: sqlite3.Row, rule: BoardRule) -> bool:
    keys = set(row.keys())
    tenant = str(row["tenant"] or "").strip().lower() if "tenant" in keys else ""
    realm = str(row["security_realm"] or "").strip().lower() if "security_realm" in keys else ""
    if _is_private_security_realm(realm):
        return False
    tenant_ok = tenant in rule.tenants if rule.tenants else True
    realm_ok = realm in rule.security_realms if rule.security_realms else True
    return bool(tenant_ok and realm_ok and (rule.tenants or rule.security_realms))


def _bounded_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= _TEXT_FIELD_MAX_CHARS:
        return text
    return text[:_TEXT_FIELD_MAX_CHARS] + "…[truncated]"


def _row_to_task(board: str, row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "board": board,
        "canonical_path": f"kanban://{board}/tasks/{row['id']}",
        "source_sha256": _task_source_hash(row),
        "title": row["title"],
        "status": row["status"],
        "assignee": row["assignee"],
        "body": _bounded_text(row["body"]),
        "result": _bounded_text(row["result"]),
        "priority": row["priority"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "tenant": row["tenant"] if "tenant" in keys else None,
        "security_realm": row["security_realm"] if "security_realm" in keys else None,
    }


def _response_size(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False).encode())
    except Exception:
        return _MAX_RESPONSE_BYTES + 1


def _enforce_response_bound(payload: dict[str, Any], *, item_key: str, start_offset: int) -> None:
    if _response_size(payload) <= _MAX_RESPONSE_BYTES:
        return
    payload.setdefault("warnings", []).append("response_truncated_to_size_limit")
    items = payload.get(item_key)
    if not isinstance(items, list):
        payload[item_key] = []
        payload["next_cursor"] = _encode_cursor(start_offset)
        return
    original_count = len(items)
    while items and _response_size(payload) > _MAX_RESPONSE_BYTES:
        items.pop()
    if _response_size(payload) > _MAX_RESPONSE_BYTES:
        payload[item_key] = []
    payload["next_cursor"] = _encode_cursor(start_offset + original_count)


async def _handle_business_board(self, request: web.Request, *, _api_server) -> web.Response:
    scope, auth_err = _business_scope_for_request(self, request, _api_server=_api_server)
    if auth_err:
        return auth_err
    assert scope is not None
    _openai_error = _api_server._openai_error
    try:
        board = _normalize_slug(request.match_info.get("slug"))
        rule = _board_rule(scope, board)
        limit = _parse_limit(request.query.get("limit"), default=_DEFAULT_BOARD_LIMIT, maximum=_MAX_BOARD_LIMIT)
        offset = _decode_cursor(request.query.get("cursor"))
    except (BusinessDataPolicyError, BusinessDataRequestError) as exc:
        return _json_error(str(exc), status=exc.status, code=exc.code, _openai_error=_openai_error)

    try:
        board_root, _allowed_board_roots = _business_board_roots(self)
    except BusinessDataPolicyError as exc:
        return _json_error(str(exc), status=exc.status, code=exc.code, _openai_error=_openai_error)

    try:
        with _connect_existing_board(board, board_root) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            security_expr = "security_realm" if "security_realm" in columns else "NULL AS security_realm"
            where = ["status != 'archived'"]
            params: list[Any] = []
            if rule.tenants:
                where.append("lower(coalesce(tenant, '')) IN (" + ",".join("?" for _ in rule.tenants) + ")")
                params.extend(rule.tenants)
            if rule.security_realms:
                if "security_realm" not in columns:
                    where.append("0")
                else:
                    where.append("lower(coalesce(security_realm, '')) IN (" + ",".join("?" for _ in rule.security_realms) + ")")
                    params.extend(rule.security_realms)
            if "security_realm" in columns:
                where.append("lower(coalesce(security_realm, '')) NOT IN (" + ",".join("?" for _ in _PRIVATE_SECURITY_REALMS) + ")")
                params.extend(sorted(_PRIVATE_SECURITY_REALMS))
                for private_prefix in _PRIVATE_SECURITY_REALM_PREFIXES:
                    where.append("lower(coalesce(security_realm, '')) NOT LIKE ?")
                    params.append(private_prefix + "%")
            params.extend([limit + 1, offset])
            rows = conn.execute(
                f"""
                SELECT id, title, body, assignee, status, priority, created_by,
                       created_at, started_at, completed_at, tenant, result,
                       {security_expr}
                FROM tasks
                WHERE {' AND '.join(where)}
                ORDER BY priority DESC, created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
    except FileNotFoundError:
        return _json_error("Business board is not available.", status=404, code="business_board_not_found", _openai_error=_openai_error)
    except Exception:
        return _json_error("Business board read failed.", status=500, code="business_board_read_failed", _openai_error=_openai_error)

    visible = [row for row in rows[:limit] if _row_authorized(row, rule)]
    payload = {
        "object": "hermes.business.board",
        "scope": scope.public(),
        "board": {"slug": board, "name": rule.name},
        "tasks": [_row_to_task(board, row) for row in visible],
        "next_cursor": _encode_cursor(offset + limit) if len(rows) > limit else None,
    }
    _enforce_response_bound(payload, item_key="tasks", start_offset=offset)
    return web.json_response(payload)


def _terms(query: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", query.lower()) if len(token) >= 2][:12]


def _knowledge_script() -> Path:
    if not _CANONICAL_KNOWLEDGE_ACCESS.exists() or not _CANONICAL_KNOWLEDGE_ACCESS.is_file():
        raise FileNotFoundError("canonical knowledge access script is unavailable")
    return _CANONICAL_KNOWLEDGE_ACCESS


def _run_knowledge_search(query: str, realm: str, limit: int) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(_knowledge_script()), "search", "--realm", realm, "--query", query, "--limit", str(limit)],
        cwd=str(_knowledge_script().parent),
        text=True,
        capture_output=True,
        timeout=12,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        return {"status": "unavailable", "results": [], "warnings": ["knowledge_access_unavailable"]}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"status": "unavailable", "results": [], "warnings": ["knowledge_access_invalid_json"]}
    return data if isinstance(data, dict) else {"status": "unavailable", "results": [], "warnings": ["knowledge_access_invalid_shape"]}


def _numeric_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _project_knowledge_result(item: dict[str, Any]) -> dict[str, Any] | None:
    realm = str(item.get("realm") or "").strip().lower()
    domain = str(item.get("domain") or "").strip().lower()
    if realm not in _ALLOWED_SOURCE_REALMS or domain not in _ALLOWED_SOURCE_DOMAINS:
        return None
    source_sha = str(item.get("source_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha):
        return None
    snippet = str(item.get("snippet") or "")[:_MAX_SNIPPET_CHARS]
    path = str(item.get("path") or "").strip()
    if not path:
        return None
    return {
        "canonical_path": path,
        "title": str(item.get("title") or "")[:240],
        "realm": realm,
        "domain": domain,
        "line": item.get("line") if isinstance(item.get("line"), int) else None,
        "source_sha256": source_sha,
        "modified_at": _numeric_timestamp(item.get("modified_at")),
        "snippet": snippet,
        "backend": str(item.get("backend") or "knowledge_access")[:80],
        "evidence_kind": item.get("evidence_kind"),
    }


async def _handle_business_knowledge_search(self, request: web.Request, *, _api_server) -> web.Response:
    scope, auth_err = _business_scope_for_request(self, request, _api_server=_api_server)
    if auth_err:
        return auth_err
    assert scope is not None
    _openai_error = _api_server._openai_error
    body, err = await self._read_json_body(request)
    if err:
        return err
    if set(body) - {"query", "limit"}:
        return _json_error("Business knowledge search accepts only query and limit.", status=400, code="business_data_bad_request", _openai_error=_openai_error)
    query = str(body.get("query") or "").strip()
    if not query or len(query) > _MAX_QUERY_CHARS or not _terms(query):
        return _json_error("query is required and must contain searchable terms up to 500 characters.", status=400, code="business_data_bad_request", _openai_error=_openai_error)
    try:
        limit = _parse_limit(body.get("limit"), default=_MAX_SEARCH_LIMIT, maximum=_MAX_SEARCH_LIMIT)
    except BusinessDataRequestError as exc:
        return _json_error(str(exc), status=exc.status, code=exc.code, _openai_error=_openai_error)

    raw = await _api_server.asyncio.to_thread(_run_knowledge_search, query, scope.knowledge_realm, limit)
    projected: list[dict[str, Any]] = []
    for item in raw.get("results", []) if isinstance(raw.get("results"), list) else []:
        if not isinstance(item, dict):
            continue
        projected_item = _project_knowledge_result(item)
        if projected_item is not None:
            projected.append(projected_item)
        if len(projected) >= limit:
            break
    warnings = [str(w) for w in raw.get("warnings", []) if isinstance(w, str)] if isinstance(raw.get("warnings"), list) else []
    backend_unavailable = raw.get("status") == "unavailable"
    if backend_unavailable:
        warnings.append("knowledge_access_unavailable")
    payload = {
        "object": "hermes.business.knowledge_search",
        "scope": scope.public(),
        "status": "unavailable" if backend_unavailable else ("ok" if projected else "no_match"),
        "catalog_built_at": _numeric_timestamp(raw.get("catalog_built_at")),
        "results": projected,
        "warnings": sorted(set(warnings)),
    }
    return web.json_response(payload)

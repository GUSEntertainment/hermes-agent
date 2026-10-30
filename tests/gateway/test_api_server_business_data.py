from __future__ import annotations

import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms import api_server_business_data as business_data
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli import kanban_db


CHRIS_PROFILE = "app-gus-chris-margot"
CONNOR_PROFILE = "app-gus-connor-margot"
API_KEY = "sk-business-data-test-key-123456"


def _make_adapter(extra=None):
    config_extra = {"key": API_KEY}
    if extra:
        config_extra.update(extra)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra=config_extra))
    adapter._expected_api_key = lambda: API_KEY
    return adapter


def _auth_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


async def _business_app(adapter: APIServerAdapter, profile: str):
    async def board(request):
        token = api_server._api_request_profile.set(profile)
        try:
            return await adapter._handle_business_board(request)
        finally:
            api_server._api_request_profile.reset(token)

    async def search(request):
        token = api_server._api_request_profile.set(profile)
        try:
            return await adapter._handle_business_knowledge_search(request)
        finally:
            api_server._api_request_profile.reset(token)

    async def capabilities(request):
        token = api_server._api_request_profile.set(profile)
        try:
            return await adapter._handle_capabilities(request)
        finally:
            api_server._api_request_profile.reset(token)

    app = web.Application()
    app.router.add_get("/v1/business/boards/{slug}", board)
    app.router.add_post("/v1/business/knowledge/search", search)
    app.router.add_get("/v1/capabilities", capabilities)
    return app



def _board_conn(slug):
    """v0.22.5 refuses to create a board by bare connect; create it first."""
    if slug != kanban_db.DEFAULT_BOARD and not kanban_db.board_metadata_path(slug).exists():
        kanban_db.create_board(slug)
    return kanban_db.connect(board=slug)

@pytest.fixture()
def isolated_board_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = _board_conn("business-test")
    try:
        allowed = kanban_db.create_task(
            conn,
            title="GUS board registry smoke",
            body="Company reference item for board width and YAML registry verification.",
            assignee="margot",
            tenant="gus-business",
            board="business-test",
        )
        private = kanban_db.create_task(
            conn,
            title="Private board item",
            body="This personal item must not leave the mixed board.",
            assignee="margot",
            tenant="personal",
            board="business-test",
        )
        conflicting = kanban_db.create_task(
            conn,
            title="Conflicting private realm item",
            body="This item has an allowed tenant but an explicit private realm.",
            assignee="margot",
            tenant="gus-business",
            board="business-test",
        )
        with conn:
            conn.execute("UPDATE tasks SET security_realm='company' WHERE id=?", (allowed,))
            conn.execute("UPDATE tasks SET security_realm='personal' WHERE id=?", (private,))
            conn.execute("UPDATE tasks SET security_realm='personal:chris' WHERE id=?", (conflicting,))
    finally:
        conn.close()
    return tmp_path


def _board_policy(profile: str = CHRIS_PROFILE):
    return {
        "business_data": {
            "profiles": {
                profile: {
                    "human": "chris" if "chris" in profile else "connor",
                    "domain": "business",
                    "knowledge_realm": "company",
                    "boards": [
                        {
                            "slug": "business-test",
                            "name": "Business Test",
                            "tenants": ["gus-business"],
                            "security_realms": ["company"],
                        }
                    ],
                }
            }
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,human", [(CHRIS_PROFILE, "chris"), (CONNOR_PROFILE, "connor")])
async def test_business_capabilities_are_bound_to_authenticated_profile(profile, human):
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, profile))) as client:
        resp = await client.get("/v1/capabilities", headers=_auth_headers())
        data = await resp.json()
    caps = data["features"]["business_data"]
    assert caps["supported"] is True
    assert caps["scope"]["profile"] == profile
    assert caps["scope"]["human"] == human
    assert caps["scope"]["domain"] == "business"
    assert caps["scope"]["knowledge_realms"] == ["company", "reference"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "root", "personal", "chris-home", "connor-home", "foreign-app"])
async def test_business_data_rejects_root_personal_default_and_foreign_profiles(isolated_board_home, profile):
    adapter = _make_adapter(_board_policy(profile=CHRIS_PROFILE))
    async with TestClient(TestServer(await _business_app(adapter, profile=profile))) as client:
        resp = await client.get("/v1/business/boards/business-test", headers=_auth_headers())
    assert resp.status == 403


@pytest.mark.asyncio
async def test_business_board_requires_configured_card_filters(isolated_board_home):
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/business-test", headers=_auth_headers())
    assert resp.status == 403


@pytest.mark.asyncio
async def test_business_board_filters_mixed_board_by_server_policy(isolated_board_home):
    adapter = _make_adapter(_board_policy())
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/business-test", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 200
    assert data["scope"]["profile"] == CHRIS_PROFILE
    assert [task["title"] for task in data["tasks"]] == ["GUS board registry smoke"]
    assert data["tasks"][0]["tenant"] == "gus-business"
    assert data["tasks"][0]["security_realm"] == "company"
    assert data["tasks"][0]["source_sha256"] != data["tasks"][0]["canonical_path"].split("/")[-1]
    assert "Conflicting private realm item" not in {task["title"] for task in data["tasks"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("board", ["default", "root", "current", "chris-home", "connor-home", "personal", "foreign-board"])
async def test_business_board_rejects_private_default_and_foreign_boards(isolated_board_home, board):
    adapter = _make_adapter(_board_policy())
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get(f"/v1/business/boards/{board}", headers=_auth_headers())
    assert resp.status == 403


@pytest.mark.asyncio
async def test_business_search_uses_canonical_knowledge_access_and_projects_company_reference(monkeypatch, isolated_board_home):
    calls = []

    def fake_search(query, realm, limit):
        calls.append((query, realm, limit))
        return {
            "status": "ok",
            "catalog_built_at": "2026-09-06T15:11:50+00:00",
            "warnings": [],
            "results": [
                {
                    "path": "Agent-Shared/agent-roster-v1.md",
                    "title": "GUS Agent Runtime Roster",
                    "realm": "company",
                    "domain": "business",
                    "line": 12,
                    "source_sha256": "a" * 64,
                    "modified_at": "2026-09-06T15:11:50+00:00",
                    "snippet": "GUS company roster",
                    "backend": "current_source",
                },
                {
                    "path": "memory/private-note.md",
                    "title": "Private note",
                    "realm": "personal:chris",
                    "domain": "family",
                    "line": 1,
                    "source_sha256": "b" * 64,
                    "modified_at": "2026-09-06T15:11:50+00:00",
                    "snippet": "personal",
                    "backend": "current_source",
                },
            ],
        }

    monkeypatch.setattr(business_data, "_run_knowledge_search", fake_search)
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CONNOR_PROFILE))) as client:
        resp = await client.post(
            "/v1/business/knowledge/search",
            headers=_auth_headers(),
            json={"query": "GUS roster", "limit": 3},
        )
        data = await resp.json()
    assert resp.status == 200
    assert calls == [("GUS roster", "company", 3)]
    assert data["scope"]["profile"] == CONNOR_PROFILE
    assert data["scope"]["human"] == "connor"
    assert data["status"] == "ok"
    assert len(data["results"]) == 1
    assert data["results"][0]["canonical_path"] == "Agent-Shared/agent-roster-v1.md"
    assert data["results"][0]["source_sha256"] == "a" * 64
    assert data["results"][0]["modified_at"] == 1788707510


@pytest.mark.asyncio
async def test_business_search_rejects_client_supplied_identity_realm_and_paths(isolated_board_home):
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.post(
            "/v1/business/knowledge/search",
            headers=_auth_headers(),
            json={
                "query": "YAML registry",
                "limit": 2,
                "realm": "personal",
                "profile": "root",
                "path": "/Users/Chris",
            },
        )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_business_search_reports_unavailable_when_canonical_script_missing(monkeypatch, isolated_board_home):
    def unavailable(query, realm, limit):
        return {"status": "unavailable", "results": [], "warnings": ["knowledge_access_unavailable"]}

    monkeypatch.setattr(business_data, "_run_knowledge_search", unavailable)
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.post(
            "/v1/business/knowledge/search",
            headers=_auth_headers(),
            json={"query": "GUS", "limit": 2},
        )
        data = await resp.json()
    assert resp.status == 200
    assert data["status"] == "unavailable"
    assert "knowledge_access_unavailable" in data["warnings"]


@pytest.mark.asyncio
async def test_room_status_grant_does_not_inherit_business_data_scope(isolated_board_home):
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.post(
            "/v1/business/knowledge/search",
            headers={"Authorization": "HermesRoom fake-room-status-grant"},
            json={"query": "GUS", "limit": 1},
        )
        data = await resp.json()
    assert resp.status == 403
    assert data["error"]["code"] == "business_room_grant_unsupported"


@pytest.mark.asyncio
async def test_business_board_filters_before_pagination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = _board_conn("business-pagination")
    try:
        with conn:
            for idx in range(1005):
                conn.execute(
                    """
                    INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,tenant,security_realm)
                    VALUES (?, ?, ?, ?, 'ready', 0, 'test', ?, 'scratch', 'personal', 'personal:chris')
                    """,
                    (f"t_private_{idx:04x}", "Private filler", "private", "margot", idx + 1),
                )
            conn.execute(
                """
                INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,tenant,security_realm)
                VALUES ('t_business_page', 'Late business card', 'GUS business card after private filler', 'margot', 'ready', 0, 'test', 2000, 'scratch', 'gus-business', 'company')
                """
            )
    finally:
        conn.close()
    adapter = _make_adapter(
        {
            "business_data": {
                "profiles": {
                    CHRIS_PROFILE: {
                        "human": "chris",
                        "domain": "business",
                        "knowledge_realm": "company",
                        "boards": [
                            {
                                "slug": "business-pagination",
                                "name": "Business Pagination",
                                "tenants": ["gus-business"],
                                "security_realms": ["company"],
                            }
                        ],
                    }
                }
            }
        }
    )
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/business-pagination?limit=1", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 200
    assert [task["title"] for task in data["tasks"]] == ["Late business card"]


@pytest.mark.asyncio
async def test_business_board_response_size_is_hard_bounded(monkeypatch, isolated_board_home):
    monkeypatch.setattr(business_data, "_MAX_RESPONSE_BYTES", 800)
    adapter = _make_adapter(_board_policy())
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/business-test", headers=_auth_headers())
        text = await resp.text()
    data = await resp.json()
    assert resp.status == 200
    assert len(text.encode()) <= 800
    assert data["tasks"] == []
    assert business_data._decode_cursor(data["next_cursor"]) == 1
    assert "response_truncated_to_size_limit" in data["warnings"]

@pytest.mark.asyncio
async def test_current_home_board_requires_explicit_business_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = _board_conn("chris-home")
    try:
        rows = [
            ("t_allowed", "Founder business card", "Allowed only by exact route tenant and founders realm.", "telegram:-1004492739101:11", "founders"),
            ("t_missing", "Missing realm card", "Route tenant without trusted realm must stay hidden.", "telegram:-1004492739101:11", None),
            ("t_private", "Private prefixed card", "Allowed tenant with explicit private-like realm must stay hidden.", "telegram:-1004492739101:11", "private-legal-finance"),
            ("t_foreign", "Foreign tenant founder card", "Trusted realm with foreign tenant must stay hidden.", "telegram:foreign:11", "founders"),
        ]
        with conn:
            for idx, (task_id, title, body, tenant, realm) in enumerate(rows):
                conn.execute(
                    """
                    INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, tenant, security_realm)
                    VALUES (?, ?, ?, 'margot', 'todo', ?, 'test', ?, ?, ?)
                    """,
                    (task_id, title, body, 100 - idx, float(idx + 1), tenant, realm),
                )
    finally:
        conn.close()

    adapter = _make_adapter(
        {
            "business_data": {
                "profiles": {
                    CHRIS_PROFILE: {
                        "human": "chris",
                        "domain": "business",
                        "knowledge_realm": "company",
                        "boards": [
                            {
                                "slug": "chris-home",
                                "name": "Chris Business Projection",
                                "tenants": ["telegram:-1004492739101:11"],
                                "security_realms": ["founders"],
                            }
                        ],
                    }
                }
            }
        }
    )
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=10", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 200
    assert [task["id"] for task in data["tasks"]] == ["t_allowed"]
    assert data["tasks"][0]["tenant"] == "telegram:-1004492739101:11"
    assert data["tasks"][0]["security_realm"] == "founders"


@pytest.mark.asyncio
async def test_unconfigured_home_board_still_forbidden(isolated_board_home):
    adapter = _make_adapter()
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=1", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 403
    assert data["error"]["code"] == "business_data_forbidden"

@pytest.mark.asyncio
async def test_business_board_uses_explicit_read_only_root_without_unpinning_kanban_home(tmp_path, monkeypatch):
    actual_home = tmp_path / "actual"
    synthetic_home = tmp_path / "synthetic-profile"

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(actual_home))
    conn = _board_conn("chris-home")
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, tenant, security_realm)
                VALUES ('t_actual_business', 'Actual business projection', 'This row lives outside the profile tool root.', 'margot', 'ready', 50, 'test', 1, 'telegram:-1004492739101:11', 'founders')
                """
            )
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(synthetic_home))
    conn = _board_conn("chris-home")
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, tenant, security_realm)
                VALUES ('t_synthetic_leak', 'Synthetic profile tool root', 'This row must not satisfy business-data reads.', 'margot', 'ready', 100, 'test', 1, 'telegram:-1004492739101:11', 'founders')
                """
            )
    finally:
        conn.close()

    adapter = _make_adapter(
        {
            "business_data": {
                "board_root": str(actual_home / "kanban" / "boards"),
                "allowed_board_roots": [str(actual_home / "kanban" / "boards")],
                "profiles": {
                    CHRIS_PROFILE: {
                        "human": "chris",
                        "domain": "business",
                        "knowledge_realm": "company",
                        "boards": [
                            {
                                "slug": "chris-home",
                                "name": "Profile-safe Business Projection",
                                "tenants": ["telegram:-1004492739101:11"],
                                "security_realms": ["founders"],
                            }
                        ],
                    }
                },
            }
        }
    )
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=10", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 200
    assert [task["id"] for task in data["tasks"]] == ["t_actual_business"]
    assert "Synthetic profile tool root" not in {task["title"] for task in data["tasks"]}


@pytest.mark.asyncio
async def test_business_board_root_must_be_explicitly_approved(tmp_path, monkeypatch):
    actual_home = tmp_path / "actual"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(actual_home))
    conn = _board_conn("chris-home")
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "synthetic-profile"))

    policy = _board_policy(profile=CHRIS_PROFILE)
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["slug"] = "chris-home"
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["tenants"] = ["gus-business"]
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["security_realms"] = ["company"]
    policy["business_data"]["board_root"] = str(actual_home / "kanban" / "boards")
    adapter = _make_adapter(policy)
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=1", headers=_auth_headers())
        data = await resp.json()
    assert resp.status == 403
    assert data["error"]["code"] == "business_data_forbidden"

@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", [None, []])
async def test_explicit_business_board_root_requires_nonempty_config_allowlist(tmp_path, monkeypatch, allowlist):
    actual_home = tmp_path / "actual"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(actual_home))
    conn = _board_conn("chris-home")
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "synthetic-profile"))

    policy = _board_policy(profile=CHRIS_PROFILE)
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["slug"] = "chris-home"
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["tenants"] = ["gus-business"]
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["security_realms"] = ["company"]
    policy["business_data"]["board_root"] = str(actual_home / "kanban" / "boards")
    if allowlist is not None:
        policy["business_data"]["allowed_board_roots"] = allowlist

    adapter = _make_adapter(policy)
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=1", headers=_auth_headers())
        data = await resp.json()

    assert resp.status == 403
    assert data["error"]["code"] == "business_data_forbidden"


@pytest.mark.asyncio
async def test_business_board_root_ignores_environment_root_control(tmp_path, monkeypatch):
    actual_home = tmp_path / "actual"
    synthetic_home = tmp_path / "synthetic-profile"

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(actual_home))
    conn = _board_conn("chris-home")
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, tenant, security_realm)
                VALUES ('t_env_only_business', 'Env only business projection', 'This row must not be reachable through env-only control.', 'margot', 'ready', 50, 'test', 1, 'telegram:-1004492739101:11', 'founders')
                """
            )
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(synthetic_home))
    conn = _board_conn("chris-home")
    conn.close()
    monkeypatch.setenv("HERMES_BUSINESS_DATA_BOARD_ROOT", str(actual_home / "kanban" / "boards"))

    policy = _board_policy(profile=CHRIS_PROFILE)
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["slug"] = "chris-home"
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["tenants"] = ["telegram:-1004492739101:11"]
    policy["business_data"]["profiles"][CHRIS_PROFILE]["boards"][0]["security_realms"] = ["founders"]

    adapter = _make_adapter(policy)
    async with TestClient(TestServer(await _business_app(adapter, CHRIS_PROFILE))) as client:
        resp = await client.get("/v1/business/boards/chris-home?limit=1", headers=_auth_headers())
        data = await resp.json()

    assert resp.status == 200
    assert data["tasks"] == []

@pytest.mark.asyncio
async def test_single_profile_listener_uses_active_profile_for_business_data(isolated_board_home, monkeypatch):
    monkeypatch.setattr(business_data, "_active_profile_name", lambda: CHRIS_PROFILE)
    adapter = _make_adapter(_board_policy())

    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_get("/v1/business/boards/{slug}", adapter._handle_business_board)

    async with TestClient(TestServer(app)) as client:
        caps_resp = await client.get("/v1/capabilities", headers=_auth_headers())
        caps = await caps_resp.json()
        board_resp = await client.get("/v1/business/boards/business-test", headers=_auth_headers())
        board = await board_resp.json()

    assert caps_resp.status == 200
    assert caps["features"]["business_data"]["supported"] is True
    assert caps["features"]["business_data"]["scope"]["profile"] == CHRIS_PROFILE
    assert board_resp.status == 200
    assert board["scope"]["profile"] == CHRIS_PROFILE
    assert [task["title"] for task in board["tasks"]] == ["GUS board registry smoke"]

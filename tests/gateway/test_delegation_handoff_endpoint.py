"""POST /v1/runs/{run_id}/delegation-handoff: principal, session, board and
concurrency coverage (Grace 11001 endpoint tests). Isolated: temp registry,
no live boards, network or model calls."""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import delegation_handoff as h
from gateway.platforms.api_server import _api_request_profile
from tests.gateway.test_api_server_runs import _create_runs_app, _make_adapter

KEYS = {"owner": "sk-owner-0001", "other": "sk-other-0001"}


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "ROOT", tmp_path)
    monkeypatch.delenv(h.ENV, raising=False)
    return tmp_path


def _bind(run_id, session_id, principal="chris"):
    with h._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO run_bindings VALUES (?,?,?,?,?)",
                     (run_id, session_id, principal,
                      json.dumps({"run_id": run_id, "session_id": session_id}), time.time()))
        conn.commit()


def _app(adapter):
    @web.middleware
    async def stamp(request, handler):
        tok = _api_request_profile.set(request.headers.get("X-Test-Profile"))
        try:
            return await handler(request)
        finally:
            _api_request_profile.reset(tok)

    adapter._expected_api_key = lambda: KEYS.get(_api_request_profile.get(), "")
    app = _create_runs_app(adapter)
    app.middlewares.append(stamp)
    app.router.add_post("/v1/runs/{run_id}/delegation-handoff", adapter._handle_delegation_handoff)
    return app


def _hdr(profile):
    return {"X-Test-Profile": profile, "Authorization": f"Bearer {KEYS[profile]}"}


def _own(adapter, run_id, session_id, profile="owner"):
    adapter._expected_api_key = lambda: KEYS.get(_api_request_profile.get(), "")
    tok = _api_request_profile.set(profile)
    try:
        req = type("R", (), {"headers": _hdr(profile)})()
        adapter._run_owners[run_id] = adapter._run_idempotency_scope(req)
    finally:
        _api_request_profile.reset(tok)
    adapter._run_statuses[run_id] = {"object": "hermes.run", "run_id": run_id,
                                     "status": "completed", "session_id": session_id}


def _tokens():
    with h._connect() as conn:
        return conn.execute("SELECT count(*) FROM tokens").fetchone()[0]


@pytest.mark.asyncio
async def test_owner_with_bound_session_gets_single_use_token():
    adapter = _make_adapter()
    _own(adapter, "run_a", "sess_a"); _bind("run_a", "sess_a")
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.post("/v1/runs/run_a/delegation-handoff", json={"session_id": "sess_a"}, headers=_hdr("owner"))
        body = await r.json()
    assert r.status == 200 and isinstance(body["token"], str) and body["expires_in"] == h.TTL
    assert _tokens() == 1


@pytest.mark.asyncio
async def test_wrong_principal_cannot_see_or_mint():
    adapter = _make_adapter()
    _own(adapter, "run_a", "sess_a"); _bind("run_a", "sess_a")
    async with TestClient(TestServer(_app(adapter))) as cli:
        foreign = await cli.post("/v1/runs/run_a/delegation-handoff", json={"session_id": "sess_a"}, headers=_hdr("other"))
        nokey = await cli.post("/v1/runs/run_a/delegation-handoff", json={"session_id": "sess_a"},
                               headers={"X-Test-Profile": "owner"})
        badkey = await cli.post("/v1/runs/run_a/delegation-handoff", json={"session_id": "sess_a"},
                                headers={"X-Test-Profile": "owner", "Authorization": "Bearer sk-wrong"})
    assert foreign.status == 404 and nokey.status == 401 and badkey.status == 401
    assert _tokens() == 0


@pytest.mark.asyncio
async def test_unstamped_or_unknown_run_fails_closed():
    adapter = _make_adapter()
    adapter._run_statuses["run_x"] = {"object": "hermes.run", "run_id": "run_x", "status": "completed", "session_id": "s"}
    _bind("run_x", "s")
    async with TestClient(TestServer(_app(adapter))) as cli:
        unstamped = await cli.post("/v1/runs/run_x/delegation-handoff", json={"session_id": "s"}, headers=_hdr("owner"))
        unknown = await cli.post("/v1/runs/run_nope/delegation-handoff", json={"session_id": "s"}, headers=_hdr("owner"))
    assert (unstamped.status, unknown.status) == (404, 404)
    assert _tokens() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"session_id": "sess_other"}, {}, {"session_id": 7}, None])
async def test_wrong_or_missing_session_refused(body):
    adapter = _make_adapter()
    _own(adapter, "run_a", "sess_a"); _bind("run_a", "sess_a")
    async with TestClient(TestServer(_app(adapter))) as cli:
        kw = {"data": "not json"} if body is None else {"json": body}
        r = await cli.post("/v1/runs/run_a/delegation-handoff", headers=_hdr("owner"), **kw)
        err = (await r.json())["error"]
    assert r.status == 403 and err == "session_mismatch"
    assert _tokens() == 0


@pytest.mark.asyncio
async def test_registry_session_mismatch_or_missing_binding_refused():
    adapter = _make_adapter()
    # Status carries no session, so only the registry decides.
    _own(adapter, "run_b", None); _bind("run_b", "sess_real")
    _own(adapter, "run_c", "sess_c")  # owned run, no snapshot binding
    async with TestClient(TestServer(_app(adapter))) as cli:
        spoof = await cli.post("/v1/runs/run_b/delegation-handoff", json={"session_id": "sess_fake"}, headers=_hdr("owner"))
        unbound = await cli.post("/v1/runs/run_c/delegation-handoff", json={"session_id": "sess_c"}, headers=_hdr("owner"))
        errs = ((await spoof.json())["error"], (await unbound.json())["error"])
    assert (spoof.status, unbound.status) == (403, 403) and errs == ("no_verified_binding",) * 2
    assert _tokens() == 0


def test_token_is_bound_to_one_board_and_request(monkeypatch):
    _bind("run_a", "sess_a")
    monkeypatch.setenv(h.ENV, h.mint("run_a", "sess_a"))
    assert h.consume("req1", "chris-home")["run_id"] == "run_a"
    assert h.consume("req1", "chris-home")["run_id"] == "run_a"  # same request+board replay only
    for req, board in (("req1", "other-board"), ("req2", "chris-home")):
        with pytest.raises(PermissionError):
            h.consume(req, board)


def test_expired_or_foreign_token_fails_closed(monkeypatch):
    _bind("run_a", "sess_a")
    tok = h.mint("run_a", "sess_a")
    with h._connect() as conn:
        conn.execute("UPDATE tokens SET expires_at=?", (time.time() - 1,)); conn.commit()
    monkeypatch.setenv(h.ENV, tok)
    with pytest.raises(PermissionError):
        h.consume("req", "chris-home")
    monkeypatch.setenv(h.ENV, "forged-token")
    with pytest.raises(PermissionError):
        h.consume("req", "chris-home")


def test_concurrent_consumers_of_one_token_bind_exactly_one_request(monkeypatch):
    _bind("run_a", "sess_a")
    monkeypatch.setenv(h.ENV, h.mint("run_a", "sess_a"))
    barrier = threading.Barrier(8)

    def attempt(i):
        barrier.wait()
        try:
            h.consume(f"req{i}", "chris-home")
            return i
        except PermissionError:
            return None

    with ThreadPoolExecutor(8) as pool:
        winners = [r for r in pool.map(attempt, range(8)) if r is not None]
    assert len(winners) == 1
    with h._connect() as conn:
        assert conn.execute("SELECT count(*) FROM tokens WHERE used_at IS NOT NULL").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_concurrent_mints_are_independent_single_use_tokens():
    adapter = _make_adapter()
    _own(adapter, "run_a", "sess_a"); _bind("run_a", "sess_a")
    async with TestClient(TestServer(_app(adapter))) as cli:
        rs = await asyncio.gather(*[cli.post("/v1/runs/run_a/delegation-handoff", json={"session_id": "sess_a"},
                                             headers=_hdr("owner")) for _ in range(6)])
        toks = [(await r.json())["token"] for r in rs]
    assert all(r.status == 200 for r in rs) and len(set(toks)) == 6 and _tokens() == 6

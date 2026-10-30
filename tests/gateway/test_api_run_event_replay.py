#!/usr/bin/env python3
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from gateway.platforms import api_server_runs as runs


class FakeRequest:
    def __init__(self, scope="scope-a", query=None, headers=None):
        self.scope = scope
        self.query = query or {}
        self.headers = headers or {}

    def get(self, key, default=None):
        return default


class FakeServer:
    def __init__(self, store, scope="scope-a"):
        self._run_idempotency_store = store
        self._run_idempotency_ids = set()
        self._run_owners = {}
        self._run_statuses = {}
        self._active_run_agents = {}
        self._active_run_tasks = {}
        self.scope = scope

    def _run_idempotency_scope(self, request):
        return getattr(request, "scope", self.scope)

    def _room_grant_token(self, request):
        return None


class DurableRunEventReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "runs_idempotency.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _reserve(self, store, scope="scope-a", run_id="run_parent"):
        status = {
            "object": "hermes.run",
            "run_id": run_id,
            "status": "queued",
            "session_id": "conversation-1",
            "client_context": {
                "conversation_id": "conv-1",
                "message_id": "msg-1",
                "destination": "ios-thread-1",
                "human": "Chris",
                "agent": "Margot",
                "scopeContract": "gus-business-v1",
            },
        }
        outcome, _ = store.reserve(
            scope,
            "idem-key",
            "fingerprint",
            run_id,
            status,
            owner_pid=0,
            owner_started=0,
        )
        self.assertEqual(outcome, "created")
        return status

    def test_child_completion_can_be_recorded_after_parent_terminal_and_store_restart(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store)
        start = {"event": "subagent.start", "run_id": "run_parent", "timestamp": 3, "subagent_id": "sa-1", "child_session_id": "child-1"}
        parent_done = {"event": "run.completed", "run_id": "run_parent", "timestamp": 4, "output": "parent done"}
        c1 = store.append_event("scope-a", "run_parent", start)
        store.update_status("run_parent", {"run_id": "run_parent", "status": "completed"})
        c2 = store.append_event("scope-a", "run_parent", parent_done)
        self.assertLess(c1, c2)
        store.close()

        # Simulate a new API-server process/store instance learning about a
        # child completion after the parent has already reached terminal status.
        restarted = RunIdempotencyStore(str(self.db))
        child_done = {"event": "subagent.complete", "run_id": "run_parent", "timestamp": 5, "subagent_id": "sa-1", "child_session_id": "child-1", "status": "completed", "summary": "child done"}
        c3 = restarted.append_event("scope-a", "run_parent", child_done)
        self.assertGreater(c3, c2)
        restarted.close()

        reopened = RunIdempotencyStore(str(self.db))
        events = reopened.list_events("scope-a", "run_parent")
        self.assertEqual([e["event"] for e in events], ["subagent.start", "run.completed", "subagent.complete"])
        self.assertEqual(events[-1]["child_session_id"], "child-1")
        self.assertEqual(events[-1]["status"], "completed")
        reopened.close()

    def test_duplicate_events_collapse_and_cursor_replay_handles_out_of_order_timestamps(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store)
        complete = {"event": "subagent.complete", "run_id": "run_parent", "timestamp": 10, "subagent_id": "sa-1", "child_session_id": "child-1", "status": "completed"}
        start = {"event": "subagent.start", "run_id": "run_parent", "timestamp": 1, "subagent_id": "sa-1", "child_session_id": "child-1"}
        c1 = store.append_event("scope-a", "run_parent", complete)
        c1_dup = store.append_event("scope-a", "run_parent", dict(complete))
        c2 = store.append_event("scope-a", "run_parent", start)
        self.assertEqual(c1, c1_dup)
        self.assertGreater(c2, c1)
        all_events = store.list_events("scope-a", "run_parent")
        self.assertEqual([e["event"] for e in all_events], ["subagent.complete", "subagent.start"])
        after_first = store.list_events("scope-a", "run_parent", after_cursor=c1)
        self.assertEqual([e["event"] for e in after_first], ["subagent.start"])
        store.close()

    def test_cross_scope_events_are_denied_by_store_owner_scope(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store, scope="scope-a")
        cursor = store.append_event("scope-a", "run_parent", {"event": "subagent.complete", "run_id": "run_parent", "timestamp": time.time(), "subagent_id": "sa-1"})
        self.assertIsInstance(cursor, int)
        self.assertEqual(store.list_events("scope-b", "run_parent"), [])
        self.assertFalse(store.owns_run("scope-b", "run_parent"))
        store.close()


    def test_request_ownership_gate_allows_owner_and_denies_other_scope(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store, scope="scope-a")
        fake = FakeServer(store)
        fake._run_owners["run_parent"] = "scope-a"
        self.assertTrue(runs._request_owns_run(fake, FakeRequest(scope="scope-a"), "run_parent"))
        self.assertFalse(runs._request_owns_run(fake, FakeRequest(scope="scope-b"), "run_parent"))
        store.close()

    def test_run_helpers_filter_event_types_and_attach_client_context(self):
        store = RunIdempotencyStore(str(self.db))
        status = self._reserve(store)
        fake = FakeServer(store)
        fake._run_idempotency_ids.add("run_parent")
        fake._run_owners["run_parent"] = "scope-a"
        fake._run_statuses["run_parent"] = status
        decorated = runs._decorate_durable_run_event(fake, "run_parent", {"event": "subagent.start", "timestamp": 1, "subagent_id": "sa-1"})
        self.assertEqual(decorated["session_id"], "conversation-1")
        self.assertEqual(decorated["client_context"]["conversation_id"], "conv-1")
        self.assertIsNone(runs._persist_durable_run_event(fake, "run_parent", {"event": "message.delta", "delta": "not persisted"}))
        cursor = runs._persist_durable_run_event(fake, "run_parent", decorated)
        self.assertIsInstance(cursor, int)
        replay = runs._list_durable_run_events(fake, FakeRequest(scope="scope-a", query={"after": "0"}), "run_parent")
        self.assertEqual(replay[0]["cursor"], cursor)
        self.assertEqual(replay[0]["client_context"]["message_id"], "msg-1")
        store.close()



    def test_pre_session_child_terminal_has_stable_identity_without_fake_session(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store, scope="scope-a")
        fake = FakeServer(store)
        cursor = runs._persist_durable_run_event(fake, "run_parent", {
            "event": "subagent.complete",
            "timestamp": 1,
            "subagent_id": "sa-early",
            "status": "failed",
            "summary": "pre-session failure",
        })
        self.assertIsInstance(cursor, int)
        events = store.list_events("scope-a", "run_parent")
        self.assertEqual(events[0]["subagent_id"], "sa-early")
        self.assertNotIn("child_session_id", events[0])
        self.assertEqual(events[0]["child_session_state"], "not_started")
        self.assertIs(events[0]["pre_session_terminal"], True)
        store.close()

    def test_malformed_child_event_without_subagent_id_gets_no_cursor(self):
        store = RunIdempotencyStore(str(self.db))
        self._reserve(store, scope="scope-a")
        fake = FakeServer(store)
        cursor = runs._persist_durable_run_event(fake, "run_parent", {
            "event": "subagent.complete",
            "timestamp": 1,
            "status": "failed",
        })
        self.assertIsNone(cursor)
        self.assertEqual(store.list_events("scope-a", "run_parent"), [])
        store.close()

    def test_run_events_replay_capability_advertises_schema_and_retention(self):
        store = RunIdempotencyStore(str(self.db))
        fake = FakeServer(store)
        caps = runs._run_events_replay_capabilities(fake, store_type=RunIdempotencyStore)
        self.assertEqual(caps["supported"], True)
        self.assertEqual(caps["durable"], True)
        self.assertEqual(caps["version"], 1)
        self.assertEqual(caps["retention_seconds"], 24 * 60 * 60)
        self.assertEqual(caps["response_schema"], {
            "object": "list",
            "run_id": "string",
            "events": "array<hermes.run_event>",
            "next_cursor": "integer",
            "has_more": "boolean",
        })
        self.assertEqual(
            caps["child_event_schema"]["stable_identity"],
            "subagent_id is required for persisted subagent lifecycle events",
        )
        self.assertIn("not_started", caps["child_event_schema"]["pre_session_terminal"])
        self.assertIn("subagent.complete", caps["terminal_child_completion"])
        store.close()

    def test_client_context_is_allowlisted_and_bounded(self):
        context = runs._run_client_context({"metadata": {
            "conversation_id": "c" * 300,
            "message_id": "m1",
            "secret": "must not persist",
            "nested": {"no": "objects"},
        }})
        self.assertEqual(set(context), {"conversation_id", "message_id"})
        self.assertEqual(len(context["conversation_id"]), 256)
        self.assertEqual(context["message_id"], "m1")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AdapterRequest:
    def __init__(self, *, run_id="run_parent", key="test-key-1234567890abcdef", query=None, path=None):
        self.headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        self.query = query or {}
        self.match_info = {"run_id": run_id}
        self.path = path or f"/v1/runs/{run_id}/events"
        self.method = "GET"

    def get(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_real_api_server_adapter_wrappers_and_endpoint_replay_after_restart(tmp_path):
    key = "test-key-1234567890abcdef"
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(tmp_path)
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    try:
        request = AdapterRequest(key=key, query={"replay": "1"})
        scope = adapter._run_idempotency_scope(request)
        outcome, _ = adapter._run_idempotency_store.reserve(
            scope,
            "idem-key",
            "fingerprint",
            "run_parent",
            {
                "object": "hermes.run",
                "run_id": "run_parent",
                "status": "completed",
                "session_id": "conversation-1",
                "client_context": {"conversation_id": "conv-1", "destination": "ios-thread-1"},
            },
            owner_pid=0,
            owner_started=0,
        )
        assert outcome == "created"
        # Simulate a fresh API object's empty in-memory run ownership caches.
        adapter._run_idempotency_ids.clear()
        adapter._run_owners.clear()
        adapter._run_statuses.clear()

        callback = adapter._make_run_event_callback("run_parent", asyncio.get_running_loop())
        callback(
            "subagent.complete",
            subagent_id="sa-early",
            status="failed",
            summary="pre-session failure",
        )

        response = await adapter._handle_run_events(request)
        assert response.status == 200
        body = json.loads(response.text)
        assert set(["object", "run_id", "events", "next_cursor", "has_more"]) <= set(body)
        assert body["events"][0]["event"] == "subagent.complete"
        assert body["events"][0]["subagent_id"] == "sa-early"
        assert "child_session_id" not in body["events"][0]
        assert body["events"][0]["child_session_state"] == "not_started"
        assert body["events"][0]["pre_session_terminal"] is True
        assert body["events"][0]["client_context"]["conversation_id"] == "conv-1"
    finally:
        await adapter.disconnect()
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


@pytest.mark.asyncio
async def test_real_api_server_adapter_capability_advertises_run_events_replay(tmp_path):
    key = "test-key-1234567890abcdef"
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(tmp_path)
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    try:
        response = await adapter._handle_capabilities(AdapterRequest(key=key, path="/v1/capabilities"))
        body = json.loads(response.text)
        replay = body["features"]["run_events_replay"]
        assert replay["supported"] is True
        assert replay["durable"] is True
        assert replay["version"] == 1
        assert replay["retention_seconds"] == 24 * 60 * 60
        assert replay["response_schema"]["events"] == "array<hermes.run_event>"
        assert "pre_session_terminal" in replay["child_event_schema"]
    finally:
        await adapter.disconnect()
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home

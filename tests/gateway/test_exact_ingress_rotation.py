import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import resolve_telegram_channel_prompt
from gateway.run import _get_channel_override
from gateway.session import SessionSource, SessionStore
from gateway.session_rotation import should_rotate_session


def _handoff(**overrides):
    payload = {
        "kind": "handoff",
        "version": 1,
        "request_id": "req-123",
        "source_agent": "julia",
        "target_profile": "creator-a",
        "message": "Inspect the status.",
    }
    payload.update(overrides)
    return "HERMES_HANDOFF_V1:" + json.dumps(payload)


def _handoff_config():
    return {
        "gateway": {
            "bot_handoff": {
                "telegram": {
                    "allowed_senders": [
                        {
                            "sender_id": "999888777",
                            "source_agent": "julia",
                            "target_profile": "creator-a",
                        }
                    ]
                }
            }
        }
    }


def test_telegram_prompt_identity_does_not_collide_between_chats():
    extra = {
        "channel_prompts": {
            "-100:42": "chat one topic",
            "-200:42": "chat two topic",
            "-100": "chat one parent",
            "42": "unsafe legacy topic prompt",
        }
    }
    assert resolve_telegram_channel_prompt(extra, "-100", "42") == "chat one topic"
    assert resolve_telegram_channel_prompt(extra, "-200", "42") == "chat two topic"
    assert resolve_telegram_channel_prompt(extra, "-300", "42") is None


def test_telegram_prompt_exact_topic_falls_back_to_parent_chat():
    extra = {"channel_prompts": {"-100": "parent"}}
    assert resolve_telegram_channel_prompt(extra, "-100", "42") == "parent"


def test_telegram_channel_override_uses_exact_chat_topic_identity():
    topic = ChannelOverride(system_prompt="exact topic")
    parent = ChannelOverride(system_prompt="parent")
    unsafe_bare = ChannelOverride(system_prompt="wrong chat topic")
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                channel_overrides={"-100:42": topic, "-100": parent, "42": unsafe_bare},
            )
        }
    )
    assert _get_channel_override(config, Platform.TELEGRAM, "-100", thread_id="42") is topic
    assert _get_channel_override(config, Platform.TELEGRAM, "-200", thread_id="42") is None


def test_bot_handoff_requires_exact_sender_agent_and_profile(tmp_path):
    from gateway.bot_ingress import BotHandoffReplayStore, classify_bot_ingress

    store = BotHandoffReplayStore(tmp_path / "handoffs.sqlite3")
    allowed = classify_bot_ingress(
        source_is_bot=True,
        text=_handoff(),
        source_user_id="999888777",
        source_profile="creator-a",
        platform="telegram",
        config=_handoff_config(),
        replay_store=store,
    )
    assert allowed.allowed and allowed.message == "Inspect the status."

    for kwargs in (
        {"source_user_id": "111"},
        {"source_profile": "creator-b"},
        {"text": _handoff(source_agent="mallory")},
    ):
        args = dict(
            source_is_bot=True,
            text=_handoff(request_id="other-" + str(len(kwargs))),
            source_user_id="999888777",
            source_profile="creator-a",
            platform="telegram",
            config=_handoff_config(),
            replay_store=store,
        )
        args.update(kwargs)
        denied = classify_bot_ingress(**args)
        assert not denied.allowed
        assert denied.silent


def test_bot_handoff_replay_is_rejected_durably_before_session_creation(tmp_path):
    from gateway.bot_ingress import BotHandoffReplayStore, classify_bot_ingress

    path = tmp_path / "handoffs.sqlite3"
    args = dict(
        source_is_bot=True,
        text=_handoff(),
        source_user_id="999888777",
        source_profile="creator-a",
        platform="telegram",
        config=_handoff_config(),
    )
    assert classify_bot_ingress(**args, replay_store=BotHandoffReplayStore(path)).allowed
    replay = classify_bot_ingress(**args, replay_store=BotHandoffReplayStore(path))
    assert not replay.allowed
    assert replay.reason == "bot_handoff_replay"


@pytest.mark.asyncio
async def test_gateway_rejects_wrong_sender_and_replay_before_session_creation(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from gateway.platforms.base import MessageEvent
    from gateway.run import GatewayRunner

    runner = GatewayRunner(GatewayConfig())
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner.session_store.get_or_create_session = pytest.fail
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", _handoff_config)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_k: [])
    monkeypatch.setattr("agent.estop.paused_reply", lambda: "paused")

    wrong = MessageEvent(
        text=_handoff(),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            user_id="111",
            is_bot=True,
            profile="creator-a",
        ),
    )
    assert await runner._handle_message(wrong) is None

    exact = MessageEvent(
        text=_handoff(),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            user_id="999888777",
            is_bot=True,
            profile="creator-a",
        ),
    )
    assert await runner._handle_message(exact) == "paused"
    assert await runner._handle_message(exact) is None


def _history():
    return [{"role": "user", "content": "x"}] * 6


def test_rotation_is_default_off_and_broad_selector_does_not_opt_in_dm():
    group = SimpleNamespace(platform="telegram", chat_type="group", chat_id="-100", thread_id="42")
    assert should_rotate_session(
        source=group,
        session_key="agent:main:telegram:group:-100:42",
        history=_history(),
        estimated_tokens=200_000,
        config={},
    ).reason == "disabled"

    dm = SimpleNamespace(platform="telegram", chat_type="dm", chat_id="123", thread_id="")
    decision = should_rotate_session(
        source=dm,
        session_key="creator-a:agent:main:telegram:dm:123",
        history=_history(),
        estimated_tokens=200_000,
        config={"session_rotation": {"enabled": True, "mode": "all", "include": {"platforms": ["telegram"]}}},
    )
    assert not decision.rotate
    assert decision.reason == "dm_exempt"


def test_rotation_exact_dm_opt_in_and_actual_token_priority():
    dm = SimpleNamespace(platform="telegram", chat_type="dm", chat_id="123", thread_id="")
    decision = should_rotate_session(
        source=dm,
        session_key="creator-a:agent:main:telegram:dm:123",
        history=_history(),
        last_prompt_tokens=130_000,
        estimated_tokens=1,
        config={
            "session_rotation": {
                "enabled": True,
                "mode": "explicit",
                "token_threshold": 120_000,
                "include": {"chat_ids": ["123"]},
            }
        },
    )
    assert decision.rotate
    assert decision.token_source == "actual"


def _store(tmp_path):
    from hermes_state import SessionDB

    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    store._loaded = True
    return store


def test_seeded_rotation_preserves_full_identity_and_seeds_before_repoint(tmp_path):
    store = _store(tmp_path)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100",
        chat_name="Ops",
        chat_type="forum",
        user_id="worker",
        user_name="Worker",
        thread_id="42",
        chat_topic="Deploy",
        scope_id="tenant-7",
        parent_chat_id="parent-9",
        profile="creator-a",
    )
    old = store.get_or_create_session(source)
    new = store.reset_session_with_transcript(
        old.session_key,
        lambda session_id: [{"role": "assistant", "content": f"pickup:{session_id}"}],
    )
    assert new is not None
    assert new.origin == source
    assert store.peek_session_id(old.session_key) == new.session_id
    assert store.load_transcript(new.session_id)[0]["content"] == f"pickup:{new.session_id}"
    row = store._db.get_session(new.session_id)
    assert row["profile_name"] == "creator-a"
    assert json.loads(row["origin_json"])["scope_id"] == "tenant-7"
    store._db.close()


def test_seed_failure_and_route_race_keep_original_route(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-100", chat_type="forum", thread_id="42")
    old = store.get_or_create_session(source)

    monkeypatch.setattr(store._db, "replace_messages", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("seed failed")))
    assert store.reset_session_with_transcript(old.session_key, [{"role": "assistant", "content": "pickup"}]) is None
    assert store.peek_session_id(old.session_key) == old.session_id

    monkeypatch.undo()
    original_replace = store._db.replace_messages

    def race(session_id, messages):
        original_replace(session_id, messages)
        with store._lock:
            store._entries[old.session_key].session_id = "raced-session"

    monkeypatch.setattr(store._db, "replace_messages", race)
    assert store.reset_session_with_transcript(old.session_key, [{"role": "assistant", "content": "pickup"}]) is None
    assert store.peek_session_id(old.session_key) == "raced-session"
    store._db.close()


def test_route_persistence_failure_restores_exact_prior_route(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100",
        chat_type="forum",
        thread_id="42",
    )
    old = store.get_or_create_session(source)
    old_entry = store.lookup_by_session_key(old.session_key)
    persisted_before = store._db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )[old.session_key]

    def fail_primary(*_args, **_kwargs):
        raise RuntimeError("injected primary route save failure")

    def fail_fallback(*_args, **_kwargs):
        raise RuntimeError("injected fallback route save failure")

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", fail_primary)
    monkeypatch.setattr(store, "_save_sessions_json", fail_fallback)

    rotated = store.reset_session_with_transcript(
        old.session_key,
        [{"role": "assistant", "content": "pickup"}],
    )

    assert rotated is None
    assert store.lookup_by_session_key(old.session_key) is old_entry
    assert store.peek_session_id(old.session_key) == old.session_id
    persisted_after = store._db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )[old.session_key]
    assert persisted_after == persisted_before
    store._db.close()


@pytest.mark.asyncio
async def test_gateway_failed_rotation_keeps_route_cache_and_prompt_bytes(
    tmp_path, monkeypatch
):
    import gateway.run as gateway_run
    from gateway.platforms.base import MessageEvent
    from gateway.run import GatewayRunner

    store = _store(tmp_path)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
        user_id="123",
    )
    old = store.get_or_create_session(source)
    for index in range(6):
        store.append_to_transcript(
            old.session_id,
            {"role": "user" if index % 2 == 0 else "assistant", "content": "x"},
        )
    old.last_prompt_tokens = 130_000

    runner = GatewayRunner(GatewayConfig())
    runner.session_store = store
    runner.adapters = {}
    runner._session_db = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._turn_leases = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._agent_cache[old.session_key] = (object(), "stable-cache")
    cached_before = runner._agent_cache[old.session_key]
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 130_000,
        }
    )

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "session_rotation": {
                "enabled": True,
                "mode": "explicit",
                "token_threshold": 120_000,
                "include": {"chat_ids": ["123"]},
            }
        },
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 1_000_000,
    )

    def fail_primary(*_args, **_kwargs):
        raise RuntimeError("injected primary route save failure")

    def fail_fallback(*_args, **_kwargs):
        raise RuntimeError("injected fallback route save failure")

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", fail_primary)
    monkeypatch.setattr(store, "_save_sessions_json", fail_fallback)

    event = MessageEvent(text="continue", source=source, message_id="msg-1")
    await asyncio.wait_for(
        runner._handle_message_with_agent(event, source, old.session_key, 1),
        timeout=20,
    )
    event.message_id = "msg-2"
    await asyncio.wait_for(
        runner._handle_message_with_agent(event, source, old.session_key, 1),
        timeout=20,
    )

    assert store.get_or_create_session(source) is old
    assert store.peek_session_id(old.session_key) == old.session_id
    assert runner._agent_cache[old.session_key] is cached_before
    calls = runner._run_agent.call_args_list
    assert [call.kwargs["session_id"] for call in calls] == [
        old.session_id,
        old.session_id,
    ]
    assert calls[0].kwargs["context_prompt"].encode() == calls[1].kwargs[
        "context_prompt"
    ].encode()
    store._db.close()

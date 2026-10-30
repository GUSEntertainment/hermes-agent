"""Caller-path fail-closed tests for Margot OS topic dispatch."""
from __future__ import annotations

import hashlib
import pytest
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.department_topic_router import load_department_topic_activation
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _use_reviewed_topic_matrix(monkeypatch):
    import gateway.department_topic_router as topic_router

    matrix_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "margot_parity"
        / "margot-departments-topics.json"
    )
    monkeypatch.setattr(topic_router, "DEFAULT_MATRIX_PATH", matrix_path)
    monkeypatch.setattr(topic_router, "_REVIEWED_MATRIX_SHA256", hashlib.sha256(matrix_path.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "routing_config",
    [
        {"department_topic_routing": {"enabled": True}},
        {"department_topic_routing": {"enabled": True, "board": "default"}},
        {
            "department_topic_routing": {
                "enabled": True,
                "board": "chris-home",
                "matrix_path": "/tmp/untrusted.json",
            }
        },
    ],
)
def test_enabled_topic_activation_rejects_missing_or_unpinned_config(routing_config):
    with pytest.raises(ValueError):
        load_department_topic_activation(routing_config)


@pytest.mark.asyncio
async def test_outer_ingress_routes_specialist_before_plugin_hooks(monkeypatch):
    import gateway.department_topic_router as topic_router
    import gateway.run as gateway_run
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"department_topic_routing": {"enabled": True, "board": "chris-home"}},
    )
    hook_calls = []
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda *_args, **_kwargs: hook_calls.append(True) or [{"action": "skip"}],
    )
    captured = []
    monkeypatch.setattr(
        topic_router,
        "dispatch_to_safe_delegate",
        lambda envelope, *, board: captured.append((envelope, board)) or {"ok": True},
    )
    runner = GatewayRunner(GatewayConfig())
    monkeypatch.setattr(runner, "_is_user_authorized", lambda _source: True)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="Synthetic outer-ingress routing canary.",
        source=source,
        message_id="outer-9011",
    )

    response = await runner._handle_message(event)

    assert "accountable department workflow" in response
    assert len(captured) == 1
    assert hook_calls == []


@pytest.mark.asyncio
async def test_outer_ingress_does_not_route_unauthorized_specialist(monkeypatch):
    import gateway.department_topic_router as topic_router
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"department_topic_routing": {"enabled": True, "board": "chris-home"}},
    )
    captured = []
    monkeypatch.setattr(
        topic_router,
        "dispatch_to_safe_delegate",
        lambda envelope, *, board: captured.append((envelope, board)) or {"ok": True},
    )
    runner = GatewayRunner(GatewayConfig())
    monkeypatch.setattr(runner, "_is_user_authorized", lambda _source: False)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="unauthorized-user",
    )
    event = MessageEvent(text="Must not route.", source=source, message_id="outer-deny")

    response = await runner._handle_message(event)

    assert response is None
    assert captured == []


@pytest.mark.asyncio
async def test_external_route_metadata_cannot_bypass_disabled_specialist_gate(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="External metadata must not authorize default execution.",
        source=source,
        message_id="8902",
        metadata={"department_route": {"resolution": "command"}},
    )

    async def should_not_run_default(*_args, **_kwargs):
        raise AssertionError("default agent execution must not run")

    monkeypatch.setattr(GatewayRunner, "_recover_telegram_topic_thread_id", should_not_run_default)
    response = await runner._handle_message_with_agent(event, source, "synthetic", 1)
    assert "did not default-execute" in response


@pytest.mark.asyncio
async def test_sensitive_topic_plaintext_is_not_written_to_gateway_log(monkeypatch, caplog):
    import logging
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="23",
        chat_type="group",
        user_id="synthetic-user",
    )
    secret = "SYNTHETIC-LEGAL-PLAINTEXT-MUST-NOT-LOG"
    event = MessageEvent(text=secret, source=source, message_id="8903")

    with caplog.at_level(logging.INFO):
        response = await runner._handle_message_with_agent(event, source, "synthetic", 1)
    assert "did not default-execute" in response
    assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["specialist", "unknown_topic", "unverified_topic", "policy_error"])
async def test_canonical_noncommand_topic_cannot_default_execute_when_activation_missing(monkeypatch, resolution):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="Must not default execute while routing is inactive.",
        source=source,
        message_id="8901",
        metadata={"department_route": {"resolution": resolution}},
    )

    async def should_not_run_default(*_args, **_kwargs):
        raise AssertionError("default agent execution must not run")

    monkeypatch.setattr(GatewayRunner, "_recover_telegram_topic_thread_id", should_not_run_default)
    response = await runner._handle_message_with_agent(event, source, "synthetic", 1)
    assert "did not default-execute" in response


@pytest.mark.asyncio
async def test_policy_error_never_reaches_default_agent_when_topic_routing_is_enabled(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"department_topic_routing": {"enabled": True, "board": "chris-home"}},
    )
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="Synthetic policy-error caller-path canary.",
        source=source,
        message_id="9001",
        metadata={"department_route": {"resolution": "policy_error"}},
    )

    async def should_not_run_default(*_args, **_kwargs):
        raise AssertionError("default agent execution must not run")

    monkeypatch.setattr(GatewayRunner, "_recover_telegram_topic_thread_id", should_not_run_default)
    response = await runner._handle_message_with_agent(event, source, "synthetic", 1)
    assert "did not default-execute" in response


@pytest.mark.asyncio
async def test_enabled_specialist_topic_preserves_canonical_handoff_envelope(monkeypatch):
    """Exercise the live pre-session caller path without creating a board card."""
    import gateway.department_topic_router as topic_router
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"department_topic_routing": {"enabled": True, "board": "chris-home"}},
    )
    captured = []

    def capture_delegate(envelope, *, board):
        captured.append((envelope, board))
        return {"ok": True, "mode": "contained-caller-path-canary"}

    async def should_not_run_default(*_args, **_kwargs):
        raise AssertionError("default agent execution must not run")

    monkeypatch.setattr(topic_router, "dispatch_to_safe_delegate", capture_delegate)
    monkeypatch.setattr(GatewayRunner, "_recover_telegram_topic_thread_id", should_not_run_default)
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="11",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="Synthetic engineering caller-path canary: inspect only.",
        source=source,
        message_id="9011",
    )

    response = await runner._handle_message_with_agent(event, source, "synthetic", 1)

    assert "accountable department workflow" in response
    assert len(captured) == 1
    envelope, board = captured[0]
    assert board == "chris-home"
    assert envelope["runtime_assignee"] == "scarlett"
    assert envelope["tenant"] == "chris-home"
    assert envelope["source_chat_id"] == "-1004479003480"
    assert envelope["source_thread_id"] == "11"
    assert envelope["source_message_id"] == "9011"
    assert envelope["return_surface"] == "telegram:-1004479003480:11"
    assert envelope["privacy_tier"] == "team"
    assert envelope["required_reviewers"] == ["grace"]
    assert envelope["idempotency_key"]
    assert envelope["default_execution_forbidden"] is True


@pytest.mark.asyncio
async def test_mission_control_caller_path_preserves_claire_package_and_worker_plan(monkeypatch):
    """Topic 27 must package through Claire/Sol before its routine worker."""
    import gateway.department_topic_router as topic_router
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"department_topic_routing": {"enabled": True, "board": "chris-home"}},
    )
    captured = []

    def capture_delegate(envelope, *, board):
        captured.append((envelope, board))
        return {"ok": True, "mode": "contained-mission-control-canary"}

    async def should_not_run_default(*_args, **_kwargs):
        raise AssertionError("default agent execution must not run")

    monkeypatch.setattr(topic_router, "dispatch_to_safe_delegate", capture_delegate)
    monkeypatch.setattr(GatewayRunner, "_recover_telegram_topic_thread_id", should_not_run_default)
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004479003480",
        thread_id="27",
        chat_type="group",
        user_id="synthetic-user",
    )
    event = MessageEvent(
        text="Synthetic mission-control engineering automation task; no live action.",
        source=source,
        message_id="99027",
    )

    response = await runner._handle_message_with_agent(event, source, "synthetic", 1)

    assert "accountable department workflow" in response
    assert len(captured) == 1
    envelope, board = captured[0]
    assert board == "chris-home"
    assert envelope["source_thread_id"] == "27"
    assert envelope["return_surface"] == "telegram:-1004479003480:27"
    assert envelope["work_plan"] == {
        "assignee": "scarlett",
        "complexity": "simple",
        "with_review": True,
        "package_author": "claire",
    }
    command = topic_router.build_safe_delegate_command(envelope, board=board)
    assert "--coordination-package-chain" in command
    assert command[command.index("--package-author") + 1] == "claire"

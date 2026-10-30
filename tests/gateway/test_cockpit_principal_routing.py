"""Authenticated cockpit routing contracts (no live server or credentials)."""
from __future__ import annotations

import pytest

from gateway.cockpit_handoff import (
    CockpitHandoffError,
    build_cockpit_handoff,
    validate_cockpit_handoff,
)
from gateway.cockpit_user_routing import (
    CockpitRoutingError,
    bind_cockpit_principal,
)
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile


class _Request(dict):
    def __init__(self, token: str):
        super().__init__()
        self.headers = {"Authorization": f"Bearer {token}"}
        self.remote = "127.0.0.1"
        self.path_qs = "/v1/chat/completions"


@pytest.mark.parametrize("principal", ["ava", "photon", "unknown", ""])
def test_unknown_or_agent_principal_fails_closed(principal):
    with pytest.raises(CockpitRoutingError):
        bind_cockpit_principal(principal)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_key", "agent-os:cockpit:connor"),
        ("requesting_user", "connor"),
        ("board", "connor-home"),
        ("audience", "connor"),
    ],
)
def test_principal_cannot_spoof_another_cockpit_lane(field, value):
    kwargs = {field: value}
    with pytest.raises(CockpitRoutingError):
        bind_cockpit_principal("chris", **kwargs)


def test_two_principals_resolve_to_distinct_canonical_lanes():
    chris = bind_cockpit_principal("chris")
    connor = bind_cockpit_principal("connor")
    assert (chris.session_key, chris.board, chris.audience) == (
        "agent-os:cockpit:chris", "chris-home", "chris"
    )
    assert (connor.session_key, connor.board, connor.audience) == (
        "agent-os:cockpit:connor", "connor-home", "connor"
    )


def test_api_auth_stamps_only_the_authenticated_profile_principal(monkeypatch, tmp_path):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "default-key-123456", "cockpit_principal": "chris"})
    )
    default_request = _Request("default-key-123456")
    assert adapter._check_auth(default_request) is None
    assert default_request["hermes.authenticated_principal"] == "chris"

    profile_home = tmp_path / "profiles" / "connor"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("API_SERVER_KEY=connor-key-123456\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _name: profile_home)
    token = _api_request_profile.set("connor")
    try:
        with adapter._profile_scope("connor"):
            connor_request = _Request("connor-key-123456")
            assert adapter._check_auth(connor_request) is None
            assert connor_request["hermes.authenticated_principal"] == "connor"
    finally:
        _api_request_profile.reset(token)


def test_api_route_binding_ignores_no_caller_identity_and_rejects_spoof():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "default-key-123456", "cockpit_principal": "chris"})
    )
    request = _Request("default-key-123456")
    assert adapter._check_auth(request) is None

    lane, error = adapter._bind_cockpit_request(request, {}, "agent-os:cockpit:chris")
    assert error is None
    assert lane.session_key == "agent-os:cockpit:chris"

    lane, error = adapter._bind_cockpit_request(
        request,
        {"requesting_user": "connor", "board": "connor-home", "audience": "connor"},
        "agent-os:cockpit:connor",
    )
    assert lane is None
    assert error.status == 403


def test_handoff_recipient_metadata_is_canonical_and_tamper_evident():
    handoff = build_cockpit_handoff(
        from_user="chris",
        to_user="connor",
        source_board="chris-home",
        suggested_ask="Review this decision",
        source_task_id="task-1",
        transferred_at="2026-08-22T00:00:00Z",
    )
    metadata = handoff.as_metadata()
    assert validate_cockpit_handoff(metadata) == handoff
    metadata["audience"] = "chris"
    with pytest.raises(CockpitHandoffError):
        validate_cockpit_handoff(metadata)

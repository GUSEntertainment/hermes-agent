import pytest

from gateway.cockpit_user_routing import (
    CockpitRoutingError,
    audience_for_task_metadata,
    normalize_cockpit_user,
    resolve_cockpit_lane,
)


def test_normalizes_only_configured_cockpit_user_syntax():
    assert normalize_cockpit_user(" Connor ") == "connor"
    assert normalize_cockpit_user("connor@example.com") is None
    assert normalize_cockpit_user("connor\nchris") is None


@pytest.mark.parametrize(
    ("value", "user", "board"),
    [
        ("chris", "chris", "margot-chat-lanes"),
        ("agent-os:cockpit:connor", "connor", "connor-cockpit-lanes"),
        ("agent-os:cockpit:joshgarcia", "joshgarcia", "josh-cockpit-lanes"),
    ],
)
def test_resolves_configured_user_to_its_only_lane(value, user, board):
    lane = resolve_cockpit_lane(value)
    assert lane.user == user
    assert lane.audience == user
    assert lane.board == board
    assert lane.session_key == f"agent-os:cockpit:{user}"


@pytest.mark.parametrize("value", [None, "agent-os:cockpit", "agent-os:cockpit:unknown", "unknown"])
def test_unknown_or_malformed_cockpit_identity_fails_closed(value):
    with pytest.raises(CockpitRoutingError):
        resolve_cockpit_lane(value)


def test_user_and_explicit_board_mismatch_fails_closed():
    with pytest.raises(CockpitRoutingError):
        resolve_cockpit_lane("connor", board="margot-chat-lanes")


def test_audience_validation_requires_all_cockpit_metadata_to_agree():
    metadata = {
        "requesting_user": "connor",
        "audience": "chris",
        "cockpit_board": "connor-cockpit-lanes",
        "source_session_key": "agent-os:cockpit:connor",
    }
    with pytest.raises(CockpitRoutingError):
        audience_for_task_metadata(metadata, board="connor-cockpit-lanes")


def test_legacy_explicit_board_is_chris_compatible_but_known_cockpit_board_maps_safely():
    assert audience_for_task_metadata({}, board="some-explicit-legacy-board") == "chris"
    assert audience_for_task_metadata({}, board="margot-chat-lanes") == "chris"
    assert audience_for_task_metadata({}, board="connor-cockpit-lanes") == "connor"

from __future__ import annotations

import pytest

from hermes_cli.command_boards import resolve_command_boards


def test_explicit_allowlist_is_trimmed_deduplicated_and_ordered():
    assert resolve_command_boards(
        {
            "kanban": {
                "command_boards": [
                    " chris-home ",
                    "josh-cockpit-lanes",
                    "chris-home",
                    "",
                ]
            }
        }
    ) == ("chris-home", "josh-cockpit-lanes")


@pytest.mark.parametrize(
    "value",
    ["chris-home", None, 123, ["chris-home", 123], ["valid", None]],
)
def test_malformed_explicit_allowlist_fails_closed(value):
    assert resolve_command_boards({"kanban": {"command_boards": value}}) == ()


def test_explicit_empty_allowlist_is_authoritative():
    assert resolve_command_boards(
        {"kanban": {"command_boards": [], "command_board": "legacy"}}
    ) == ()


def test_valid_legacy_single_board_is_trimmed():
    assert resolve_command_boards(
        {"kanban": {"command_board": " chris-home "}}
    ) == ("chris-home",)


@pytest.mark.parametrize("value", [None, "", "   ", 123, ["chris-home"]])
def test_present_malformed_legacy_value_fails_closed(value):
    assert resolve_command_boards({"kanban": {"command_board": value}}) == ()


def test_absent_configuration_uses_validated_default():
    assert resolve_command_boards({}) == ("chris-home",)
    assert resolve_command_boards({"kanban": {}}) == ("chris-home",)
    assert resolve_command_boards({}, default=" custom ") == ("custom",)


@pytest.mark.parametrize("default", ["", "   ", None, 123])
def test_invalid_default_does_not_select_a_board(default):
    assert resolve_command_boards({}, default=default) is None


def test_non_mapping_config_and_kanban_section_use_validated_default():
    assert resolve_command_boards(None) == ("chris-home",)
    assert resolve_command_boards({"kanban": []}) == ("chris-home",)

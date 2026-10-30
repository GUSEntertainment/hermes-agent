"""Scope evidence must validate the scoping verb's complement, not just its head."""
import pytest
from hermes_cli.auto_same_owner_recovery import _find_hold, _normalize

HOLD = [
    "Do not use this task to restart production.",
    "Do not repeat the restart.",
    "Do not reuse this card to deploy.",
    "Do not modify files and restart.",
    "Do not silently restart.",
    "Do not attempt to restart.",
    "Do not under any circumstances restart.",
    "Do not proceed.",
]
RECOVER = [
    "Do not use unrelated files.",
    "Do not repeat the same failed approach.",
    "Do not remove test coverage for restart behavior.",
    "Do not modify unrelated runtime files.",
    "Do not modify unrelated workspaces.",
]

@pytest.mark.parametrize("text", HOLD)
def test_holds(text):
    assert _find_hold(_normalize(text)) is not None

@pytest.mark.parametrize("text", RECOVER)
def test_recovers(text):
    assert _find_hold(_normalize(text)) is None

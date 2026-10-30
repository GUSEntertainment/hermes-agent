"""Round 14: descriptive noun-complement exemption is bound to the governing predicate."""
import pytest
from hermes_cli import auto_same_owner_recovery as r

HOLD = [
    "Do not use scripts for restarting production.",
    "Do not use helpers for deploying applications.",
    "Do not add tests for restarting production.",
    "Do not use this task for restarting production.",
    "Do not change the service by restarting production.",
    "Do not use this task to restart production.",
    "Do not repeat the restart.",
]
CLEAR = [
    "Do not remove test coverage for restarting services.",
    "Do not change documentation about deploying applications.",
    "Do not remove test coverage or documentation about deploying applications.",
    "Do not use unrelated files.",
    "Do not repeat the same failed approach.",
]

@pytest.mark.parametrize("text", HOLD)
def test_holds(text):
    assert r._find_hold(r._normalize(text)) is not None

@pytest.mark.parametrize("text", CLEAR)
def test_clear(text):
    assert r._find_hold(r._normalize(text)) is None

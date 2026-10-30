"""Round 17: nominal action words inside a preserved documentary object are adjuncts, not actions."""
import pytest
from hermes_cli import auto_same_owner_recovery as r

HOLD = [
    "Do not repeat the restart.",
    "Do not use deployment scripts for restarting production.",
    "Do not use restart helpers for deploying applications.",
    "Do not change the service by restarting production.",
    "Do not remove deployment documentation and restart services.",
    "Do not change restart test coverage, then deploy applications.",
    "Do not add restart tests to deploy production.",
]
CLEAR = [
    "Do not change deployment documentation about restarting services.",
    "Do not remove restart test coverage for deploying applications.",
    "Do not edit deploy notes.",
    "Do not remove production test coverage for restarting services.",
    "Do not change operator documentation about deploying applications.",
]

@pytest.mark.parametrize("text", HOLD)
def test_holds(text):
    assert r._find_hold(r._normalize(text)) is not None

@pytest.mark.parametrize("text", CLEAR)
def test_clear(text):
    assert r._find_hold(r._normalize(text)) is None

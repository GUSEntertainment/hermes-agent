"""Rounds 15-16 (domain noun adjuncts by role): open-class modifiers before documentary objects; instruments still hold."""
import pytest
from hermes_cli import auto_same_owner_recovery as r

HOLD = [
    "Do not use scripts for restarting production.",
    "Do not change deploy scripts for restarting production.",
    "Do not change the service by restarting production.",
    "Do not remove coverage and restart production.",
    "Do not use automated helpers for deploying applications.",
    "Do not add operator tests for restarting production.",
    "Do not use this task to restart production.",
    "Do not use production scripts for restarting services.",
    "Do not use service helpers for deploying applications.",
]
CLEAR = [
    "Do not remove production gateway documentation about restarting services.",
    "Do not remove production test coverage for restarting services.",
    "Do not change service documentation about deploying applications.",
    "Do not remove automated test coverage for restarting services.",
    "Do not change operator documentation about deploying applications.",
    "Do not edit the existing end-to-end smoke tests for restarting services.",
    "Do not remove test coverage for restarting services.",
    "Do not change documentation about deploying applications.",
]

@pytest.mark.parametrize("text", HOLD)
def test_holds(text):
    assert r._find_hold(r._normalize(text)) is not None

@pytest.mark.parametrize("text", CLEAR)
def test_clear(text):
    assert r._find_hold(r._normalize(text)) is None

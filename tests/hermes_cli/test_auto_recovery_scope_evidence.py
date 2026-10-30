from hermes_cli.auto_same_owner_recovery import _find_hold, _normalize
import pytest

@pytest.mark.parametrize("text", [
    "Do not attempt to restart.", "Do not under any circumstances restart.",
    "Do not try restarting.", "Do not proceed.", "Do not silently restart.",
    "Do not modify unrelated files or restart.", "Never bother to deploy.",
    "Do not foo the bar.",
])
def test_holds(text):
    assert _find_hold(_normalize(text))

@pytest.mark.parametrize("text", [
    "Repair the local index. Do not modify unrelated files.",
    "Repair the local index; do not repeat the same failed approach.",
    "Do not modify unrelated runtime files.", "Do not modify unrelated workspaces.",
    "Do not remove test coverage for restart behavior.",
])
def test_scope_recovers(text):
    assert _find_hold(_normalize(text)) is None

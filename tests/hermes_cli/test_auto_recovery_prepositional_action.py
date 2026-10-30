import pytest
from hermes_cli.auto_same_owner_recovery import _pure_scope, _COORD


def _scope(text):
    return _pure_scope([c for c in _COORD.split(text) if c and c.strip()])


@pytest.mark.parametrize("text", [
    "use this task for restarting production",
    "change the service by restarting production",
    "use this task for restart production",
    "change the service by restarted production",
    "use this task to restart production",
    "repeat the restart",
])
def test_action_after_preposition_holds(text):
    assert not _scope(text)


@pytest.mark.parametrize("text", [
    "remove test coverage for restart behavior",
    "modify unrelated runtime files",
    "use unrelated files",
    "repeat the same failed approach",
])
def test_descriptive_scope_recovers(text):
    assert _scope(text)

import pytest
from hermes_cli.auto_same_owner_recovery import _pure_scope, _COORD


def _scope(text):
    return _pure_scope([c for c in _COORD.split(text) if c and c.strip()])


@pytest.mark.parametrize("text", [
    "remove test coverage for restarting services",
    "change documentation about deploying applications",
    "remove test coverage for restart behavior",
    "use unrelated files",
])
def test_descriptive_object_complement_recovers(text):
    assert _scope(text)


@pytest.mark.parametrize("text", [
    "use this task for restarting production",
    "change the service by restarting production",
    "use this task to restart production",
    "repeat the restart",
    "change the tests by restarting production",
    "change the docs to deploy applications",
])
def test_action_bound_to_non_descriptive_object_holds(text):
    assert not _scope(text)

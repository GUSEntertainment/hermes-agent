"""Boot the actual in-process adapter, without submitting a model turn."""
from pathlib import Path
from test_app_rooms import app
from test_app_rooms_runtime import service_module


def test_idle_worker_is_bound_without_desktop(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_PROFILE', 'default')
    import tui_gateway.hosted_room_service as original
    monkeypatch.setattr(original, 'HostedRoomService', service_module.HostedRoomService)
    db=Path(app.hosted_rooms.default_db_path())
    assert db.is_relative_to(tmp_path)
    instance=app._service(None,db)
    try:
        assert instance.runtime.status()['running']
        assert not instance.bindings()
    finally:
        from tui_gateway import methods_groups
        methods_groups.stop_hosted_room_service(timeout=3)
        app._services.clear()

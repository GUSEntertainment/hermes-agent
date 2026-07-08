"""Kanban dashboard API handling for corrupt board databases."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_corrupt_db_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_board_endpoint_returns_actionable_error_for_corrupt_db(monkeypatch, tmp_path):
    mod = _load_plugin_module()
    db_path = tmp_path / "kanban.db"
    backup_path = tmp_path / "kanban.db.corrupt.demo.bak"
    error = kb.KanbanDbCorruptError(
        db_path,
        backup_path,
        "integrity_check returned 'wrong # of entries in index idx_events_task'",
    )

    def raise_corrupt(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(mod.kanban_db, "init_db", raise_corrupt)
    monkeypatch.setattr(mod.kanban_db, "connect", raise_corrupt)

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/plugins/kanban/board")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "kanban_db_corrupt",
            "message": str(error),
            "db_path": str(db_path),
            "backup_path": str(backup_path),
            "reason": error.reason,
        }
    }

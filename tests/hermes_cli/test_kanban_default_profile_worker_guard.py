"""Fail-closed guard for Kanban workers that would share root state.db."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    return home


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_ready_default_task_is_parked_once_without_spawning(kanban_home: Path) -> None:
    spawned: list[str] = []

    def fake_spawn(task, _workspace):
        spawned.append(task.id)
        return 43210

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="direct worker-created task must not share state",
            assignee="Default",
            created_by="worker",
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        task = kb.get_task(conn, task_id)
        assert spawned == []
        assert first.blocked_unsafe_profile == [task_id]
        assert first.spawned == []
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        payloads = _event_payloads(conn, task_id, "blocked")
        assert len(payloads) == 1
        assert "root Hermes home" in payloads[0]["reason"]
        assert "Reassign" in payloads[0]["reason"]

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert second.blocked_unsafe_profile == []
        assert kb.get_task(conn, task_id).status == "blocked"
        assert len(_event_payloads(conn, task_id, "blocked")) == 1
        assert spawned == []


def test_specialist_task_still_spawns(kanban_home: Path) -> None:
    spawned: list[str] = []

    def fake_spawn(task, _workspace):
        spawned.append(task.id)
        return 43211

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="specialist work", assignee="scarlett")
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        assert spawned == [task_id]
        assert [item[0] for item in result.spawned] == [task_id]
        assert result.blocked_unsafe_profile == []
        assert kb.get_task(conn, task_id).status == "running"


def test_default_fallback_is_assigned_then_parked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="dashboard fallback", assignee=None, created_by="dashboard",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("default worker spawned"),
            default_assignee="default",
        )

        task = kb.get_task(conn, task_id)
        assert result.auto_assigned_default == [task_id]
        assert result.blocked_unsafe_profile == [task_id]
        assert result.spawned == []
        assert task is not None
        assert task.assignee == "default"
        assert task.status == "blocked"


def test_dependency_promoted_default_task_is_parked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="scarlett")
        child = kb.create_task(
            conn,
            title="default child",
            assignee="default",
            parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        assert kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("default worker spawned"),
        )
        assert result.blocked_unsafe_profile == [child]
        assert kb.get_task(conn, child).status == "blocked"


def test_reclaimed_default_task_is_parked_before_retry_spawn(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="reclaimed default", assignee="default")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.reclaim_task(conn, task_id, reason="test retry")
        assert kb.get_task(conn, task_id).status == "ready"

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("default worker spawned"),
        )
        assert result.blocked_unsafe_profile == [task_id]
        assert kb.get_task(conn, task_id).status == "blocked"


def test_default_review_task_is_parked_without_review_worker(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="default review", assignee="default")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("default review worker spawned"),
        )
        assert result.blocked_unsafe_profile == [task_id]
        assert kb.get_task(conn, task_id).status == "blocked"


def test_default_spawn_backstop_never_calls_popen_or_opens_root_writer(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="backstop", assignee="default")
        task = kb.get_task(conn, task_id)

    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Popen must not be reached")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="root Hermes home"):
        kb._default_spawn(task, str(kanban_home))
    assert called is False
    assert not (kanban_home / "state.db").exists()


def test_default_profile_remains_valid_for_interactive_gateway(
    kanban_home: Path,
) -> None:
    assert profiles.normalize_profile_name("Default") == "default"
    assert profiles.profile_exists("default") is True
    assert Path(profiles.resolve_profile_env("default")) == kanban_home

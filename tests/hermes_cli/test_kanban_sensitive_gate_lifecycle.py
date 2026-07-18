"""Regression coverage for safety-sensitive Kanban lifecycle holds."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decompose
from hermes_cli import kanban_specify as specify


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


def _block_into_loop_triage(conn) -> str:
    task_id = kb.create_task(conn, title="approval-gated task", assignee="worker")
    assert kb.claim_task(conn, task_id, claimer="worker:one") is not None
    assert kb.block_task(conn, task_id, reason="need approval", kind="needs_input")
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id, claimer="worker:two") is not None
    assert kb.block_task(conn, task_id, reason="still need approval", kind="needs_input")
    assert _status(conn, task_id) == "triage"
    return task_id


def test_initially_blocked_task_never_auto_promotes_without_unblock(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="await explicit approval",
            assignee="worker",
            initial_status="blocked",
        )
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, task_id) == "blocked"
        assert [event.kind for event in kb.list_events(conn, task_id)] == [
            "created",
            "blocked",
        ]


def test_loop_escalation_cannot_be_auto_specified_or_decomposed(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = _block_into_loop_triage(conn)

    assert specify.list_triage_ids() == []
    assert decompose.list_triage_ids() == []

    with kb.connect_closing() as conn:
        assert kb.specify_triage_task(conn, task_id, body="must not bypass approval") is False
        assert kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="worker",
            children=[{"title": "unsafe child"}],
        ) is None
        assert _status(conn, task_id) == "triage"
        # An explicit unblock is the sole supported recovery path; it preserves
        # the loop history while returning the existing task to normal gating.
        assert kb.unblock_task(conn, task_id)
        assert _status(conn, task_id) == "ready"


def test_decomposition_inherits_open_root_parent_gate(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        approval_hold = kb.create_task(conn, title="approval hold", assignee="steward")
        root = kb.create_task(conn, title="gated triage", triage=True)
        kb.link_tasks(conn, approval_hold, root)

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "first child"},
                {"title": "second child", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None
        assert [_status(conn, child_id) for child_id in child_ids] == [
            "todo",
            "todo",
        ]
        assert all(
            approval_hold in kb.parent_ids(conn, child_id) for child_id in child_ids
        )

        assert kb.complete_task(conn, approval_hold, result="approved")
        assert _status(conn, child_ids[0]) == "ready"
        assert _status(conn, child_ids[1]) == "todo"

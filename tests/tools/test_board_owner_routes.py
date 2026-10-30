import json

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_PROFILE", "HERMES_SESSION_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def test_native_triage_error_exposes_supported_continuation(board):
    tid = kb.create_task(board, title="Local repair", assignee="maker", triage=True)
    with kb.write_txn(board):
        kb._append_event(board, tid, "blocked", {"reason": "Tool unavailable"})
    kb.add_comment(board, tid, "owner", "Resume the authorized local repair after tool verification")
    result = json.loads(kt._handle_unblock({"task_id": tid}))
    assert result["code"] == "triage_resolution_required"
    route = result["board_administration"]
    assert "resume-triage" in route["command_templates"]["resume_dry_run"]
    assert "--dry-run" in route["command_templates"]["resume_dry_run"]
    assert route["latest_stop_event_id"] > 0
    assert route["latest_comment_id"] > 0
    assert len(route["resume_snapshot_sha256"]) == 64
    assert kb.get_task(board, tid).status == "triage"


def test_native_show_names_unsatisfied_dependency_and_owner(board):
    parent = kb.create_task(board, title="Prepare required input", assignee="input-owner", triage=True)
    child = kb.create_task(board, title="Consume input", assignee="maker", parents=[parent])
    result = json.loads(kt._handle_show({"task_id": child}))
    assert result["board_administration"]["waiting_on"] == [
        {"task_id": parent, "title": "Prepare required input", "status": "triage", "owner": "input-owner"}]
    assert all("id" in e for e in result["events"])
    assert "archive" in result["board_administration"]["command_templates"]


def test_worker_does_not_receive_orchestrator_routes(board, monkeypatch):
    tid = kb.create_task(board, title="Worker task", assignee="maker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert kt._owner_board_routes(kb, board, kb.get_task(board, tid)) is None
    result = json.loads(kt._handle_unblock({"task_id": tid}))
    assert "error" in result


def test_retirement_is_not_dependency_completion_but_real_completion_releases(board):
    parent = kb.create_task(board, title="Prerequisite", assignee="maker")
    child = kb.create_task(board, title="Dependent", assignee="maker", parents=[parent])
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='archived',completed_at=NULL WHERE id=?", (parent,))
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == "todo"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET completed_at=1 WHERE id=?", (parent,))
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == "ready"

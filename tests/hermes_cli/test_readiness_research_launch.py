"""Regression: the preparer must be able to verify its research prerequisite."""
import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb, kanban_completion as completion
from hermes_cli.worker_readiness import preparation_launch_surface


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("HERMES_PROFILE", "riley")
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def pair(conn, original_body=None):
    if original_body is None:
        routing = {"delegation_package": {"workspace_and_tools": {
            "allowed_toolsets": ["web", "file", "terminal", "kanban", "skills"]}}}
        original_body = "Public research only.\n## Routing metadata\n```json\n" + json.dumps(routing) + "\n```"
    original = kb.create_task(conn, title="Research official sources", body=original_body,
                              assignee="riley", tenant="research")
    prep = kb.create_task(conn, title="Prepare worker access: " + original,
                         body='repair\n```worker-requirements\n{"toolsets":["file","kanban"]}\n```',
                         assignee="riley", tenant="research", created_by="worker-readiness")
    conn.execute("UPDATE tasks SET owner_profile='riley' WHERE id IN (?,?)", (prep, original))
    conn.execute("INSERT INTO task_links(parent_id,child_id,edge_role) VALUES(?,?,'dependency')", (prep, original))
    kb._append_event(conn, prep, "readiness_preparation_for", {"task_id": original})
    conn.commit()
    return original, prep


def selection(conn, prep, admitted=None):
    return kb._readiness_preparation_launch_toolsets(
        kb.get_task(conn, prep), board=None,
        admitted_toolsets=admitted or ["file", "kanban", "web", "terminal", "skills", "browser", "metricool"],
        admission_contract={"task": prep},
    )


def test_research_preparer_keeps_only_task_required_admitted_tools(board):
    _, prep = pair(board)
    assert set(selection(board, prep)) == {"file", "kanban", "web", "terminal", "skills"}
    assert selection(board, prep, ["file", "kanban"]) == ["file", "kanban"]


def test_declared_offline_phase_overrides_old_research_package(board):
    original, prep = pair(board)
    body = kb.get_task(board, original).body + '\n```worker-requirements\n{"toolsets":["file"]}\n```'
    board.execute("UPDATE tasks SET body=? WHERE id=?", (body, original)); board.commit()
    assert selection(board, prep) == ["file", "kanban"]


def test_service_and_execution_prerequisites_survive_selection(board):
    req = {"toolsets": ["file"], "mcp": {"metricool": []}, "commands": ["python3"], "browser_session": True}
    _, prep = pair(board, '```worker-requirements\n' + json.dumps(req) + '\n```')
    assert set(selection(board, prep)) == {"file", "kanban", "terminal", "browser", "metricool"}


@pytest.mark.parametrize("change", ["edge", "owner", "tenant", "ordinary", "contract"])
def test_invalid_preparation_binding_cannot_select_tools(board, change):
    original, prep = pair(board)
    if change == "edge": board.execute("DELETE FROM task_links WHERE parent_id=?", (prep,))
    elif change == "owner": board.execute("UPDATE tasks SET owner_profile='someone-else' WHERE id=?", (original,))
    elif change == "tenant": board.execute("UPDATE tasks SET tenant='other' WHERE id=?", (original,))
    elif change == "ordinary": board.execute("UPDATE tasks SET created_by='user' WHERE id=?", (prep,))
    else: board.execute("UPDATE tasks SET body=? WHERE id=?", ('```worker-requirements\n{"toolsets":["web"]}\n```', prep))
    board.commit()
    assert selection(board, prep) is None


def test_fingerprint_tracks_real_required_tools_without_resetting_retry_budget(board):
    original, prep = pair(board)
    limits = {**completion.DEFAULTS, "max_attempts_per_input": 1}
    completion.record_admission(board, prep, limits)
    assert completion.admission_reason(board, prep, limits) == "completion_repeated_input_budget"
    board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (prep,)); board.commit()
    assert completion.admission_reason(board, prep, limits) == "completion_repeated_input_budget"
    board.execute("UPDATE tasks SET body=? WHERE id=?", ('```worker-requirements\n{"toolsets":["file"]}\n```', original)); board.commit()
    assert completion.admission_reason(board, prep, limits) is None
    completion.record_admission(board, prep, limits)
    assert completion.admission_reason(board, prep, limits) == "completion_repeated_input_budget"


def test_real_spawn_argv_keeps_web_and_original_limits(board, monkeypatch, tmp_path):
    _, prep = pair(board)
    captured = {}
    class Process:
        pid = 424242
    def capture(cmd, **kwargs):
        captured.update(cmd=cmd, env=kwargs["env"])
        return Process()
    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(completion, "policy", lambda: {**completion.DEFAULTS, "task_budgets": {}})
    task = kb.get_task(board, prep)
    kb._default_spawn(task, str(tmp_path), board=None,
                      capability_toolsets=["file", "web", "terminal", "kanban", "skills", "metricool"],
                      admission_contract={"task": prep, "workspace_confinement": False})
    args = captured["cmd"]
    assert set(args[args.index("--toolsets") + 1].split(",")) == {"web", "file", "terminal", "kanban", "skills"}
    assert captured["env"]["HERMES_KANBAN_TASK"] == prep
    assert captured["env"]["HERMES_KANBAN_REQUEST_TOKEN_LIMIT"] == "32000"
    assert captured["env"]["HERMES_KANBAN_TOTAL_TOKEN_LIMIT"] == "120000"

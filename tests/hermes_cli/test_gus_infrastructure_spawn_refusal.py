"""[GUS v0.21.5 port] #114720 on the customized dispatcher, platform-independent:
a host refusal (RestartSafeScopeUnavailable) never charges the card."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli.test_kanban_db import kanban_home  # noqa: F401  (fixture)


def test_custom_dispatch_infrastructure_refusal_is_free(kanban_home, monkeypatch, all_assignees_spawnable):
    from tools.process_registry import RestartSafeScopeUnavailable
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")

    def refuse(task, workspace, board=None):
        raise RestartSafeScopeUnavailable("no user bus; enable-linger")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bus is down", assignee="a")
        for _ in range(3):
            assert kbd.dispatch_once(conn, spawn_fn=refuse, failure_limit=2).auto_blocked == []
        row = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["consecutive_failures"]) == ("ready", 0)
        runs = conn.execute("SELECT outcome, metadata FROM task_runs WHERE task_id=?", (tid,)).fetchall()
        assert [r["outcome"] for r in runs] == ["spawn_failed"] * 3
        assert all(json.loads(r["metadata"])["infrastructure"] is True for r in runs)

        def boom(task, workspace, board=None):
            raise RuntimeError("ordinary failure")
        kbd.dispatch_once(conn, spawn_fn=boom, failure_limit=2)
        assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 1

"""t_df999f30: a repeated-input held card leaves ready visibly and a same-owner
requeue is a fresh, dispatchable revision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_completion as kc
from gateway import owner_attention as attention


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as c:
        yield c


def test_held_card_leaves_ready_and_requeue_is_fresh(conn):
    tid = kb.create_task(conn, title="t", assignee="riley")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    limits = {"max_attempts_per_input": 1, "budget_scope": "task:" + tid}
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "completion_admission", {
            "input_fingerprint": kc.input_fingerprint(conn, tid), "budget_scope": "task:" + tid})
    assert kc.admission_reason(conn, tid, limits) == "completion_repeated_input_budget"

    assert kc.hold_repeated_input(conn, tid) is True
    task = kb.get_task(conn, tid)
    assert task.status == "blocked" and task.block_kind == "transient"
    assert "already ran once" in (conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()[0])

    assert attention.same_owner_retry(conn, tid, None) is True
    task = kb.get_task(conn, tid)
    assert task.status in {"ready", "todo"} and task.assignee == "riley"
    assert kc.admission_reason(conn, tid, limits) is None
    # Not ready any more -> no second hold event.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
    assert kc.hold_repeated_input(conn, tid) is False


def test_needs_input_hold_is_not_requeued(conn):
    tid = kb.create_task(conn, title="t", assignee="riley")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    kb.block_task(conn, tid, kind="needs_input", reason="Needs Chris approval")
    assert attention.same_owner_retry(conn, tid, None) is False

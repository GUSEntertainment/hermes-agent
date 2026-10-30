"""Regression: unchanged dependencies/stops must not churn events or triage
rewrites across repeated dispatcher passes (t_4359056c)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _count(conn, tid, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)
    ).fetchone()[0]


def test_stale_signature_ignores_root_status_flips():
    base = {"task_id": "c", "truncated": False, "max_nodes": 1, "max_roots": 1}
    a = dict(base, roots=[{"root_id": "p", "owner": "q", "edge_role": "dependency",
                           "path": ["c", "p"], "status": "todo", "reason": "parent_todo",
                           "block_kind": "needs_input"}])
    b = dict(base, roots=[{"root_id": "p", "owner": "q", "edge_role": "dependency",
                           "path": ["c", "p"], "status": "running", "reason": "parent_running",
                           "block_kind": "capability"}])
    c = dict(base, roots=[{"root_id": "x", "owner": "q", "edge_role": "dependency",
                           "path": ["c", "x"], "status": "todo", "reason": "parent_todo",
                           "block_kind": None}])
    assert kb._dependency_stale_signature(a) == kb._dependency_stale_signature(b)
    assert kb._dependency_stale_signature(a) != kb._dependency_stale_signature(c)


def test_repeated_passes_on_churning_root_emit_one_stale_event(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="root", assignee="quinn")
        child = kb.create_task(conn, title="child", assignee="lauren", parents=[parent])
        for status in ("todo", "ready", "running", "triage", "todo", "ready") * 5:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, parent))
            kb._append_dependency_stale_chain_event(
                conn, child, kb.dependency_stale_chain(conn, child))
        assert _count(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT) == 1


def test_loop_stopped_triage_is_not_auto_specified(kanban_home):
    with kbc.connect_closing() as conn:
        fresh = kb.create_task(conn, title="new one-liner", triage=True)
        stopped = kb.create_task(conn, title="advisory", body="ORIGINAL", triage=True)
        kb._append_event(conn, stopped, "block_loop_detected",
                         {"kind": "capability", "recurrences": 3})
        held = kb.create_task(conn, title="held", triage=True)
        kb._append_event(conn, held, "blocked", {"kind": "needs_input"})
        assert kb.is_initial_triage(conn, fresh)
        assert not kb.is_initial_triage(conn, stopped)
        assert not kb.is_initial_triage(conn, held)
    assert decomp.list_triage_ids() == [fresh]
    assert spec.list_triage_ids() == [fresh]
    # Automatic author cannot rewrite even when called directly; no LLM call made.
    out = decomp.decompose_task(stopped, author="auto-decomposer")
    assert not out.ok and "not initial triage" in out.reason
    with kbc.connect_closing() as conn:
        t = kb.get_task(conn, stopped)
        assert t.status == "triage" and t.body == "ORIGINAL"
        assert _count(conn, stopped, "specified") == 0


def test_dedupe_against_legacy_signature_event(kanban_home):
    """A prior event written by the old (status-sensitive) scheme still dedupes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="root", assignee="quinn")
        child = kb.create_task(conn, title="child", assignee="lauren", parents=[parent])
        report = kb.dependency_stale_chain(conn, child)
        legacy = dict(report, signature="legacy-status-sensitive-hash")
        kb._append_event(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT, legacy)
        for status in ("ready", "running", "todo"):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, parent))
            kb._append_dependency_stale_chain_event(
                conn, child, kb.dependency_stale_chain(conn, child))
        assert _count(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT) == 1

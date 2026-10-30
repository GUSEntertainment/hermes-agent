"""Retry course-correction: first failure, repeated fingerprint, distinct
failure, and preservation of holds/stops/privacy (t_64ae1aaf)."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import retry_course_correction as rc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    c = kb.connect(tmp_path / "kanban.db")
    yield c
    c.close()


def _task(conn, body="do the thing"):
    return kb.create_task(conn, title="t", body=body, assignee="scarlett")


def _fail(conn, tid, reason, kind="transient", run_id=None):
    conn.execute("INSERT INTO task_runs(task_id, profile, status, started_at, ended_at) "
                 "VALUES (?,?,?,?,?)", (tid, "scarlett", "blocked", 1, 2))
    rid = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
    kb._append_event(conn, tid, "blocked", {"kind": kind, "reason": reason}, run_id=rid)
    conn.commit()


def _start_run(conn, tid):
    conn.execute("INSERT INTO task_runs(task_id, profile, status, started_at) VALUES (?,?,?,?)",
                 (tid, "scarlett", "running", 3))
    rid = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
    conn.commit()
    return rid


def test_first_run_has_no_retry_block(conn):
    tid = _task(conn)
    _start_run(conn, tid)
    assert rc.context_lines(conn, tid) == []
    assert rc.gate_error(conn, tid, "kanban_complete") is None


def test_first_failure_leads_context_without_note_requirement(conn):
    tid = _task(conn)
    _fail(conn, tid, "restart gate found no quiet window")
    _start_run(conn, tid)
    ctx = kb.build_worker_context(conn, tid)
    head = ctx.split("## RETRY")[0]
    assert "## RETRY: previous attempt failed" in ctx
    assert "## Body" not in head  # leads before body/handoffs
    assert "restart gate found no quiet window" in ctx
    assert "Do NOT repeat" in ctx
    assert rc.gate_error(conn, tid, "kanban_complete") is None


def test_repeated_fingerprint_requires_change_note(conn):
    tid = _task(conn)
    _fail(conn, tid, "same tool error X")
    _fail(conn, tid, "same   tool error X")  # whitespace-normalized identical
    _start_run(conn, tid)
    assert rc.state(conn, tid)["repeated"]
    assert "SAME failure has now repeated" in kb.build_worker_context(conn, tid)
    assert "refused" in rc.gate_error(conn, tid, "kanban_block")
    assert rc.record_change_note(conn, tid, "hello", "scarlett") is None
    assert rc.record_change_note(conn, tid, "What I changed this time: x", "s") is None  # too short
    note = rc.record_change_note(
        conn, tid, "What I changed this time: cause was stale PATH; now call the release python", "scarlett")
    assert note.startswith("cause was stale PATH")
    assert rc.gate_error(conn, tid, "kanban_complete") is None
    assert "Recorded change note" in kb.build_worker_context(conn, tid)


def test_note_from_earlier_run_does_not_satisfy_new_run(conn):
    tid = _task(conn)
    _fail(conn, tid, "E")
    _fail(conn, tid, "E")
    _start_run(conn, tid)
    rc.record_change_note(conn, tid, "What I changed this time: tried approach two", "s")
    _fail(conn, tid, "E")
    _start_run(conn, tid)
    assert rc.gate_error(conn, tid, "kanban_complete")


def test_distinct_failure_needs_no_note(conn):
    tid = _task(conn)
    _fail(conn, tid, "error A")
    _fail(conn, tid, "error B")
    _start_run(conn, tid)
    s = rc.state(conn, tid)
    assert s["retry"] and not s["repeated"]
    assert rc.gate_error(conn, tid, "kanban_complete") is None


def test_preserves_history_holds_assignee_and_privacy(conn):
    body = "Do not send anything until Chris approves the payment."
    tid = _task(conn, body)
    _fail(conn, tid, "same", kind="capability")
    _fail(conn, tid, "same", kind="capability")
    _start_run(conn, tid)
    before = conn.execute("SELECT id,kind,payload FROM task_events WHERE kind='blocked' AND task_id=?",
                          (tid,)).fetchall()
    rc.record_change_note(conn, tid, "What I changed this time: repaired tool config first", "s")
    after = conn.execute("SELECT id,kind,payload FROM task_events WHERE kind='blocked' AND task_id=?",
                         (tid,)).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    t = kb.get_task(conn, tid)
    assert t.assignee == "scarlett" and t.body == body
    ctx = kb.build_worker_context(conn, tid)
    assert "Do not send anything until Chris approves" in ctx
    assert "grants no new authority" in ctx
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                            "('block_loop_detected','triaged')", (tid,)).fetchone()
    # note is redacted/bounded text only
    ev = conn.execute("SELECT payload FROM task_events WHERE kind=?", (rc.CHANGE_NOTE_EVENT,)).fetchone()
    assert set(json.loads(ev[0])) == {"note", "author"}


def test_create_rejects_nonexistent_profile_assignee(tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    home = tmp_path / "home"
    (home / "profiles" / "scarlett").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kt, "_require_orchestrator_tool", lambda *_: None, raising=False)
    out = json.loads(kt._handle_create({"title": "x", "assignee": "margot"}))
    assert "not an existing profile" in out.get("error", "")

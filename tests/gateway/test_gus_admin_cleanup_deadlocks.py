"""t_9ebe1c65: cleanup deadlock prevention.

Separates already-granted work, technical failures, active independent
checks and genuinely protected decisions so obsolete items can be archived
(reversibly) and resolved stops resume without re-asking Chris.
"""
import json

import pytest

from gateway import gus_admin_cli as adm
from hermes_cli import kanban_db as kb

PEER = {"task": "t_peer_own", "profile": "claire", "board": "test"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    monkeypatch.setattr(adm, "_assignment", lambda c, w: None)
    tok = adm._active.set(PEER)
    with kb.connect_closing() as c:
        yield c
    adm._active.reset(tok)


def _mk(conn, title, assignee="scarlett", block=None, reason="", status=None):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    if block:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', block_kind=? WHERE id=?", (block, tid))
            kb._append_event(conn, tid, "blocked", {"reason": reason})
    if status:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def _history(conn, tid):
    return (conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0])


def test_technical_block_is_not_missing_consent_and_archive_restore_round_trips(conn):
    tid = _mk(conn, "Per-agent orbs", block="capability", reason="needs Chris to sign in at the app browser")
    kind, plain = adm.classify_hold(conn, kb.get_task(conn, tid))
    assert kind == "technical" and "not missing consent" in plain
    kb.add_comment(conn, tid, "claire", "evidence")
    ev, cm = _history(conn, tid)
    assert kb.archive_task(conn, tid, reason="superseded by t_x")
    assert kb.get_task(conn, tid).status == "archived"
    assert kb.restore_task(conn, tid, reason="needed again") == "blocked"
    ev2, cm2 = _history(conn, tid)
    assert cm2 == cm and ev2 == ev + 3  # archived, workspace_preserved, restored; nothing erased


def test_obsolete_stale_review_projection_can_be_archived(conn):
    tid = _mk(conn, "Archive blocked business cards", assignee="grace", status="triage")
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO owner_cell_goals(task_id,goal_id,owner_profile,owner_role_alias,runtime_profile,"
            "security_realm,router_mode,risk_class,phase,capability_bundle,worker_budget_json,callback_json,"
            "approval_gates_json,contract_json,scope_fingerprint,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
            (tid, "g1", "grace", "qa", "grace", "chris", "reviewed", "low", "review", "b", "{}", "{}", "{}", "{}", "f"))
    # Stale review projection on an idle card: unblock/promote still refused...
    with pytest.raises(PermissionError, match="Preserve active review"):
        kb.unblock_task(conn, tid)
    # ...but reversible archive is allowed, so it is no longer physically stuck.
    assert kb.archive_task(conn, tid, reason="obsolete")


def test_self_clear_prohibited(conn):
    tid = _mk(conn, "Claire's own access fix", assignee="claire", block="capability", reason="tooling")
    with pytest.raises(PermissionError, match="cannot clear its own"):
        kb.unblock_task(conn, tid)


def test_no_approval_laundering_for_protected_decisions(conn):
    tid = _mk(conn, "Send capital proposal", block="needs_input", reason="approve payment to investors")
    with pytest.raises(PermissionError, match="Needs Chris to decide"):
        kb.unblock_task(conn, tid)
    with pytest.raises(PermissionError, match="stays visible"):
        kb.archive_task(conn, tid, reason="tidy")
    assert kb.get_task(conn, tid).status == "blocked"


def test_connor_hold_preserved(conn):
    tid = _mk(conn, "Invite Connor to app", block="needs_input", reason="Connor no-invite hold")
    with pytest.raises(PermissionError):
        kb.unblock_task(conn, tid)


def test_already_granted_work_resumes_without_reapproval(conn):
    tid = _mk(conn, "Bottom bar fix", block="needs_input", reason="waiting on QA reader repair")
    assert adm.classify_hold(conn, kb.get_task(conn, tid))[0] == "granted"
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.get_task(conn, tid).status in {"ready", "todo"}


def test_active_work_and_review_protected(conn):
    run = _mk(conn, "Live build", status="running")
    rev = _mk(conn, "Under QA", status="review")
    for tid in (run, rev):
        with pytest.raises(PermissionError, match="Preserve active review"):
            kb.archive_task(conn, tid, reason="x")


def test_archive_refuses_live_dependents(conn):
    parent = _mk(conn, "Prep", status="todo")
    child = kb.create_task(conn, title="Real work", assignee="scarlett", parents=[parent])
    with pytest.raises(PermissionError, match="live dependency"):
        kb.archive_task(conn, parent, reason="x")
    assert kb.get_task(conn, child).status != "archived"


def test_repeated_stop_does_not_need_repeated_consent(conn):
    tid = _mk(conn, "Recall chain", block="capability", reason="stale backend target")
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "blocked", {"reason": "stale backend target"})
    # Second identical stop: still technical, still resumable by a peer, no new consent asked.
    assert adm.classify_hold(conn, kb.get_task(conn, tid))[0] == "technical"
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)


def test_restore_only_archived(conn):
    tid = _mk(conn, "Plain", status="todo")
    with pytest.raises(PermissionError, match="Only archived"):
        kb.restore_task(conn, tid)


# --- t_1cc0200c: guarded triage -> todo (specify) ---

def test_specify_moves_peer_triage_card_to_todo_with_dependency_gating(conn):
    parent = _mk(conn, "Open parent")
    tid = kb.create_task(conn, title="Cleanup inventory follow-up", assignee="grace", parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    assert kb.specify_triage_task(conn, tid, author="claire")
    assert kb.get_task(conn, tid).status == "todo"  # open parent keeps it gated


def test_specify_refuses_protected_self_active_and_non_triage(conn):
    prot = _mk(conn, "Approve vendor payment", assignee="grace", status="triage")
    with pytest.raises(PermissionError, match="Needs Chris"):
        kb.specify_triage_task(conn, prot)
    assert kb.get_task(conn, prot).status == "triage"
    own = _mk(conn, "Claire triage", assignee="claire", status="triage")
    with pytest.raises(PermissionError, match="cannot clear its own"):
        kb.specify_triage_task(conn, own)
    todo = _mk(conn, "Already todo", assignee="grace")
    with pytest.raises(PermissionError):
        kb.specify_triage_task(conn, todo)
    act = _mk(conn, "Active", assignee="grace", status="triage")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=123 WHERE id=?", (act,))
    with pytest.raises(PermissionError, match="Active tasks"):
        kb.specify_triage_task(conn, act)


def test_specify_cli_refuses_blanket_sweep(monkeypatch):
    import argparse
    monkeypatch.setattr("gateway.gus_worker_authority.current", lambda: PEER, raising=False)
    args = argparse.Namespace(kanban_action="specify", all_triage=True, task_id=None, reason="x", board="test")
    monkeypatch.setattr("gateway.josh_board_authority.admitted_board", lambda b: "test", raising=False)
    with pytest.raises(PermissionError):
        adm._proof(args)


def test_specify_without_open_parents_follows_normal_ready_gating(conn):
    tid = _mk(conn, "Parentless triage", assignee="grace", status="triage")
    assert kb.specify_triage_task(conn, tid, author="claire")
    assert kb.get_task(conn, tid).status in {"todo", "ready"}
    assert kb.get_task(conn, tid).title == "Parentless triage"  # no rewrite

"""t_017ce141: technical stalls stay with their owner and retry on the same card."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
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


def _running(conn, assignee="riley"):
    tid = kb.create_task(conn, title="t", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer=assignee) is not None
    return tid


def _again(conn, tid, who="riley"):
    kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer=who) is not None


def test_technical_block_stays_blocked_with_same_owner_past_old_limit(conn):
    tid = _running(conn)
    kb.block_task(conn, tid, kind="capability", reason="Reader missing")
    _again(conn, tid)
    kb.block_task(conn, tid, kind="transient", reason="Mac load high")
    t = kb.get_task(conn, tid)
    assert t.status == "blocked" and t.assignee == "riley"
    assert t.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT


def test_technical_block_reaches_triage_only_at_bounded_limit(conn):
    tid = _running(conn)
    for i in range(kb.TECHNICAL_BLOCK_RECURRENCE_LIMIT):
        if i:
            _again(conn, tid)
        kb.block_task(conn, tid, kind="capability", reason=f"error {i}")
    t = kb.get_task(conn, tid)
    assert t.status == "triage" and t.assignee == "riley"
    ev = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]
    assert ev.payload["limit"] == kb.TECHNICAL_BLOCK_RECURRENCE_LIMIT


def test_needs_input_loop_still_goes_to_triage_at_original_limit(conn):
    tid = _running(conn)
    kb.block_task(conn, tid, kind="needs_input", reason="Which date do you want?")
    _again(conn, tid)
    kb.block_task(conn, tid, kind="needs_input", reason="Which date do you want?")
    assert kb.get_task(conn, tid).status == "triage"


def test_operator_routing_retries_same_card_same_owner_and_is_bounded(conn):
    tid = _running(conn)
    for n in range(attention.SAME_OWNER_AUTO_RETRIES + 1):
        if n:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready', block_recurrences=0 WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="riley") is not None
        kb.block_task(conn, tid, kind="capability", reason="worker capability admission failed")
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
        attention._route_to_operator(conn, tid, ev.id, None)
        t = kb.get_task(conn, tid)
        assert t.assignee == "riley"  # never Claire/Steven
        if n < attention.SAME_OWNER_AUTO_RETRIES:
            assert t.status in {"ready", "todo"}
        else:
            assert t.status == "blocked"  # bounded: stays visible, no loop
    routed = [e for e in kb.list_events(conn, tid) if e.kind == "operator_attention_routed"]
    assert routed and all(e.payload["route"] == "same_owner_recovery" and e.payload["owner"] == "riley" for e in routed)
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "same_owner_retry"]) == attention.SAME_OWNER_AUTO_RETRIES


def test_needs_input_hold_is_never_auto_retried(conn):
    tid = _running(conn)
    kb.block_task(conn, tid, kind="needs_input", reason="Approve the capital action?")
    ev = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
    assert attention.same_owner_retry(conn, tid, ev.id) is False
    assert kb.get_task(conn, tid).status == "blocked"

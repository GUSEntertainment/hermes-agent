"""t_df999f30 round 2: changes_requested / new work-order revisions are fresh
input, and the hourly automatic lane issues a dispatchable revision itself."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_completion as kc
from hermes_cli import owner_work_start as ows
from hermes_cli import auto_same_owner_recovery as auto


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as c:
        yield c


LIMITS = lambda tid: {"max_attempts_per_input": 1, "budget_scope": "task:" + tid}


def _admit(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "completion_admission", {
            "input_fingerprint": kc.input_fingerprint(conn, tid), "budget_scope": "task:" + tid})


def _looped_triage(conn, title="fix dispatcher", reason="evidence admission failed"):
    tid = kb.create_task(conn, title=title, assignee="riley", body="do the work")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    _admit(conn, tid)  # it already ran once on these instructions
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',block_kind='transient' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "blocked", {"reason": reason, "kind": "transient"})
    return tid


def test_changes_requested_is_new_input(conn):
    tid = kb.create_task(conn, title="t", assignee="riley", body="b")
    _admit(conn, tid)
    # Repeated-input caps were removed by the Oct 11 directive: admission is open.
    assert kc.admission_reason(conn, tid, LIMITS(tid)) is None
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "changes_requested", {"reason": "fix x"})
    assert kc.admission_reason(conn, tid, LIMITS(tid)) is None


def test_hourly_lane_recovers_looped_triage_card_to_dispatch(conn):
    tid = _looped_triage(conn)
    # Repeated-input caps were removed by the Oct 11 directive: admission is open.
    assert kc.admission_reason(conn, tid, LIMITS(tid)) is None
    later = int(time.time()) + 2 * 3600
    report = auto.run(conn, now=later)
    assert {"task": tid, "action": "recovered"}.items() <= next(r for r in report if r["task"] == tid).items()
    task = kb.get_task(conn, tid)
    assert task.status in {"ready", "todo"} and task.assignee == "riley"
    order = ows.current(conn, tid)
    assert order is not None and order["recovery"] is True
    assert ows.allows_claim(conn, task) is True or task.status == "todo"
    assert kc.admission_reason(conn, tid, LIMITS(tid)) is None  # dispatchable

    # Recurrence: the worker runs, fails the same way -> no unchanged auto retry.
    _admit(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',block_kind='transient' WHERE id=?", (tid,))
    stop = ows._stop(conn, tid)
    # same stop event still latest -> refused
    rep = dict((r["task"], r) for r in auto.run(conn, now=later + 7200))
    assert rep[tid]["action"] == "hold" and "already auto-recovered" in rep[tid]["detail"]
    assert stop is not None


def test_daily_cap(conn):
    tid = _looped_triage(conn)
    t = int(time.time()) + 3600
    for i in range(auto.MAX_PER_DAY):
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {"reason": f"fail {i}", "kind": "transient"})
            conn.execute("UPDATE tasks SET status='triage',block_kind='transient' WHERE id=?", (tid,))
        t += 3600 * 2
        assert dict((r["task"], r) for r in auto.run(conn, now=t))[tid]["action"] == "recovered"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"reason": "again", "kind": "transient"})
        conn.execute("UPDATE tasks SET status='triage',block_kind='transient' WHERE id=?", (tid,))
    r = dict((r["task"], r) for r in auto.run(conn, now=t + 7200))[tid]
    assert r["action"] == "hold" and "cap" in r["detail"]


def test_protected_and_needs_input_untouched(conn):
    cap = _looped_triage(conn, title="GUS capital action", reason="wire transfer")
    ni = kb.create_task(conn, title="x", assignee="riley", body="b")
    kb.block_task(conn, ni, kind="needs_input", reason="Needs Chris")
    rep = dict((r["task"], r) for r in auto.run(conn, now=int(time.time()) + 7200))
    assert rep[cap]["action"] == "hold"
    assert ni not in rep
    assert kb.get_task(conn, cap).status == "triage"
    assert kb.get_task(conn, ni).status == "blocked"


def test_cooldown(conn):
    tid = _looped_triage(conn)
    rep = dict((r["task"], r) for r in auto.run(conn))
    assert rep[tid]["action"] == "wait"


def test_identical_recurrence_held_without_new_evidence(conn):
    tid = _looped_triage(conn)
    t = int(time.time()) + 7200
    assert dict((r["task"], r) for r in auto.run(conn, now=t))[tid]["action"] == "recovered"
    with kb.write_txn(conn):
        last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                            (tid, auto.ows.EVENT)).fetchone()[0]
        prior = (json.loads(last).get("preserved_stop") or {}).get("reason")
        kb._append_event(conn, tid, "blocked", {"reason": prior, "kind": "transient"})
        conn.execute("UPDATE tasks SET status='triage',block_kind='transient' WHERE id=?", (tid,))
    r = dict((r["task"], r) for r in auto.run(conn, now=t + 7200))[tid]
    assert r["action"] == "hold" and "no new evidence" in r["detail"]


def test_body_or_comment_hold_blocks_recovery(conn):
    tid = _looped_triage(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body='Then publish it to Instagram.' WHERE id=?", (tid,))
    r = dict((r["task"], r) for r in auto.run(conn, now=int(time.time()) + 7200))[tid]
    assert r["action"] == "hold"

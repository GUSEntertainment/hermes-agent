import sqlite3
import pytest
from gateway.kanban_dispatch_wake import CommittedTransitions

@pytest.mark.parametrize("event,state", [
    ("completed", "done"), ("triage_resumed", "ready"), ("review_triage_resumed", "review"),
    ("owner_work_requested", "ready"), ("owner_work_started", "ready"), ("owner_handoff", "ready"),
    ("owner_recovery_requested", "ready"),
])
def test_recovered_work_wakes_once_from_committed_transition(tmp_path,event,state):
    path=tmp_path/"board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT)")
        conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT)")
        conn.execute("INSERT INTO tasks VALUES('card','triage')")
    observer=CommittedTransitions()
    assert not observer.changed([path])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id='card'",(state,))
        conn.execute("INSERT INTO task_events(task_id,kind) VALUES('card',?)",(event,))
    assert observer.changed([path])
    assert not observer.changed([path])


def test_held_work_does_not_wake_an_expensive_dispatch(tmp_path):
    path=tmp_path/"board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT)")
        conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT)")
        conn.execute("INSERT INTO tasks VALUES('card','triage')")
    observer=CommittedTransitions()
    assert not observer.changed([path])
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO task_events(task_id,kind) VALUES('card','owner_work_started')")
    assert not observer.changed([path])

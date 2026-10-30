"""Crash-atomic opt-in authority for target-native review decisions."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _claimed_review(conn, *, contract=None):
    task_id = kb.create_task(conn, title="Atomic review", assignee="builder")
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="ready",
        expected_run_id=implementation.current_run_id,
        review_decision_contract=contract,
    )
    review = kb.claim_review_task(
        conn,
        task_id,
        claimer="reviewer:1",
    )
    assert review is not None
    kb._set_worker_pid(conn, task_id, os.getpid())
    return task_id, review


def _event_count(conn, task_id, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()[0]


def test_review_decision_authority_is_default_off(conn):
    task_id, review = _claimed_review(conn)
    assert review.review_decision_capability is None
    assert kb.complete_task(
        conn,
        task_id,
        summary="ordinary target approval",
        expected_run_id=review.current_run_id,
    )
    assert _event_count(conn, task_id, "review_approved") == 0


def test_review_decision_contract_must_match_exactly(conn):
    task_id = kb.create_task(conn, title="Exact contract", assignee="builder")
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    with pytest.raises(ValueError, match="exact review decision contract"):
        kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
            review_decision_contract={"version": 1},
        )
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "running"
    assert task.current_run_id == implementation.current_run_id


def test_approval_event_and_completion_rollback_without_consuming_capability(
    conn, monkeypatch
):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    capability = review.review_decision_capability
    assert capability

    original_append = kb._append_event

    def fail_after_review_verdict(db, event_task_id, kind, *args, **kwargs):
        original_append(db, event_task_id, kind, *args, **kwargs)
        if kind == "review_approved":
            raise sqlite_failure("injected failure after review verdict")

    class sqlite_failure(RuntimeError):
        pass

    monkeypatch.setattr(kb, "_append_event", fail_after_review_verdict)
    with pytest.raises(sqlite_failure, match="injected failure"):
        kb.complete_task(
            conn,
            task_id,
            summary="PASS",
            expected_run_id=review.current_run_id,
            review_decision_capability=capability,
        )

    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "running"
    assert task.current_run_id == review.current_run_id
    assert _event_count(conn, task_id, "review_approved") == 0
    assert _event_count(conn, task_id, "completed") == 0
    assert kb._has_review_decision_capability(conn, task_id, capability)

    monkeypatch.setattr(kb, "_append_event", original_append)
    assert kb.complete_task(
        conn,
        task_id,
        summary="PASS",
        expected_run_id=review.current_run_id,
        review_decision_capability=capability,
    )
    assert kb.get_task(conn, task_id).status == "done"
    assert _event_count(conn, task_id, "review_approved") == 1
    assert _event_count(conn, task_id, "completed") == 1
    assert not kb._has_review_decision_capability(conn, task_id, capability)
    with pytest.raises(ValueError, match="capability mismatch"):
        kb.complete_task(
            conn,
            task_id,
            review_decision_capability=capability,
        )


def test_request_changes_rolls_back_run_and_event_without_consuming_capability(
    conn, monkeypatch
):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    capability = review.review_decision_capability
    original_append = kb._append_event

    def fail_after_event(db, event_task_id, kind, *args, **kwargs):
        original_append(db, event_task_id, kind, *args, **kwargs)
        if kind == "changes_requested":
            raise RuntimeError("injected changes failure")

    monkeypatch.setattr(kb, "_append_event", fail_after_event)
    with pytest.raises(RuntimeError, match="injected changes failure"):
        kb.request_changes(
            conn,
            task_id,
            reason="fix it",
            expected_run_id=review.current_run_id,
            review_decision_capability=capability,
        )

    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "running"
    assert task.current_run_id == review.current_run_id
    run = kb.latest_run(conn, task_id)
    assert run is not None and run.status == "running" and run.ended_at is None
    assert _event_count(conn, task_id, "changes_requested") == 0
    assert kb._has_review_decision_capability(conn, task_id, capability)

    monkeypatch.setattr(kb, "_append_event", original_append)
    assert kb.request_changes(
        conn,
        task_id,
        reason="fix it",
        expected_run_id=review.current_run_id,
        review_decision_capability=capability,
    ) == (True, "builder")
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "ready"
    assert task.current_run_id is None
    assert task.claim_lock is None and task.claim_expires is None
    run = kb.latest_run(conn, task_id)
    assert run is not None and run.outcome == "changes_requested"
    # v0.21.5 upstream: the closed run keeps its claim_lock as the only host
    # evidence reap_terminal_workers needs to end a hung reviewer; the TASK
    # claim (asserted above) is what must be released.
    assert run.ended_at is not None and run.claim_lock == review.claim_lock
    assert not kb._has_review_decision_capability(conn, task_id, capability)
    assert kb.request_changes(
        conn,
        task_id,
        reason="replay",
        review_decision_capability=capability,
    )[0] is False


def test_failed_precondition_preserves_capability_for_corrected_retry(conn):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    capability = review.review_decision_capability
    with pytest.raises(ValueError, match="capability mismatch"):
        kb.complete_task(
            conn,
            task_id,
            expected_run_id=review.current_run_id,
            review_decision_capability="wrong-token",
        )
    assert kb._has_review_decision_capability(conn, task_id, capability)
    assert kb.complete_task(
        conn,
        task_id,
        expected_run_id=review.current_run_id,
        review_decision_capability=capability,
    )


def test_reclaimed_capability_is_replaced_for_the_next_contracted_claim(conn):
    task_id, first_review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    stale_capability = first_review.review_decision_capability
    assert kb._record_spawn_failure(
        conn,
        task_id,
        "reviewer exited",
        failure_limit=3,
    ) is False
    assert kb.get_task(conn, task_id).status == "review"

    replacement = kb.claim_review_task(conn, task_id, claimer="reviewer:2")
    assert replacement is not None
    kb._set_worker_pid(conn, task_id, os.getpid())
    replacement_capability = replacement.review_decision_capability
    assert replacement_capability and replacement_capability != stale_capability
    assert not kb._has_review_decision_capability(conn, task_id, stale_capability)
    assert kb.request_changes(
        conn,
        task_id,
        reason="replacement contracted review",
        expected_run_id=replacement.current_run_id,
        review_decision_capability=replacement_capability,
    ) == (True, "builder")


def test_review_decision_capability_rejects_expired_claim(conn):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    capability = review.review_decision_capability
    conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,))
    conn.execute(
        "UPDATE task_runs SET claim_expires = 1 WHERE id = ?",
        (review.current_run_id,),
    )

    with pytest.raises(ValueError, match="active review run"):
        kb.complete_task(
            conn,
            task_id,
            expected_run_id=review.current_run_id,
            review_decision_capability=capability,
        )
    assert kb.get_task(conn, task_id).status == "running"


def test_review_decision_capability_rejects_worker_pid_mismatch(conn):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    capability = review.review_decision_capability
    mismatched_pid = os.getppid()
    assert mismatched_pid != os.getpid()
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?", (mismatched_pid, task_id)
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
        (mismatched_pid, review.current_run_id),
    )

    with pytest.raises(ValueError, match="active review run"):
        kb.request_changes(
            conn,
            task_id,
            reason="must not cross worker ownership",
            expected_run_id=review.current_run_id,
            review_decision_capability=capability,
        )
    assert kb.get_task(conn, task_id).status == "running"


def test_review_decision_capability_rejects_dead_worker(conn):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    worker = subprocess.Popen(["sleep", "30"])
    try:
        kb._set_worker_pid(conn, task_id, worker.pid)
        worker.terminate()
        worker.wait(timeout=5)

        with pytest.raises(ValueError, match="active review run"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=review.current_run_id,
                review_decision_capability=review.review_decision_capability,
            )
        assert kb.get_task(conn, task_id).status == "running"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_review_decision_capability_is_scoped_to_canonical_database(conn, tmp_path):
    task_id, review = _claimed_review(
        conn, contract=kb.REVIEW_DECISION_CONTRACT_V1,
    )
    source_path = conn.execute("PRAGMA database_list").fetchone()[2]
    copied_path = tmp_path / "other-board.db"
    copied = kb.connect(copied_path)
    try:
        conn.backup(copied)
        with pytest.raises(ValueError, match="capability mismatch"):
            kb.complete_task(
                copied,
                task_id,
                expected_run_id=review.current_run_id,
                review_decision_capability=review.review_decision_capability,
            )
        assert kb.get_task(copied, task_id).status == "running"
        assert os.path.realpath(source_path) != os.path.realpath(copied_path)
    finally:
        copied.close()


def test_review_decision_capability_crosses_real_subprocess_and_is_single_use(conn, tmp_path):
    task_id, review = _claimed_review(conn, contract=kb.REVIEW_DECISION_CONTRACT_V1)
    token = review.review_decision_capability
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    gate = tmp_path / "go"
    code = textwrap.dedent("""
        import os, pathlib
        from hermes_cli import kanban_db as kb
        while not pathlib.Path(os.environ['GATE']).exists():
            pass
        with kb.contextlib.closing(kb.connect(pathlib.Path(os.environ['DB']))) as conn:
            ok = kb.complete_task(
                conn, os.environ['TASK'], summary='subprocess PASS',
                expected_run_id=int(os.environ['RUN']),
                review_decision_capability=os.environ['TOKEN'],
            )
        raise SystemExit(0 if ok else 3)
    """)
    env = dict(os.environ)
    env.update(DB=db_path, TASK=task_id, RUN=str(review.current_run_id), TOKEN=token, GATE=str(gate))
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        kb._set_worker_pid(conn, task_id, child.pid)
        gate.write_text("go")
        assert child.wait(timeout=15) == 0
        assert kb.get_task(conn, task_id).status == "done"
        assert not kb._has_review_decision_capability(conn, task_id, token)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_contracted_review_never_defaults_open_after_connection_restart(conn):
    task_id, review = _claimed_review(conn, contract=kb.REVIEW_DECISION_CONTRACT_V1)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    restarted = kb.connect(Path(db_path))
    try:
        with pytest.raises(ValueError, match="capability mismatch"):
            kb.complete_task(
                restarted, task_id, summary="must reject missing handoff",
                expected_run_id=review.current_run_id,
            )
        assert kb.get_task(restarted, task_id).status == "running"
    finally:
        restarted.close()


def test_reviewer_tool_consumes_subprocess_handoff_authority(conn, monkeypatch):
    task_id, review = _claimed_review(conn, contract=kb.REVIEW_DECISION_CONTRACT_V1)
    payload = {
        "version": "hermes.review-decision-handoff/v1",
        "task": task_id,
        "run_id": review.current_run_id,
        "profile": review.assignee,
        "token": review.review_decision_capability,
    }
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(payload).encode())
    os.close(write_fd)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("HERMES_REVIEW_DECISION_FD", str(read_fd))
    monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", str(review.assignee))

    from tools.kanban_tools import _handle_request_changes
    result = json.loads(_handle_request_changes({"reason": "fix boundary"}))
    assert result["ok"] is True
    assert kb.get_task(conn, task_id).status == "ready"

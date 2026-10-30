"""Actual failure identity on isolated native boards; no models or workers."""
import pytest
from hermes_cli import kanban_db as kb, board_owner_manage as control
from tests.hermes_cli.test_simple_owner_work import board, request


def task(conn):
    return kb.create_task(conn, title="Prepare saved local output",
        body="Use the existing local source. No external action requested.", assignee="maker")


def resume(conn, tid):
    assert kb.unblock_task(conn, tid, preserve_failure_history=True)
    assert kb.get_task(conn, tid).status == "ready"


def test_independent_errors_in_one_category_do_not_fake_a_loop(board):
    tid = task(board)
    budget = '{"execution_budget":{"max_runs_per_day":2,"max_total_tokens":2000}}'
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET consecutive_failures=3,max_retries=4,worker_budget_json=? WHERE id=?",
                     (budget, tid))
    assert kb.block_task(board, tid, kind="capability", reason="Reader account missing")
    first = kb.latest_worker_block(board, tid)
    resume(board, tid)
    assert kb.block_task(board, tid, kind="capability", reason="Source file missing")
    current = kb.get_task(board, tid)
    assert current.status == "triage" and current.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.latest_worker_block(board, tid)["failure_fingerprint"] != first["failure_fingerprint"]
    assert current.consecutive_failures == 3 and current.max_retries == 4
    assert current.worker_budget_json == budget
    stop = kb.latest_worker_block(board, tid)
    assert stop["same_failure"] is False
    assert stop["escalation_basis"] == "blocked_attempt_budget"


def test_same_normalized_error_still_stops_the_repeat(board):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader   account\nmissing")
    first = kb.latest_worker_block(board, tid)
    resume(board, tid)
    assert kb.block_task(board, tid, kind="capability", reason=" Reader account missing  ")
    current = kb.get_task(board, tid)
    assert current.status == "triage"
    assert current.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.latest_worker_block(board, tid)["failure_fingerprint"] == first["failure_fingerprint"]
    assert kb.latest_worker_block(board, tid)["same_failure"] is True
    assert kb.latest_worker_block(board, tid)["escalation_basis"] == "repeated_failure"


@pytest.mark.parametrize("reason", ["Source ID 18 missing", "source ID 17 missing", "Source ID 17 missing."])
def test_identity_preserves_causal_ids_case_and_punctuation(reason):
    assert kb.block_failure_fingerprint("capability", reason) != kb.block_failure_fingerprint("capability", "Source ID 17 missing")


def test_kind_is_part_of_failure_identity(board):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader unavailable")
    resume(board, tid)
    assert kb.block_task(board, tid, kind="transient", reason="Reader unavailable")
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.latest_worker_block(board, tid)["same_failure"] is False
    assert kb.latest_worker_block(board, tid)["escalation_basis"] == "blocked_attempt_budget"


def test_owner_hold_is_not_a_worker_failure(board):
    tid = task(board)
    control.execute(dict(action="block", task_id=tid, kind="capability", reason="Reader missing"))
    assert kb.latest_worker_block(board, tid) is None
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
    request(tid, "Use the supplied source for this existing request.")
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.get_task(board, tid).block_recurrences == 1


@pytest.mark.parametrize("next_reason,same_failure", [("Reader missing", True), ("Source missing", False)])
def test_intervening_owner_hold_neither_erases_nor_fakes_previous_error(board, next_reason, same_failure):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    first = kb.latest_worker_block(board, tid)
    control.execute(dict(action="block", task_id=tid, kind="needs_input", reason=next_reason))
    assert kb.latest_worker_block(board, tid) == first
    request(tid, "Use the supplied source for this existing request.")
    assert kb.block_task(board, tid, kind="capability", reason=next_reason)
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.latest_worker_block(board, tid)["same_failure"] is same_failure


@pytest.mark.parametrize("payload", [None, "not json", "[]", '{"kind":"capability"}',
    '{"kind":"capability","reason":"  "}', '{"kind":"capability","reason":["Reader missing"]}',
    '{"kind":{},"reason":"Reader missing"}'])
def test_missing_or_malformed_legacy_reason_cannot_replenish_repeat_limit(board, payload):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    event = kb.latest_worker_block(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload=? WHERE id=?", (payload, event["event_id"]))
    assert kb.latest_worker_block(board, tid)["failure_fingerprint"] is None
    resume(board, tid)
    assert kb.block_task(board, tid, kind="capability", reason="Some newly recorded detail")
    assert kb.get_task(board, tid).status == "triage"


@pytest.mark.parametrize("reason", [None, "", "  ", "capability"])
def test_missing_or_generic_current_reason_keeps_conservative_limit(board, reason):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    resume(board, tid)
    assert kb.block_failure_fingerprint("capability", reason) is None
    assert kb.block_task(board, tid, kind="capability", reason=reason)
    assert kb.get_task(board, tid).status == "triage"


def test_legacy_counter_without_event_stays_conservative(board):
    tid = task(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET block_kind='capability',block_recurrences=? WHERE id=?",
                     (kb.BLOCK_RECURRENCE_LIMIT - 1, tid))
    assert kb.latest_worker_block(board, tid) is None
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    assert kb.get_task(board, tid).status == "triage"


def test_owner_hold_cannot_reset_ambiguous_legacy_worker_failure(board):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader missing")
    event = kb.latest_worker_block(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload=NULL WHERE id=?", (event["event_id"],))
    control.execute(dict(action="block", task_id=tid, kind="needs_input", reason="Supply local source"))
    request(tid, "Use the supplied local input.")
    assert kb.block_task(board, tid, kind="capability", reason="Newly recorded failure detail")
    assert kb.get_task(board, tid).status == "triage"


def test_legacy_unblock_cannot_replenish_attempts_by_changing_kind_or_words(board):
    tid = task(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET consecutive_failures=3 WHERE id=?", (tid,))
    assert kb.block_task(board, tid, kind="capability", reason="Reader unavailable")
    assert kb.unblock_task(board, tid)  # ordinary legacy API clears dispatcher failures
    assert kb.get_task(board, tid).consecutive_failures == 0
    assert kb.block_task(board, tid, kind="transient", reason="Different temporary problem")
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.latest_worker_block(board, tid)["escalation_basis"] == "blocked_attempt_budget"
    assert kb.unblock_task(board, tid) is False


def test_successful_native_completion_resets_existing_block_counter(board):
    tid = task(board)
    assert kb.block_task(board, tid, kind="capability", reason="Reader unavailable")
    assert kb.get_task(board, tid).block_recurrences == 1
    prior = kb.latest_worker_block(board, tid)
    assert kb.complete_task(board, tid, result="Completed from the existing local source.")
    finished = kb.get_task(board, tid)
    assert finished.status == "done" and finished.block_recurrences == 0
    assert kb.latest_worker_block(board, tid) == prior  # original failure evidence retained


@pytest.fixture(autouse=True)
def _legacy_conservative_technical_limit(monkeypatch):
    # These contracts pin loop-guard identity at the original limit; the
    # t_017ce141 same-owner technical allowance is covered in
    # test_same_owner_recovery.py.
    monkeypatch.setattr(kb, "TECHNICAL_BLOCK_RECURRENCE_LIMIT", kb.BLOCK_RECURRENCE_LIMIT)

"""Owner Cell integration regressions against the current Kanban lifecycle.

All databases and homes are disposable. Review decisions use the current
process-bound, digest-backed review capability; no process-local registry or
legacy review wrapper is exercised here.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import owner_cell as oc


def contract(**overrides):
    value = {
        "goal_id": "qa-goal",
        "objective": "isolated regression fixture",
        "owner_profile": "scarlett",
        "owner_role_alias": "engineering_systems",
        "runtime_profile": "scarlett",
        "required_reviewer_profile": "grace",
        "security_realm": "company",
        "router_mode": "reviewed",
        "risk_class": "material",
        "capability_bundle": "engineering",
        "worker_budget": {"producers": 1, "reviewers": 1, "extras": 0},
        "targets": [],
        "acceptance_test": {"expected": "pass"},
        "rollback": {"procedure_ref": "fixture://rollback"},
        "callback": {
            "source_surface": "test",
            "return_surface": "test",
            "report_policy": "milestones_only",
        },
        "approval_gates": [],
    }
    value.update(overrides)
    return value


def disposable_conn(tmp_path, monkeypatch):
    for name in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "kanban.db"
    oc.assert_disposable_db_path(db_path)
    return kb.connect(db_path)


def request_and_claim_review(conn, task_id, *, reviewer="grace", ref="artifact", digest="digest"):
    task = kb.get_task(conn, task_id)
    assert task is not None
    expected_run_id = task.current_run_id if task.status == "running" else None
    assert kb.request_review(
        conn,
        task_id,
        summary="artifact ready",
        reviewer=reviewer,
        metadata={"owner_cell_artifact": {"ref": ref, "digest": digest}},
        expected_run_id=expected_run_id,
    )
    claimed = kb.claim_review_task(conn, task_id, claimer=f"{reviewer}:review")
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, os.getpid())
    assert claimed.review_decision_capability
    return claimed


def pass_metadata(*, ref="artifact", digest="digest", state_version=1):
    return {"owner_cell_verdict": {
        "state_version": state_version,
        "artifact_ref": ref,
        "artifact_digest": digest,
        "verdict": "PASS",
        "defects": [],
    }}


def test_claim_rejects_target_mutation_without_token_handoff(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn,
        title="lease",
        assignee="scarlett",
        owner_cell_contract=contract(targets=[{
            "target_key": "repo://isolated/fixture",
            "access_mode": "mutate",
            "expected_version": "v1",
        }]),
    )
    with pytest.raises(oc.OwnerCellGuardError, match="target mutation token handoff"):
        kb.claim_task(conn, task_id, claimer="qa")
    assert kb.get_task(conn, task_id).status == "ready"
    assert conn.execute(
        "SELECT COUNT(*) FROM target_leases WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0


def test_dispatcher_path_guards_target_mutation_without_spawning(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(
        conn,
        title="dispatcher lease",
        assignee="scarlett",
        owner_cell_contract=contract(targets=[{
            "target_key": "repo://isolated/fixture",
            "access_mode": "mutate",
            "expected_version": "v1",
        }]),
    )
    result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kwargs: None)
    assert not any(item[0] == task_id for item in result.spawned)
    assert (task_id, "owner_cell_target_token_handoff_required") in result.respawn_guarded
    assert conn.execute(
        "SELECT COUNT(*) FROM target_leases WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0


def test_dispatcher_churn_guard_stops_spawn_and_dedupes_event(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(
        conn, title="dispatcher churn", assignee="scarlett",
        owner_cell_contract=contract(churn_budget={
            "cards": 10, "handoffs": 1, "elapsed_seconds": 3600,
            "non_cache_tokens": 1000, "acceptance_gaps_required": 1,
        }),
    )
    kb._append_event(conn, task_id, "assigned", {"assignee": "unplanned-owner"})
    first = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kwargs: None)
    second = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kwargs: None)
    assert (task_id, "stop_spawn") in first.respawn_guarded
    assert (task_id, "stop_spawn") in second.respawn_guarded
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND kind = 'owner_churn_circuit_breaker'", (task_id,),
    ).fetchone()[0] == 1


def test_owner_acceptance_completes_with_process_bound_exact_pass(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn, title="owner acceptance", assignee="engineering-maker",
        owner_cell_contract=contract(
            owner_profile="scarlett", runtime_profile="engineering-maker",
            required_reviewer_profile="scarlett",
            owner_acceptance={"mode": "accountable_owner"},
        ),
    )
    review = request_and_claim_review(
        conn, task_id, reviewer="scarlett", ref="artifact-owner", digest="digest-owner"
    )
    assert kb.complete_task(
        conn,
        task_id,
        summary="owner PASS",
        metadata=pass_metadata(ref="artifact-owner", digest="digest-owner"),
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    )
    assert kb.get_task(conn, task_id).status == "done"
    assert oc.goal_projection(conn, task_id)["phase"] == "completed"

def test_reviewed_completion_requires_process_bound_review_capability(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn,
        title="review authority cannot be omitted",
        assignee="scarlett",
        owner_cell_contract=contract(),
    )
    oc.begin_review(
        conn,
        task_id,
        reviewer_profile="grace",
        artifact_ref="artifact",
        artifact_digest="digest",
    )
    conn.execute(
        "UPDATE tasks SET status='review', assignee='grace' WHERE id=?",
        (task_id,),
    )
    conn.commit()

    with pytest.raises(oc.OwnerCellGuardError, match="review decision capability"):
        kb.complete_task(conn, task_id, metadata=pass_metadata())
    assert kb.get_task(conn, task_id).status == "review"
    assert conn.execute(
        "SELECT COUNT(*) FROM review_verdicts WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0


def test_verdict_and_completion_roll_back_together_on_injected_failure(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(conn, title="atomic verdict", assignee="scarlett", owner_cell_contract=contract())
    review = request_and_claim_review(conn, task_id)
    original = kb._append_event

    def fail_after_approval(db, event_task_id, kind, *args, **kwargs):
        original(db, event_task_id, kind, *args, **kwargs)
        if kind == "review_approved":
            raise RuntimeError("injected after verdict")

    monkeypatch.setattr(kb, "_append_event", fail_after_approval)
    with pytest.raises(RuntimeError, match="injected after verdict"):
        kb.complete_task(
            conn, task_id, metadata=pass_metadata(),
            expected_run_id=review.current_run_id,
            review_decision_capability=review.review_decision_capability,
        )
    assert kb.get_task(conn, task_id).status == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM review_verdicts WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0
    assert kb._has_review_decision_capability(
        conn, task_id, review.review_decision_capability
    )


def test_mismatched_pass_preserves_capability_for_corrected_retry(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(conn, title="artifact", assignee="scarlett", owner_cell_contract=contract())
    review = request_and_claim_review(conn, task_id, ref="artifact-A", digest="digest-A")
    with pytest.raises(oc.OwnerCellGuardError, match="artifact"):
        kb.complete_task(
            conn, task_id,
            metadata=pass_metadata(ref="artifact-B", digest="digest-B"),
            expected_run_id=review.current_run_id,
            review_decision_capability=review.review_decision_capability,
        )
    assert kb._has_review_decision_capability(
        conn, task_id, review.review_decision_capability
    )
    assert kb.complete_task(
        conn, task_id,
        metadata=pass_metadata(ref="artifact-A", digest="digest-A"),
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    )


def test_request_changes_reuses_same_card_and_reclaims_for_rework(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(conn, title="same card", assignee="scarlett", owner_cell_contract=contract())
    implementation = kb.claim_task(conn, task_id, claimer="maker")
    assert implementation is not None
    review = request_and_claim_review(conn, task_id)
    assert kb.request_changes(
        conn,
        task_id,
        reason="repair artifact",
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    ) == (True, "scarlett")
    task = kb.get_task(conn, task_id)
    projection = oc.goal_projection(conn, task_id)
    assert task.status == "ready" and task.assignee == "scarlett"
    assert projection["phase"] == "rework"
    assert projection["review_loop_count"] == 1
    assert oc.count_goal_cards(conn, projection["goal_id"]) == 1
    reclaimed = kb.claim_task(conn, task_id, claimer="maker-rework")
    assert reclaimed is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM target_leases WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0


def test_owner_acceptance_request_changes_returns_to_original_maker(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn, title="owner rework", assignee="engineering-maker",
        owner_cell_contract=contract(
            owner_profile="scarlett", runtime_profile="engineering-maker",
            required_reviewer_profile="scarlett",
            owner_acceptance={"mode": "accountable_owner"},
        ),
    )
    review = request_and_claim_review(conn, task_id, reviewer="scarlett")
    assert kb.request_changes(
        conn, task_id, reason="repair",
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    ) == (True, "engineering-maker")
    task = kb.get_task(conn, task_id)
    projection = oc.goal_projection(conn, task_id)
    assert task.assignee == "engineering-maker"
    assert projection["owner_profile"] == "scarlett"
    assert projection["runtime_profile"] == "engineering-maker"




def test_normal_reviewer_request_changes_routes_back_to_distinct_maker_without_escalating_privilege(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn,
        title="wren rework",
        assignee="engineering-wren",
        owner_cell_contract=contract(
            owner_profile="scarlett",
            runtime_profile="engineering-wren",
            required_reviewer_profile="grace",
            approval_gates=["external_publish"],
            targets=[{
                "target_key": "repo://isolated/fixture",
                "access_mode": "read",
                "expected_version": "v1",
            }],
        ),
    )
    implementation = kb.claim_task(conn, task_id, claimer="engineering-wren:run")
    assert implementation is not None
    review = request_and_claim_review(conn, task_id, reviewer="grace")
    before = oc.goal_projection(conn, task_id)
    assert before["owner_profile"] == "scarlett"
    assert before["runtime_profile"] == "grace"
    before_contract = before["contract_json"]
    before_budget = before["worker_budget_json"]
    before_gates = before["approval_gates_json"]

    assert kb.request_changes(
        conn,
        task_id,
        reason="keep the seven Grace defects and rework the implementation",
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    ) == (True, "engineering-wren")

    task = kb.get_task(conn, task_id)
    projection = oc.goal_projection(conn, task_id)
    assert task.status == "ready"
    assert task.assignee == "engineering-wren"
    assert task.current_run_id is None
    assert projection["phase"] == "rework"
    assert projection["owner_profile"] == "scarlett"
    assert projection["runtime_profile"] == "engineering-wren"
    assert projection["reviewer_profile"] == "grace"
    assert projection["security_realm"] == "company"
    assert projection["capability_bundle"] == "engineering"
    assert projection["contract_json"] == before_contract
    assert projection["worker_budget_json"] == before_budget
    assert projection["approval_gates_json"] == before_gates


def test_second_reviewer_request_changes_reuses_owner_and_preserves_maker(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn,
        title="bounded retry",
        assignee="engineering-wren",
        owner_cell_contract=contract(
            owner_profile="scarlett",
            runtime_profile="engineering-wren",
            required_reviewer_profile="grace",
        ),
    )
    assert kb.claim_task(conn, task_id, claimer="engineering-wren:first") is not None
    review = request_and_claim_review(conn, task_id, reviewer="grace", ref="artifact-1", digest="digest-1")
    assert kb.request_changes(
        conn,
        task_id,
        reason="first repair",
        expected_run_id=review.current_run_id,
        review_decision_capability=review.review_decision_capability,
    ) == (True, "engineering-wren")
    oc.update_state(conn, task_id, expected_version=1, facts=["maker reworked"])
    assert kb.claim_task(conn, task_id, claimer="engineering-wren:second") is not None
    review_2 = request_and_claim_review(conn, task_id, reviewer="grace", ref="artifact-2", digest="digest-2")

    assert kb.request_changes(
        conn, task_id, reason="second repair with existing accountable owner",
        expected_run_id=review_2.current_run_id,
        review_decision_capability=review_2.review_decision_capability,
    ) == (True, "engineering-wren")
    projection = oc.goal_projection(conn, task_id)
    assert projection["senior_owner_profile"] == "scarlett"
    assert projection["owner_profile"] == "scarlett"
    assert projection["runtime_profile"] == "engineering-wren"
    assert projection["review_loop_count"] == 2
    assert not kb._has_review_decision_capability(conn, task_id, review_2.review_decision_capability)
    assert kb.claim_task(conn, task_id, claimer="engineering-wren:consolidation") is not None
    oc.update_state(conn, task_id, expected_version=2, facts=["second repair complete"])
    final_review = request_and_claim_review(conn, task_id, reviewer="grace", ref="artifact-3", digest="digest-3")
    assert final_review is not None
    assert kb.request_changes(conn, task_id, reason="new artifact still has a missing field",
        expected_run_id=final_review.current_run_id,
        review_decision_capability=final_review.review_decision_capability) == (True, "engineering-wren")
    assert oc.goal_projection(conn, task_id)["review_loop_count"] == 3
    assert kb.claim_task(conn, task_id, claimer="engineering-wren:third") is not None
    oc.update_state(conn, task_id, expected_version=3, facts=["missing field included"])
    final_review = request_and_claim_review(conn, task_id, reviewer="grace", ref="artifact-4", digest="digest-4")
    assert kb.complete_task(conn, task_id, summary="corrected work passed",
        metadata=pass_metadata(ref="artifact-4", digest="digest-4", state_version=4),
        expected_run_id=final_review.current_run_id,
        review_decision_capability=final_review.review_decision_capability)
    assert kb.get_task(conn, task_id).status == "done"


def test_request_changes_on_terminal_task_does_not_mutate_owner_cell(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(
        conn,
        title="terminal",
        assignee="engineering-wren",
        owner_cell_contract=contract(owner_profile="scarlett", runtime_profile="engineering-wren"),
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done', current_run_id=NULL WHERE id=?", (task_id,))
        conn.execute("UPDATE owner_cell_goals SET phase='completed' WHERE task_id=?", (task_id,))
    before = dict(oc.goal_projection(conn, task_id))

    assert kb.request_changes(conn, task_id, reason="late", expected_run_id=None) == (False, "task is not in an active review run")
    assert dict(oc.goal_projection(conn, task_id)) == before

def test_reviewed_completion_without_current_pass_fails_before_side_effect(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(conn, title="no pass", assignee="scarlett", owner_cell_contract=contract())
    with pytest.raises(oc.OwnerCellGuardError, match="review decision capability"):
        kb.complete_task(conn, task_id, summary="must not complete")
    assert kb.get_task(conn, task_id).status == "ready"


def test_reviewed_internal_acceptance_is_not_a_human_input_block(tmp_path, monkeypatch):
    conn = disposable_conn(tmp_path, monkeypatch)
    task_id = kb.create_task(conn, title="internal QA", assignee="scarlett", owner_cell_contract=contract())
    with pytest.raises(oc.OwnerCellGuardError, match="internal QA, not needs_input"):
        kb.block_task(
            conn, task_id, kind="needs_input",
            reason="Dakota owner acceptance must inspect/accept the bundle.",
        )
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb.block_task(
        conn, task_id, kind="needs_input",
        reason="Chris must choose whether private footage may be published.",
    )
    assert kb.get_task(conn, task_id).status == "blocked"


def test_churn_breaker_thresholds_are_deterministic():
    base = {
        "goal_id": "qa-goal", "cards": 1, "cross_owner_handoffs": 1,
        "elapsed_seconds": 0, "non_cache_tokens": 0,
        "acceptance_gaps_closed": 1, "failed_review_loops": 0,
        "budget": {"cards": 2, "handoffs": 2, "elapsed_seconds": 100,
                   "non_cache_tokens": 100, "acceptance_gaps_required": 1},
    }
    assert oc.evaluate_churn({**base, "elapsed_seconds": 50, "acceptance_gaps_closed": 0}).action == "require_progress"
    assert oc.evaluate_churn({**base, "elapsed_seconds": 75}).action == "pause_spawn"
    stop = oc.evaluate_churn({**base, "elapsed_seconds": 100})
    assert stop.action == "stop_spawn" and stop.stop_spawn
    assert stop.notification_key == oc.evaluate_churn({**base, "elapsed_seconds": 100}).notification_key
    assert oc.evaluate_churn({**base, "failed_review_loops": 2}).action == "senior_consolidation"


def test_disposable_db_guard_rejects_canonical_board_path(tmp_path):
    with pytest.raises(oc.OwnerCellGuardError, match="canonical"):
        oc.assert_disposable_db_path(Path.home() / ".hermes" / "kanban.db")
    oc.assert_disposable_db_path(tmp_path / "safe.db")

@pytest.mark.parametrize('mode', ['normal_review', 'warning', 'senior', 'senior_review'])
def test_normal_lifecycle_is_dispatchable(tmp_path, monkeypatch, mode):
    conn = disposable_conn(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'profile_exists', lambda _: True)
    task_id = kb.create_task(conn, title='normal lifecycle', assignee='scarlett',
        owner_cell_contract=contract(senior_owner_profile='claire', churn_budget={
            'cards': 100, 'handoffs': 1, 'elapsed_seconds': 10000,
            'non_cache_tokens': 1000, 'acceptance_gaps_required': 1}))
    for who in ['scarlett','scarlett','grace','scarlett','claire','grace']:
        kb._append_event(conn, task_id, 'assigned', {'assignee':who})
    for kind in ['review_requested','changes_requested'] * 3:
        kb._append_event(conn, task_id, kind, {})
    if mode == 'warning':
        conn.execute('UPDATE owner_cell_goals SET created_at=? WHERE task_id=?', (oc._now()-7600,task_id))
    if mode.startswith('senior'):
        conn.execute('UPDATE owner_cell_goals SET owner_profile=?,review_loop_count=2,phase=? WHERE task_id=?',
            ('claire', 'review' if mode=='senior_review' else 'senior_consolidation',task_id))
    conn.commit()
    decision=oc.dispatch_churn_decision(conn,task_id)
    assert not decision.stop_spawn
    result=kb.dispatch_once(conn,dry_run=True)
    assert any(item[0]==task_id for item in result.spawned), result


def test_senior_recovery_does_not_bypass_spend_limit():
    d=oc.evaluate_churn({'failed_review_loops':2,'senior_recovery_bound':True,
        'non_cache_tokens':100,'budget':{'non_cache_tokens':100}})
    assert d.stop_spawn and d.action=='stop_spawn'

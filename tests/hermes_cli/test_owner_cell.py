"""Synthetic Owner Cell v1 canaries. Enforcement is opt-in for every test."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import owner_cell as oc


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    oc.install_schema(conn)
    return conn


def contract(**overrides):
    value = {
        "goal_id": "goal-a",
        "objective": "synthetic reversible artifact",
        "owner_profile": "scarlett",
        "owner_role_alias": "engineering_systems",
        "runtime_profile": "scarlett",
        "required_reviewer_profile": "grace",
        "security_realm": "company",
        "router_mode": "single",
        "risk_class": "low",
        "capability_bundle": "engineering",
        "worker_budget": {"producers": 1, "reviewers": 0, "extras": 0},
        "targets": [{"target_key": "repo://synthetic/worktree/a", "access_mode": "mutate", "expected_version": "v1"}],
        "acceptance_test": {"expected": "pass"},
        "rollback": {"procedure_ref": "synthetic://rollback"},
        "callback": {"source_surface": "test", "return_surface": "test", "report_policy": "milestones_only"},
        "approval_gates": [],
    }
    value.update(overrides)
    return value


def test_owner_cell_uses_release_pinned_topic_matrix():
    release_root = Path(__file__).resolve().parents[2]
    expected = release_root / "gateway" / "policies" / "margot-departments-topics.json"
    assert oc._DEFAULT_TOPIC_MATRIX == expected
    assert expected.is_file()


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [
        ({"deterministic": True, "read_only": True}, "script"),
        ({"deterministic": False, "review_required": False}, "single"),
        ({"material_code": True}, "reviewed"),
        ({"extra_workers": [{"deliverable_key": "a", "target_keys": ["repo://a"], "rationale": "independent", "merge_contract": "merge"}]}, "fanout"),
    ],
)
def test_shadow_router_selects_all_four_modes_deterministically(input_value, expected):
    first = oc.select_router_mode(input_value)
    second = oc.select_router_mode(dict(reversed(list(input_value.items()))))
    assert first.mode == expected
    assert first == second


def test_same_card_request_changes_preserves_owner_and_pins_reviewer():
    conn = db()
    goal = contract(router_mode="reviewed", risk_class="material", worker_budget={"producers": 1, "reviewers": 1, "extras": 0})
    oc.create_goal(conn, "task-1", goal)
    oc.begin_review(conn, "task-1", reviewer_profile="grace", artifact_ref="a.txt", artifact_digest="digest-a")
    oc.record_verdict(conn, "task-1", reviewer_profile="grace", state_version=1, artifact_ref="a.txt", artifact_digest="digest-a", verdict="REQUEST_CHANGES", defects=["one defect"])
    result = oc.request_changes_same_card(conn, "task-1")
    row = oc.goal_projection(conn, "task-1")
    assert result.phase == "rework"
    assert row["owner_profile"] == "scarlett"
    assert row["runtime_profile"] == "scarlett"
    assert row["reviewer_profile"] == "grace"
    assert row["review_loop_count"] == 1
    assert oc.count_goal_cards(conn, "goal-a") == 1


def test_owner_acceptance_request_changes_returns_same_card_to_original_maker():
    conn = db()
    goal = contract(
        owner_profile="scarlett",
        runtime_profile="engineering-maker",
        required_reviewer_profile="scarlett",
        router_mode="reviewed",
        risk_class="material",
        worker_budget={"producers": 1, "reviewers": 1, "extras": 0},
        owner_acceptance={"mode": "accountable_owner"},
    )
    oc.create_goal(conn, "task-owner-rework", goal)
    oc.begin_review(
        conn,
        "task-owner-rework",
        reviewer_profile="scarlett",
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
    )
    oc.record_verdict(
        conn,
        "task-owner-rework",
        reviewer_profile="scarlett",
        state_version=1,
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
        verdict="REQUEST_CHANGES",
        defects=["repair"],
    )
    result = oc.request_changes_same_card(conn, "task-owner-rework")
    row = oc.goal_projection(conn, "task-owner-rework")
    assert result.phase == "rework"
    assert result.owner_profile == "scarlett"
    assert row["owner_profile"] == "scarlett"
    assert row["runtime_profile"] == "engineering-maker"
    assert row["reviewer_profile"] == "scarlett"
    assert oc.count_goal_cards(conn, "goal-a") == 1




def test_normal_request_changes_returns_same_card_to_original_distinct_maker():
    conn = db()
    goal = contract(
        owner_profile="scarlett",
        runtime_profile="engineering-wren",
        required_reviewer_profile="grace",
        router_mode="reviewed",
        risk_class="material",
        worker_budget={"producers": 1, "reviewers": 1, "extras": 0},
    )
    oc.create_goal(conn, "task-maker-rework", goal)
    original_contract_json = oc.goal_projection(conn, "task-maker-rework")["contract_json"]
    oc.begin_review(
        conn,
        "task-maker-rework",
        reviewer_profile="grace",
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
    )
    oc.record_verdict(
        conn,
        "task-maker-rework",
        reviewer_profile="grace",
        state_version=1,
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
        verdict="REQUEST_CHANGES",
        defects=["repair"],
    )
    result = oc.request_changes_same_card(conn, "task-maker-rework")
    row = oc.goal_projection(conn, "task-maker-rework")
    assert result.phase == "rework"
    assert result.owner_profile == "scarlett"
    assert result.runtime_profile == "engineering-wren"
    assert row["owner_profile"] == "scarlett"
    assert row["runtime_profile"] == "engineering-wren"
    assert row["reviewer_profile"] == "grace"
    assert row["security_realm"] == "company"
    assert row["capability_bundle"] == "engineering"
    assert row["contract_json"] == original_contract_json
    assert row["review_loop_count"] == 1


def test_second_request_changes_escalates_to_senior_without_extra_card():
    conn = db()
    oc.create_goal(
        conn,
        "task-1",
        contract(
            owner_profile="scarlett",
            runtime_profile="engineering-wren",
            required_reviewer_profile="grace",
            router_mode="reviewed",
            risk_class="material",
            worker_budget={"producers": 1, "reviewers": 1, "extras": 0},
            senior_owner_profile="claire",
        ),
    )
    for state_version in (1, 2):
        oc.begin_review(
            conn,
            "task-1",
            reviewer_profile="grace",
            artifact_ref=f"artifact-{state_version}",
            artifact_digest=f"digest-{state_version}",
        )
        oc.record_verdict(
            conn,
            "task-1",
            reviewer_profile="grace",
            state_version=state_version,
            artifact_ref=f"artifact-{state_version}",
            artifact_digest=f"digest-{state_version}",
            verdict="REQUEST_CHANGES",
            defects=["repair"],
        )
        result = oc.request_changes_same_card(conn, "task-1")
        if state_version == 1:
            assert result.runtime_profile == "engineering-wren"
            assert result.owner_profile == "scarlett"
            oc.update_state(conn, "task-1", expected_version=1, facts=["maker repaired"] )
    row = oc.goal_projection(conn, "task-1")
    assert row["owner_profile"] == "claire"
    assert row["runtime_profile"] == "claire"
    assert row["phase"] == "senior_consolidation"
    assert row["review_loop_count"] == 2
    assert oc.count_goal_cards(conn, "goal-a") == 1

def test_two_loop_consolidation_transfers_same_goal_to_senior_owner():
    conn = db()
    oc.create_goal(conn, "task-1", contract(router_mode="reviewed", risk_class="material", worker_budget={"producers": 1, "reviewers": 1, "extras": 0}, senior_owner_profile="claire"))
    for state_version in (1, 2):
        oc.begin_review(conn, "task-1", reviewer_profile="grace", artifact_ref="a.txt", artifact_digest=f"digest-{state_version}")
        oc.record_verdict(conn, "task-1", reviewer_profile="grace", state_version=state_version, artifact_ref="a.txt", artifact_digest=f"digest-{state_version}", verdict="REQUEST_CHANGES", defects=["fix"])
        oc.request_changes_same_card(conn, "task-1")
        if state_version == 1:
            oc.update_state(conn, "task-1", expected_version=1, facts=["reworked"])
    row = oc.goal_projection(conn, "task-1")
    assert row["owner_profile"] == "claire"
    assert row["phase"] == "senior_consolidation"
    with pytest.raises(oc.OwnerCellGuardError, match="repair loop limit"):
        oc.request_changes_same_card(conn, "task-1")


def test_missing_verdict_rejects_reviewed_completion_and_pass_allows_it():
    conn = db()
    oc.create_goal(conn, "task-1", contract(router_mode="reviewed", risk_class="material", worker_budget={"producers": 1, "reviewers": 1, "extras": 0}))
    with pytest.raises(oc.OwnerCellGuardError, match="PASS verdict"):
        oc.assert_completion_allowed(conn, "task-1")
    oc.begin_review(conn, "task-1", reviewer_profile="grace", artifact_ref="a.txt", artifact_digest="digest-a")
    oc.record_verdict(conn, "task-1", reviewer_profile="grace", state_version=1, artifact_ref="a.txt", artifact_digest="digest-a", verdict="PASS", defects=[])
    oc.assert_completion_allowed(conn, "task-1")


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"owner_acceptance": True},
        {"owner_acceptance": {"mode": "accountable_owner"}, "runtime_profile": "scarlett"},
    ],
)
def test_owner_self_acceptance_requires_exact_mode_and_distinct_maker(overrides):
    conn = db()
    fields = {
        "owner_profile": "scarlett",
        "runtime_profile": "engineering-maker",
        "required_reviewer_profile": "scarlett",
        "router_mode": "reviewed",
        "risk_class": "material",
        "worker_budget": {"producers": 1, "reviewers": 1, "extras": 0},
    }
    fields.update(overrides)
    goal = contract(**fields)
    with pytest.raises(oc.OwnerCellGuardError, match="owner|distinct"):
        oc.create_goal(conn, "task-owner-self-acceptance", goal)
    assert conn.execute("SELECT COUNT(*) FROM owner_cell_goals").fetchone()[0] == 0


def test_explicit_owner_acceptance_allows_accountable_owner_to_review_distinct_maker():
    conn = db()
    goal = contract(
        owner_profile="scarlett",
        runtime_profile="engineering-maker",
        required_reviewer_profile="scarlett",
        router_mode="reviewed",
        risk_class="material",
        worker_budget={"producers": 1, "reviewers": 1, "extras": 0},
        owner_acceptance={"mode": "accountable_owner"},
    )
    oc.create_goal(conn, "task-owner-acceptance", goal)
    oc.begin_review(
        conn,
        "task-owner-acceptance",
        reviewer_profile="scarlett",
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
    )
    oc.record_verdict(
        conn,
        "task-owner-acceptance",
        reviewer_profile="scarlett",
        state_version=1,
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
        verdict="PASS",
        defects=[],
    )
    oc.assert_completion_allowed(conn, "task-owner-acceptance")


def test_target_collision_and_stale_state_fail_before_side_effect():
    conn = db()
    oc.create_goal(conn, "task-1", contract(goal_id="goal-a"))
    with pytest.raises(oc.OwnerCellGuardError, match="target already owned"):
        oc.create_goal(conn, "task-2", contract(goal_id="goal-b"))
    lease = oc.acquire_mutator_leases(conn, "task-1", run_id=7, expected_state_version=1)
    assert len(lease) == 1
    oc.update_state(conn, "task-1", expected_version=1, facts=["fresh state"])
    with pytest.raises(oc.OwnerCellGuardError, match="stale state"):
        oc.assert_mutation_fresh(conn, "task-1", lease[0], expected_state_version=1, observed_target_version="v1")


def test_fanout_requires_unique_deliverables_and_disjoint_mutation_targets():
    conn = db()
    allowed = contract(
        router_mode="fanout",
        worker_budget={"producers": 1, "reviewers": 1, "extras": 2},
        targets=[],
        extra_workers=[
            {"deliverable_key": "research", "target_keys": ["repo://a"], "rationale": "independent", "merge_contract": "merge"},
            {"deliverable_key": "qa", "target_keys": ["repo://b"], "rationale": "independent", "merge_contract": "merge"},
        ],
    )
    oc.validate_goal_contract(allowed)
    rejected = dict(allowed)
    rejected["extra_workers"] = [
        {"deliverable_key": "same", "target_keys": ["repo://a"], "rationale": "independent", "merge_contract": "merge"},
        {"deliverable_key": "same", "target_keys": ["repo://a"], "rationale": "independent", "merge_contract": "merge"},
    ]
    with pytest.raises(oc.OwnerCellGuardError, match="independent"):
        oc.validate_goal_contract(rejected)


def test_privacy_and_approval_gates_are_fail_closed_and_callback_survives():
    conn = db()
    private = contract(security_realm="creator:connor", runtime_profile="connor", owner_profile="connor", callback={"source_surface": "telegram:private", "return_surface": "telegram:private", "report_policy": "milestones_only"}, targets=[{"target_key": "app://capcut/creator:connor/project/disposable", "access_mode": "mutate", "expected_version": "v1"}])
    oc.create_goal(conn, "task-1", private)
    with pytest.raises(oc.OwnerCellGuardError, match="privacy realm"):
        oc.assert_realm_access(conn, "task-1", "company", "scarlett")
    assert oc.callback_projection(conn, "task-1")["return_surface"] == "telegram:private"
    assert oc.requires_human_approval(["deploy"]) is True
    assert oc.requires_human_approval([]) is False


def test_frontstage_filter_and_disposable_capcut_handle_replica():
    assert oc.is_frontstage_event("claimed") is False
    assert oc.is_frontstage_event("review_requested") is False
    assert oc.is_frontstage_event("request_changes") is False
    assert oc.is_frontstage_event("goal_completed") is True
    conn = db()
    capcut = contract(goal_id="capcut-handle", router_mode="reviewed", risk_class="material", worker_budget={"producers": 1, "reviewers": 1, "extras": 0}, targets=[{"target_key": "app://capcut/creator:connor/project/disposable-handle", "access_mode": "mutate", "expected_version": "opaque-1"}], security_realm="creator:connor", runtime_profile="connor", owner_profile="connor")
    oc.create_goal(conn, "task-1", capcut)
    first = oc.acquire_mutator_leases(conn, "task-1", run_id=1, expected_state_version=1)
    assert first
    with pytest.raises(oc.OwnerCellGuardError, match="already leased"):
        oc.acquire_mutator_leases(conn, "task-1", run_id=2, expected_state_version=1)
    oc.begin_review(conn, "task-1", reviewer_profile="grace", artifact_ref="handle-recovery.txt", artifact_digest="opaque-digest")
    oc.record_verdict(conn, "task-1", reviewer_profile="grace", state_version=1, artifact_ref="handle-recovery.txt", artifact_digest="opaque-digest", verdict="REQUEST_CHANGES", defects=["retry binding"])
    oc.request_changes_same_card(conn, "task-1")
    assert oc.count_goal_cards(conn, "capcut-handle") == 1


def test_kanban_schema_adds_owner_cell_projections_without_rewriting_legacy_history(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert {"owner_cell_goals", "goal_state_versions", "target_registry", "target_leases", "review_verdicts", "task_deliverables"} <= tables
    assert {"goal_id", "owner_profile", "runtime_profile", "security_realm", "router_mode", "reviewer_profile", "current_state_version"} <= task_columns


def test_create_task_opt_in_projects_owner_cell_without_changing_legacy_assignee(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="owner-cell canary", assignee="scarlett", owner_cell_contract=contract(goal_id="goal-from-task"))
        task = kb.get_task(conn, tid)
        projection = oc.goal_projection(conn, tid)
        task_projection = conn.execute(
            "SELECT goal_id, owner_profile, runtime_profile, security_realm, "
            "router_mode, current_state_version FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert task.assignee == "scarlett"
    assert projection["owner_profile"] == "scarlett"
    assert projection["runtime_profile"] == "scarlett"
    assert dict(task_projection) == {
        "goal_id": "goal-from-task",
        "owner_profile": "scarlett",
        "runtime_profile": "scarlett",
        "security_realm": "company",
        "router_mode": "single",
        "current_state_version": 1,
    }



def command_routing_body(**overrides):
    import json
    metadata = {
        "department_id": "engineering_systems",
        "executable_assignee": "scarlett",
        "source_surface": "telegram:-1004479003480:11",
        "return_surface": "telegram:-1004479003480:11",
        "privacy_tier": "team",
        "reviewer_profiles": ["grace"],
        "callback_payload": {"report_back_policy": {"mode": "milestone_only"}},
    }
    metadata.update(overrides)
    return "work\n\n## Routing metadata\n\n```json\n" + json.dumps(metadata) + "\n```\n"



def _canonical_safe_delegate_module():
    candidates = [
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "margot_parity"
        / "safe_delegate_card.py",
        Path.home() / ".openclaw/workspace/Agent-Shared/tools/kanban/safe_delegate_card.py",
    ]
    script = next((path for path in candidates if path.is_file()), candidates[0])
    spec = importlib.util.spec_from_file_location("safe_delegate_card_owner_cell_e2e", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _producer_owner_acceptance_body():
    producer = _canonical_safe_delegate_module()
    argv = [
        "--board", "margot-chat-lanes",
        "--tenant", "telegram:1963997029",
        "--department", "engineering",
        "--assignee", "engineering-wren",
        "--title", "Build bounded artifact on one root",
        "--body", "Implement and verify the requested artifact.",
        "--package-class", "deliverable",
        "--routing-contract", "sol_owner_terra_maker_single_root",
        "--accountable-owner-profile", "scarlett",
    ]
    with patch.object(
        producer,
        "spawnable_profiles",
        return_value={"scarlett", "engineering-wren"},
    ), patch.object(
        producer,
        "known_boards",
        return_value={"margot-chat-lanes"},
    ), patch.object(
        producer,
        "load_registry",
        return_value={
            "roles": [{
                "id": "engineering_systems",
                "name": "Engineering Systems",
                "default_runtimes": [{"agent": "engineering-wren"}],
            }],
            "review_policy": {"matrix": {}},
        },
    ):
        args = producer.parse_args(argv)
        route = producer.resolve_route(args)
        package = producer.build_delegation_package(args, route)
        return producer.body_with_metadata(args.body, route, package)


def test_canonical_producer_metadata_enforces_owner_acceptance_end_to_end(monkeypatch):
    from hermes_cli import profiles

    body = _producer_owner_acceptance_body()
    metadata = oc._routing_metadata(body)
    routing = metadata["routing_contract"]
    assert metadata["owner_acceptance"] == {"mode": "accountable_owner"}
    assert metadata["accountable_owner_profile"] == "scarlett"
    assert metadata["maker_profile"] == "engineering-wren"
    assert metadata["runtime_profile"] == "engineering-wren"
    assert metadata["required_acceptor_profile"] == "scarlett"
    assert metadata["required_reviewer_profile"] == "scarlett"
    assert metadata["reviewer_profiles"] == ["scarlett"]
    assert metadata["department_id"] == routing["department_id"] == "engineering_systems"
    assert metadata["topology"] == routing["topology"] == "single_root"
    assert metadata["max_child_depth"] == routing["max_child_depth"] == 0
    assert metadata["subscription_scope"] == routing["subscription_scope"] == "root_only"
    assert metadata["report_back_scope"] == routing["report_back_scope"] == "root_only"

    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: name in {"scarlett", "engineering-wren", "claire"},
    )
    derived = oc.command_board_contract(
        board="chris-home",
        title="Producer to Owner Cell",
        body=body,
        assignee="engineering-wren",
        parents=(),
        goal_id="goal-producer-owner-cell",
    )
    assert derived["router_mode"] == "reviewed"
    assert derived["owner_profile"] == "scarlett"
    assert derived["runtime_profile"] == "engineering-wren"
    assert derived["required_reviewer_profile"] == "scarlett"
    assert derived["owner_acceptance"] == {"mode": "accountable_owner"}

    conn = db()
    oc.create_goal(conn, "task-producer-owner-cell", derived)
    with pytest.raises(oc.OwnerCellGuardError, match="PASS verdict"):
        oc.assert_completion_allowed(conn, "task-producer-owner-cell")
    with pytest.raises(oc.OwnerCellGuardError, match="required reviewer"):
        oc.begin_review(
            conn,
            "task-producer-owner-cell",
            reviewer_profile="engineering-wren",
            artifact_ref="artifact-a",
            artifact_digest="digest-a",
        )
    oc.begin_review(
        conn,
        "task-producer-owner-cell",
        reviewer_profile="scarlett",
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
    )
    oc.record_verdict(
        conn,
        "task-producer-owner-cell",
        reviewer_profile="scarlett",
        state_version=1,
        artifact_ref="artifact-a",
        artifact_digest="digest-a",
        verdict="PASS",
        defects=[],
    )
    oc.assert_completion_allowed(conn, "task-producer-owner-cell")

    for field, wrong_owner in (
        ("accountable_owner_profile", "claire"),
        ("required_reviewer_profile", "claire"),
    ):
        tampered = dict(metadata)
        tampered[field] = wrong_owner
        tampered_body = "work\n\n## Routing metadata\n\n```json\n" + json.dumps(tampered) + "\n```\n"
        with pytest.raises(oc.OwnerCellGuardError, match="owner-acceptance metadata"):
            oc.command_board_contract(
                board="chris-home",
                title="Tampered owner",
                body=tampered_body,
                assignee="engineering-wren",
                parents=(),
                goal_id=f"goal-tampered-{field}",
            )

    tampered_routing = dict(metadata)
    tampered_routing["routing_contract"] = {
        **routing,
        "accountable_owner_profile": "claire",
    }
    tampered_routing_body = "work\n\n## Routing metadata\n\n```json\n" + json.dumps(tampered_routing) + "\n```\n"
    with pytest.raises(oc.OwnerCellGuardError, match="owner-acceptance metadata"):
        oc.command_board_contract(
            board="chris-home",
            title="Tampered nested owner",
            body=tampered_routing_body,
            assignee="engineering-wren",
            parents=(),
            goal_id="goal-tampered-routing-owner",
        )

    self_accepting = dict(metadata)
    self_accepting.update({
        "accountable_owner_profile": "engineering-wren",
        "required_acceptor_profile": "engineering-wren",
        "required_reviewer_profile": "engineering-wren",
        "reviewer_profiles": ["engineering-wren"],
    })
    self_accepting["routing_contract"] = {
        **routing,
        "accountable_owner_profile": "engineering-wren",
        "required_acceptor_profile": "engineering-wren",
    }
    self_accepting_body = "work\n\n## Routing metadata\n\n```json\n" + json.dumps(self_accepting) + "\n```\n"
    with pytest.raises(oc.OwnerCellGuardError, match="distinct from maker"):
        oc.command_board_contract(
            board="chris-home",
            title="Maker self acceptance",
            body=self_accepting_body,
            assignee="engineering-wren",
            parents=(),
            goal_id="goal-maker-self-acceptance",
        )

def test_command_board_root_derives_durable_accountable_owner_contract(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace"})
    derived = oc.command_board_contract(
        board="chris-home",
        title="Fix the system",
        body=command_routing_body(),
        assignee="engineering_systems",
        parents=(),
        goal_id="goal-command",
    )
    assert derived["owner_profile"] == "scarlett"
    assert derived["runtime_profile"] == "scarlett"
    assert derived["owner_role_alias"] == "engineering_systems"
    assert derived["security_realm"] == "team"
    assert derived["callback"] == {
        "source_surface": "telegram:-1004479003480:11",
        "return_surface": "telegram:-1004479003480:11",
        "report_policy": "milestone_only",
    }
    assert derived["worker_budget"] == {"producers": 1, "reviewers": 1, "extras": 0, "max_depth": 1}


def test_command_board_explicit_owner_acceptance_routes_owner_over_distinct_maker(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: name in {"scarlett", "engineering-wren"},
    )
    derived = oc.command_board_contract(
        board="chris-home",
        title="Owner accepted implementation",
        body=_producer_owner_acceptance_body(),
        assignee="engineering-wren",
        parents=(),
        goal_id="goal-owner-acceptance",
    )
    assert derived["router_mode"] == "reviewed"
    assert derived["runtime_profile"] == "engineering-wren"
    assert derived["required_reviewer_profile"] == "scarlett"
    assert derived["owner_acceptance"] == {"mode": "accountable_owner"}
    assert derived["worker_budget"]["producers"] == 1
    assert derived["worker_budget"]["reviewers"] == 1


def test_command_board_create_assignment_and_review_preserve_accountable_owner(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace", "claire"})
    with kb.connect(board="chris-home") as conn:
        task_id = kb.create_task(
            conn, title="owned root", body=command_routing_body(),
            assignee="engineering_systems", board="chris-home",
        )
        row = conn.execute(
            "SELECT owner_profile, runtime_profile, source_surface, return_surface, security_realm FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(row) == {
            "owner_profile": "scarlett", "runtime_profile": "scarlett",
            "source_surface": "telegram:-1004479003480:11",
            "return_surface": "telegram:-1004479003480:11", "security_realm": "team",
        }
        assert kb.assign_task(conn, task_id, "engineering_systems")
        assert kb.request_review(
            conn, task_id, summary="review", reviewer="Grace",
            metadata={"owner_cell_artifact": {"ref": "artifact", "digest": "digest"}},
        )
        projected = conn.execute(
            "SELECT owner_profile, runtime_profile, reviewer_profile FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(projected) == {
            "owner_profile": "scarlett", "runtime_profile": "grace", "reviewer_profile": "grace",
        }
        assert oc.goal_projection(conn, task_id)["owner_profile"] == "scarlett"
        with pytest.raises(ValueError, match="installed runtime profile"):
            kb.assign_task(conn, task_id, "imaginary-reviewer")
        with pytest.raises(oc.OwnerCellGuardError, match="owner-cell contract"):
            kb.assign_task(conn, task_id, "claire")


def test_command_board_budget_allows_one_maker_one_reviewer_and_no_grandchildren(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace"})
    with kb.connect(board="chris-home") as conn:
        root = kb.create_task(conn, title="root", body=command_routing_body(), assignee="scarlett", board="chris-home")
        maker = kb.create_task(conn, title="Maker: implement", assignee="scarlett", parents=[root], board="chris-home", parent_edge_role="delegation_maker")
        kb.create_task(conn, title="Review: verify", assignee="grace", parents=[root], board="chris-home", parent_edge_role="delegation_reviewer")
        with pytest.raises(oc.OwnerCellGuardError, match="one maker"):
            kb.create_task(conn, title="Maker: duplicate", assignee="scarlett", parents=[root], board="chris-home", parent_edge_role="delegation_maker")
        with pytest.raises(oc.OwnerCellGuardError, match="grandchildren"):
            kb.create_task(conn, title="Review: nested", assignee="grace", parents=[maker], board="chris-home", parent_edge_role="delegation_reviewer")


def test_configured_command_board_root_without_routing_metadata_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import config as hermes_config
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"command_boards": ["chris-home", "josh-cockpit-lanes"]}},
    )
    with kb.connect(board="josh-cockpit-lanes") as conn:
        with pytest.raises(oc.OwnerCellGuardError, match="routing metadata"):
            kb.create_task(
                conn,
                title="unowned root",
                body="no trusted route",
                assignee="scarlett",
                board="josh-cockpit-lanes",
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_link_tasks_cannot_bypass_command_board_worker_or_depth_budget(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace"})
    with kb.connect(board="chris-home") as conn:
        root = kb.create_task(conn, title="root", body=command_routing_body(), assignee="scarlett", board="chris-home")
        maker = kb.create_task(conn, title="Maker: first", assignee="scarlett", board="legacy-unscoped")
        duplicate = kb.create_task(conn, title="Maker: duplicate", assignee="scarlett", board="legacy-unscoped")
        grandchild = kb.create_task(conn, title="Review: nested", assignee="grace", board="legacy-unscoped")
        kb.link_tasks(conn, root, maker, edge_role="delegation_maker")
        with pytest.raises(oc.OwnerCellGuardError, match="one maker"):
            kb.link_tasks(conn, root, duplicate, edge_role="delegation_maker")
        with pytest.raises(oc.OwnerCellGuardError, match="grandchildren"):
            kb.link_tasks(conn, maker, grandchild, edge_role="delegation_reviewer")


def test_reviewed_goal_persists_required_reviewer_and_rejects_owner_or_maker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace"})
    body = command_routing_body(reviewer_profiles=["scarlett", "grace"])
    with kb.connect(board="chris-home") as conn:
        task_id = kb.create_task(conn, title="reviewed root", body=body, assignee="scarlett", board="chris-home")
        goal = oc.goal_projection(conn, task_id)
        assert goal["required_reviewer_profile"] == "grace"
        artifact = {"owner_cell_artifact": {"ref": "artifact", "digest": "digest"}}
        with pytest.raises(oc.OwnerCellGuardError, match="required reviewer"):
            kb.request_review(
                conn, task_id, summary="self review", reviewer="scarlett", metadata=artifact
            )
        assert kb.request_review(
            conn, task_id, summary="independent review", reviewer="grace", metadata=artifact
        )


def test_josh_command_board_root_derives_owner_contract_and_enforces_budget(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    from hermes_cli import config as hermes_config, profiles
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"command_boards": ["chris-home", "josh-cockpit-lanes"]}},
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"scarlett", "grace"})
    body = command_routing_body(accountable_owner_profile="scarlett")
    with kb.connect(board="josh-cockpit-lanes") as conn:
        root = kb.create_task(
            conn, title="Josh owned root", body=body, assignee="scarlett", board="josh-cockpit-lanes"
        )
        goal = oc.goal_projection(conn, root)
        assert goal["owner_profile"] == "scarlett"
        assert goal["required_reviewer_profile"] == "grace"
        kb.create_task(
            conn, title="Maker: one", assignee="scarlett", parents=[root], board="josh-cockpit-lanes", parent_edge_role="delegation_maker"
        )
        kb.create_task(
            conn, title="Review: one", assignee="grace", parents=[root], board="josh-cockpit-lanes", parent_edge_role="delegation_reviewer"
        )
        with pytest.raises(oc.OwnerCellGuardError, match="one reviewer"):
            kb.create_task(
                conn, title="Review: extra", assignee="grace", parents=[root], board="josh-cockpit-lanes", parent_edge_role="delegation_reviewer"
            )


def test_rejected_bytes_do_not_get_another_maker_run():
    conn=db()
    oc.create_goal(conn,"task-1",contract(router_mode="reviewed",risk_class="material",worker_budget={"producers":1,"reviewers":1,"extras":0},senior_owner_profile="claire"))
    for n in (1,2):
        oc.begin_review(conn,"task-1",reviewer_profile="grace",artifact_ref="a",artifact_digest="same-bytes")
        oc.record_verdict(conn,"task-1",reviewer_profile="grace",state_version=1,artifact_ref="a",artifact_digest="same-bytes",verdict="REQUEST_CHANGES",defects=[f"different prose {n}"])
        if n==1:
            oc.request_changes_same_card(conn,"task-1")
        else:
            with pytest.raises(oc.OwnerCellGuardError,match="unchanged rejected artifact"):
                oc.request_changes_same_card(conn,"task-1")
    assert oc.goal_projection(conn,"task-1")["review_loop_count"]==1


def test_correction_budget_is_still_finite():
    conn=db()
    oc.create_goal(conn,"task-1",contract(router_mode="reviewed",risk_class="material",worker_budget={"producers":1,"reviewers":1,"extras":0},senior_owner_profile="claire"))
    conn.execute("UPDATE owner_cell_goals SET review_loop_count=6 WHERE task_id='task-1'")
    with pytest.raises(oc.OwnerCellGuardError,match="repair budget exhausted"):
        oc.request_changes_same_card(conn,"task-1")

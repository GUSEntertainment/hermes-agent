"""Regression contract for Margot OS inbound department-topic dispatch.

These tests use only synthetic request text and prove the policy boundary: a
specialist topic yields a canonical, idempotent handoff envelope rather than
falling through to the default Margot agent.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

from gateway.department_topic_router import (
    CANONICAL_MARGOT_OS_CHAT_ID,
    DEFAULT_MATRIX_PATH,
    _DEFAULT_ROLE_REGISTRY_PATH,
    _SAFE_DELEGATE_HELPER,
    _PrivateIntakeBroker,
    _reserve_private_intake_for_task_body,
    build_safe_delegate_command,
    dispatch_department_topic,
    load_topic_matrix,
    plan_department_workflow,
    resolve_department_topic,
)


MATRIX_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "margot_parity"
    / "margot-departments-topics.json"
)


EXPECTED_FRONT_DOORS = {
    1: "default",
    7: "default",
    9: "riley",
    11: "scarlett",
    13: "geneva",
    15: "dakota",
    17: "lauren",
    19: "harper",
    21: "maya",
    23: "steven",
    25: "riley",
    27: "claire",
    29: "scarlett",
    31: "default",
    33: "default",
    49: "ingrid",
    50: "julia",
    51: "claude-review",
}


@pytest.fixture
def matrix():
    return load_topic_matrix(MATRIX_PATH)


def test_runtime_matrix_digest_rejects_same_path_content_drift(monkeypatch, tmp_path):
    import gateway.department_topic_router as topic_router

    drifted = tmp_path / "margot-departments-topics.json"
    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    raw["routing_version"] = "unreviewed-drift"
    drifted.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(topic_router, "DEFAULT_MATRIX_PATH", drifted)

    with pytest.raises(ValueError, match="digest"):
        topic_router.load_topic_matrix()


def test_runtime_helper_and_registry_digests_are_checked_before_execution(monkeypatch, tmp_path):
    import gateway.department_topic_router as topic_router

    helper = tmp_path / "safe_delegate_card.py"
    helper.write_text("print('unreviewed')\n", encoding="utf-8")
    registry = tmp_path / "role-registry.yaml"
    registry.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(topic_router, "_SAFE_DELEGATE_HELPER", helper)
    monkeypatch.setattr(topic_router, "_DEFAULT_ROLE_REGISTRY_PATH", registry)
    monkeypatch.setattr(
        topic_router.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unreviewed helper must not execute")
        ),
    )

    with pytest.raises(ValueError, match="digest"):
        topic_router.dispatch_to_safe_delegate({}, board="chris-home")


def test_runtime_routing_authority_is_shipped_inside_release():
    assert DEFAULT_MATRIX_PATH.is_file()
    assert _SAFE_DELEGATE_HELPER.is_file()
    assert _DEFAULT_ROLE_REGISTRY_PATH.is_file()
    assert DEFAULT_MATRIX_PATH.is_relative_to(Path(__file__).resolve().parents[2])
    assert _SAFE_DELEGATE_HELPER.is_relative_to(Path(__file__).resolve().parents[2])
    assert _DEFAULT_ROLE_REGISTRY_PATH.is_relative_to(Path(__file__).resolve().parents[2])


def test_canonical_matrix_resolves_every_topic_to_a_spawnable_owner(matrix):
    assert matrix.chat_id == CANONICAL_MARGOT_OS_CHAT_ID
    assert set(matrix.topic_ids) == set(EXPECTED_FRONT_DOORS)

    for thread_id, expected_owner in EXPECTED_FRONT_DOORS.items():
        decision = resolve_department_topic(
            chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
            thread_id=thread_id,
            matrix=matrix,
        )
        expected_status = (
            "command"
            if thread_id in {1, 7, 31, 33}
            else "unverified_topic"
            if thread_id in {49, 50, 51}
            else "specialist"
        )
        assert decision.status == expected_status
        assert decision.route.front_door_profile == expected_owner
        assert decision.envelope.return_surface == f"telegram:{CANONICAL_MARGOT_OS_CHAT_ID}:{thread_id}"
        assert decision.route.front_door_profile not in {"signer", "monitor", "qa", "sydney"}


def test_specialist_dispatch_preserves_topic_and_never_default_executes(matrix):
    created = []

    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=11,
        message_id=712,
        message_text="Synthetic engineering canary: inspect only.",
        matrix=matrix,
        create_task=created.append,
    )

    assert outcome.status == "delegated"
    assert outcome.acknowledgement_only is True
    assert len(created) == 1
    envelope = created[0]
    assert envelope["runtime_assignee"] == "scarlett"
    assert envelope["tenant"] == "chris-home"
    assert envelope["source_thread_id"] == "11"
    assert envelope["return_surface"] == f"telegram:{CANONICAL_MARGOT_OS_CHAT_ID}:11"
    assert envelope["idempotency_key"] == outcome.idempotency_key
    assert envelope["default_execution_forbidden"] is True
    assert envelope["intake"]["retention"] == "task_body"
    assert envelope["intake"]["request_text"] == "Synthetic engineering canary: inspect only."


def test_replay_is_idempotent_and_unknown_topic_fails_closed(matrix):
    created = []
    kwargs = dict(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=9,
        message_id=713,
        message_text="Synthetic research canary.",
        matrix=matrix,
        create_task=created.append,
    )
    first = dispatch_department_topic(**kwargs)
    second = dispatch_department_topic(**kwargs)
    unknown = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=9999,
        message_id=714,
        message_text="Synthetic unknown topic canary.",
        matrix=matrix,
        create_task=created.append,
    )

    assert first.status == "delegated"
    assert second.status == "duplicate"
    assert len(created) == 1
    assert unknown.status == "unknown_topic"
    assert unknown.default_execution_forbidden is True
    assert unknown.acknowledgement_only is True


def test_concurrent_replay_reserves_idempotency_before_task_creation(matrix):
    callback_entered = threading.Event()
    release_callback = threading.Event()
    created = []
    first_result = []

    def blocking_create(payload):
        created.append(payload)
        callback_entered.set()
        assert release_callback.wait(timeout=5)

    kwargs = dict(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=11,
        message_id=7713,
        message_text="Synthetic concurrent engineering canary.",
        matrix=matrix,
        create_task=blocking_create,
    )
    thread = threading.Thread(
        target=lambda: first_result.append(dispatch_department_topic(**kwargs))
    )
    thread.start()
    assert callback_entered.wait(timeout=5)

    duplicate = dispatch_department_topic(**kwargs)
    release_callback.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_result[0].status == "delegated"
    assert duplicate.status == "duplicate"
    assert len(created) == 1


def test_failed_sensitive_task_creation_discards_intake_and_allows_retry(matrix):
    intake_store = _PrivateIntakeBroker()
    failed_payloads = []
    request_text = "Synthetic legal packet for failed-create cleanup."
    kwargs = dict(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=23,
        message_id=7715,
        message_text=request_text,
        matrix=matrix,
        intake_store=intake_store,
    )

    def fail_create(payload):
        failed_payloads.append(payload)
        raise RuntimeError("task insert failed")

    with pytest.raises(RuntimeError, match="task insert failed"):
        dispatch_department_topic(**kwargs, create_task=fail_create)

    failed_handle = failed_payloads[0]["intake"]["handle"]
    with pytest.raises(KeyError, match="unavailable"):
        intake_store._take_for_gateway_dispatch(
            failed_handle, claimed_assignee="steven", claim_lock="x"
        )

    created = []
    retry = dispatch_department_topic(**kwargs, create_task=created.append)
    assert retry.status == "delegated"
    retry_handle = created[0]["intake"]["handle"]
    assert intake_store._take_for_gateway_dispatch(
        retry_handle, claimed_assignee="steven", claim_lock="x"
    ) == request_text


def test_legal_dispatch_encodes_senior_to_junior_to_senior_chain(matrix):
    intake_store = _PrivateIntakeBroker()
    created = []
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=23,
        message_id=715,
        message_text="Synthetic NDA draft request; no signature or send.",
        matrix=matrix,
        create_task=created.append,
        intake_store=intake_store,
    )

    assert outcome.status == "delegated"
    envelope = created[0]
    assert envelope["runtime_assignee"] == "steven"
    assert envelope["workflow"]["kind"] == "legal_senior_junior_senior"
    assert envelope["workflow"]["steps"] == [
        {"profile": "steven", "role": "package_author"},
        {"profile": "legal-drafter", "role": "draft_maker", "display_identity": "Avery"},
        {"profile": "steven", "role": "exact_artifact_reviewer"},
    ]
    assert envelope["privacy_tier"] == "legal"
    assert envelope["approval_gates"] == [
        "legal_commitment",
        "signature",
        "counsel_send",
        "external_send",
    ]
    assert envelope["workflow"]["optional_fable"] == "explicit_manual_redacted_only"
    assert "Synthetic NDA draft request" not in repr(envelope)
    handle = envelope["intake"]["handle"]
    assert envelope["intake"]["retention"] == "process_local_ephemeral"
    with pytest.raises(PermissionError):
        intake_store._take_for_gateway_dispatch(handle, claimed_assignee="geneva", claim_lock="x")
    assert intake_store._take_for_gateway_dispatch(
        handle, claimed_assignee="steven", claim_lock="x"
    ) == "Synthetic NDA draft request; no signature or send."


@pytest.mark.parametrize(
    ("thread_id", "expected_profile", "expected_reviewer"),
    [
        (9, "riley", "grace"),
        (11, "scarlett", "grace"),
        (13, "geneva", "grace"),
        (27, "claire", "grace"),
    ],
)
def test_representative_canaries_keep_canonical_owner_and_reviewer(matrix, thread_id, expected_profile, expected_reviewer):
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=thread_id,
        message_id=8000 + thread_id,
        message_text="Synthetic dry-run canary only.",
        matrix=matrix,
        create_task=lambda _: None,
    )

    assert outcome.status == "delegated"
    assert outcome.envelope["runtime_assignee"] == expected_profile
    assert expected_reviewer in outcome.envelope["required_reviewers"]
    assert outcome.envelope["fallback_degraded_critical_approval"] is False


def test_live_handoff_command_uses_private_handle_for_sensitive_intake_and_selects_legal_chain(matrix):
    intake_store = _PrivateIntakeBroker()
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=23,
        message_id=716,
        message_text="Synthetic legal text must not enter task metadata.",
        matrix=matrix,
        create_task=lambda _: None,
        intake_store=intake_store,
    )

    command = build_safe_delegate_command(outcome.envelope, board="default")

    assert "--canonical-topic-intake" in command
    assert "--legal-inbound-chain" in command
    assert command[command.index("--assignee") + 1] == "steven"
    assert command[command.index("--package-author") + 1] == "steven"
    assert command[command.index("--package-author-tier") + 1] == "sol"
    assert command[command.index("--complexity") + 1] == "complex"
    assert command[command.index("--tenant") + 1] == "chris-home"
    assert command[command.index("--source-surface") + 1] == "telegram:-1004479003480:23"
    assert "Synthetic legal text must not enter task metadata." not in command
    assert "--source-ref" in command
    assert command[command.index("--source-ref") + 1].startswith("margot-intake://")
    assert command[command.index("--approval-gate") + 1] == "legal_commitment"
    command_gates = [command[index + 1] for index, value in enumerate(command) if value == "--gate"]
    assert command_gates == ["legal_commitment", "signature", "counsel_send", "external_send"]
    command_forbidden = [
        command[index + 1] for index, value in enumerate(command) if value == "--forbidden-action"
    ]
    assert {"legal_advice", "logged_in_account_action", "broad_retention", "raw_privileged_disclosure"}.issubset(
        command_forbidden
    )


@pytest.mark.parametrize(
    ("thread_id", "request_text", "expected_assignee", "expected_complexity", "expected_review"),
    [
        (17, "Prepare a local reminder draft.", "lauren", "simple", False),
        (17, "Coordinate a cross-team architecture and dependency plan.", "claire", "complex", True),
        (25, "Analyze campaign content analytics.", "geneva", "standard", True),
        (25, "Build an analytics data pipeline integration.", "scarlett", "standard", True),
        (27, "Create a local engineering automation task.", "scarlett", "simple", True),
        (27, "Plan a cross-team durable operations migration.", "lauren", "complex", False),
    ],
)
def test_workflow_plan_selects_the_explicit_conditional_owner(
    matrix, thread_id, request_text, expected_assignee, expected_complexity, expected_review
):
    route = resolve_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=thread_id,
        matrix=matrix,
    ).route
    assert route is not None
    plan = plan_department_workflow(route, request_text)
    assert plan.assignee == expected_assignee
    assert plan.complexity == expected_complexity
    assert plan.with_review is expected_review
    if thread_id == 27:
        assert plan.package_author == "claire"


def test_mission_control_command_requires_claire_package_chain(matrix):
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=27,
        message_id=9027,
        message_text="Synthetic mission-control engineering automation task; no live action.",
        matrix=matrix,
        create_task=lambda _: None,
    )
    command = build_safe_delegate_command(outcome.envelope, board="chris-home")

    assert command[command.index("--assignee") + 1] == "scarlett"
    assert command[command.index("--package-author") + 1] == "claire"
    assert command[command.index("--package-author-tier") + 1] == "sol"
    assert "--coordination-package-chain" in command


def test_candidate_topic_never_delegates_before_inbound_verification(matrix):
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=49,
        message_id=8049,
        message_text="Synthetic unverified watchdog canary.",
        matrix=matrix,
        create_task=lambda _: pytest.fail("candidate topic must not create a specialist task"),
    )
    assert outcome.status == "unverified_topic"
    assert outcome.default_execution_forbidden is True
    assert outcome.acknowledgement_only is True


def test_verified_restricted_review_intake_never_copies_raw_text_to_the_task_payload(matrix):
    """A future verified Claude Review topic remains private/redacted by default."""
    candidate = resolve_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=51,
        matrix=matrix,
    ).route
    assert candidate is not None
    verified_routes = dict(matrix.routes)
    verified_routes[51] = replace(candidate, discovery_confidence="verified")
    verified_matrix = replace(matrix, routes=verified_routes)
    intake_store = _PrivateIntakeBroker()
    raw_packet = "Synthetic restricted packet: never mirror this text into a team task."

    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=51,
        message_id=8051,
        message_text=raw_packet,
        matrix=verified_matrix,
        create_task=lambda _: None,
        intake_store=intake_store,
    )

    assert outcome.status == "delegated"
    assert raw_packet not in repr(outcome.envelope)
    assert outcome.envelope["intake"]["retention"] == "process_local_ephemeral"
    assert intake_store._take_for_gateway_dispatch(
        outcome.envelope["intake"]["handle"], claimed_assignee="claude-review",
        claim_lock="x",
    ) == raw_packet



def test_private_intake_identity_comes_from_claimed_dispatch_not_caller_environment(matrix):
    intake_store = _PrivateIntakeBroker()
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=23,
        message_id=88051,
        message_text="Synthetic private legal packet.",
        matrix=matrix,
        create_task=lambda _: None,
        intake_store=intake_store,
    )
    handle = outcome.envelope["intake"]["handle"]

    with pytest.raises(PermissionError, match="not authorized"):
        intake_store._take_for_gateway_dispatch(
            handle, claimed_assignee="default", claim_lock="x"
        )
    assert intake_store._take_for_gateway_dispatch(
        handle, claimed_assignee="steven", claim_lock="x"
    ) == "Synthetic private legal packet."


def test_private_intake_handle_is_one_shot_per_authorized_profile():
    intake_store = _PrivateIntakeBroker()
    handle = intake_store.put(
        request_text="Synthetic shared legal packet.",
        allowed_profiles=("steven", "legal-drafter"),
        idempotency_key="legal-chain",
    )

    steven = intake_store._reserve_for_gateway_dispatch(
        handle, claimed_assignee="steven", claim_lock="parent-lock"
    )
    intake_store._commit_gateway_dispatch(steven)
    drafter = intake_store._reserve_for_gateway_dispatch(
        handle, claimed_assignee="legal-drafter", claim_lock="draft-lock"
    )
    intake_store._commit_gateway_dispatch(drafter)

    assert steven.request_text == drafter.request_text == "Synthetic shared legal packet."
    with pytest.raises(KeyError, match="unavailable"):
        intake_store._reserve_for_gateway_dispatch(
            handle, claimed_assignee="steven", claim_lock="replay-lock"
        )


def test_private_intake_task_body_reserves_exact_handle_for_claimed_assignee():
    intake_store = _PrivateIntakeBroker()
    handle = intake_store.put(
        request_text="Synthetic reserved packet.",
        allowed_profiles=("steven",),
        idempotency_key="reserve-body",
    )
    body = (
        "Canonical Margot OS private intake is held by the gateway until the "
        f"authorized assignee is actually claimed. Opaque handle: {handle}"
    )

    reservation = _reserve_private_intake_for_task_body(
        body,
        claimed_assignee="steven",
        claim_lock="claim-lock",
        intake_store=intake_store,
    )
    assert reservation is not None
    assert reservation.request_text == "Synthetic reserved packet."
    intake_store._release_gateway_dispatch(reservation)

    assert _reserve_private_intake_for_task_body(
        "ordinary task body",
        claimed_assignee="steven",
        claim_lock="claim-lock",
        intake_store=intake_store,
    ) is None
    with pytest.raises(ValueError, match="malformed private intake handle"):
        _reserve_private_intake_for_task_body(
            "Opaque handle: margot-intake://not-a-valid-handle",
            claimed_assignee="steven",
            claim_lock="claim-lock",
            intake_store=intake_store,
        )


def test_matrix_environment_redirect_is_ignored_and_missing_policy_is_an_error(monkeypatch, tmp_path):
    import gateway.department_topic_router as topic_router

    redirected = tmp_path / "untrusted.json"
    redirected.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MARGOT_DEPARTMENT_TOPICS_PATH", str(redirected))
    monkeypatch.setattr(topic_router, "DEFAULT_MATRIX_PATH", MATRIX_PATH)
    monkeypatch.setattr(topic_router, "_REVIEWED_MATRIX_SHA256", hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest())
    assert load_topic_matrix().chat_id == CANONICAL_MARGOT_OS_CHAT_ID
    with pytest.raises(FileNotFoundError):
        load_topic_matrix(tmp_path / "missing.json")


def test_legal_matrix_rejects_implicit_fable_and_noncanonical_drafting_makers(tmp_path):
    canonical = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    legal = next(topic for topic in canonical["topics"] if topic["topic_id"] == 23)

    legal["required_reviewers"].insert(0, "fable-review")
    implicit_fable = tmp_path / "implicit-fable.json"
    implicit_fable.write_text(json.dumps(canonical), encoding="utf-8")
    with pytest.raises(ValueError, match="return the exact artifact to Steven first|Fable"):
        load_topic_matrix(implicit_fable)

    canonical = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    legal = next(topic for topic in canonical["topics"] if topic["topic_id"] == 23)
    legal["maker_profiles"] = ["legal-drafter", "default"]
    extra_maker = tmp_path / "extra-maker.json"
    extra_maker.write_text(json.dumps(canonical), encoding="utf-8")
    with pytest.raises(ValueError, match="only to legal-drafter/Avery"):
        load_topic_matrix(extra_maker)


def test_legal_safe_delegate_command_rejects_junior_bypass_and_raw_task_body(matrix):
    intake_store = _PrivateIntakeBroker()
    outcome = dispatch_department_topic(
        chat_id=CANONICAL_MARGOT_OS_CHAT_ID,
        thread_id=23,
        message_id=99123,
        message_text="Synthetic legal routing guard canary.",
        matrix=matrix,
        create_task=lambda _: None,
        intake_store=intake_store,
    )

    junior_bypass = dict(outcome.envelope)
    junior_bypass["work_plan"] = {
        "assignee": "legal-drafter",
        "complexity": "complex",
        "with_review": False,
        "package_author": None,
    }
    with pytest.raises(ValueError, match="Steven package"):
        build_safe_delegate_command(junior_bypass, board="chris-home")

    raw_body = dict(outcome.envelope)
    raw_body["intake"] = {
        "retention": "task_body",
        "request_text": "Synthetic raw legal text must fail closed.",
    }
    with pytest.raises(ValueError, match="private-intake handle"):
        build_safe_delegate_command(raw_body, board="chris-home")



def test_dispatch_to_safe_delegate_rejects_noncanonical_intake_board():
    from gateway.department_topic_router import dispatch_to_safe_delegate
    with pytest.raises(ValueError, match="chris-home"):
        dispatch_to_safe_delegate({"department_id": "engineering_systems"}, board="legacy-board")

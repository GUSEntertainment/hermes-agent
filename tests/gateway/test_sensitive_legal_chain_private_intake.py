from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from gateway.department_topic_router import _PrivateIntakeBroker


def _safe_delegate_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "safe_delegate_card.py"
    spec = importlib.util.spec_from_file_location("safe_delegate_legal_chain_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sensitive_legal_chain_uses_one_handle_for_parent_and_drafter_only(monkeypatch):
    producer = _safe_delegate_module()
    broker = _PrivateIntakeBroker()
    raw = "Synthetic privileged legal request."
    handle = broker.put(
        request_text=raw,
        allowed_profiles=("steven", "legal-drafter"),
        idempotency_key="legal-chain-e2e",
    )
    route = producer.ResolvedRoute(
        board="chris-home",
        requesting_user=None,
        audience="internal",
        cockpit_board=None,
        source_session_key=None,
        tenant="chris-home",
        title="Synthetic legal packet",
        assignee="steven",
        owner_name="Steven",
        department_id="legal_compliance",
        department_name="Legal",
        pod="Legal",
        pod_slug="legal",
        member_role="Senior Legal",
        member_role_slug="senior-legal",
        handoff_label="legal/senior-legal",
        source_surface="telegram:-1004479003480:23",
        return_surface="telegram:-1004479003480:23",
        privacy_tier="legal",
        approval_gate="Chris/counsel",
        verifier="steven",
        reviewer_profiles=["steven"],
        route_reason="reviewed legal chain",
        proposed_new_department=False,
    )
    package = {
        "package_id": "pkg-legal-e2e",
        "package_class": "legal",
        "chain_contract": "legal_steven_drafter_steven",
        "created_by": "steven",
        "package_author_tier": "sol",
        "assignee": {"reviewer_profiles": ["steven"]},
        "fable_advisory_review": {"enabled": False},
        "model_tier": {},
        "irreversible_external_action_gates": {},
    }
    task_ids = iter(("t_aaaaaaaa", "t_bbbbbbbb", "t_cccccccc"))
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": next(task_ids)}),
        )

    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    result = producer.create_legal_inbound_chain(
        route=route,
        body=f"Opaque handle: {handle}",
        priority=3,
        idempotency_key="legal-chain-e2e",
        delegation_package=package,
    )

    bodies = [call[call.index("--body") + 1] for call in calls]
    assert result["parent_task_id"] == "t_aaaaaaaa"
    assert result["draft_task_id"] == "t_bbbbbbbb"
    assert result["review_task_id"] == "t_cccccccc"
    assert handle in bodies[0]
    assert handle in bodies[1]
    assert handle not in bodies[2]
    assert all(raw not in body for body in bodies)

    parent = broker._reserve_for_gateway_dispatch(
        handle, claimed_assignee="steven", claim_lock="parent-lock"
    )
    broker._commit_gateway_dispatch(parent)
    drafter = broker._reserve_for_gateway_dispatch(
        handle, claimed_assignee="legal-drafter", claim_lock="draft-lock"
    )
    broker._commit_gateway_dispatch(drafter)
    assert parent.request_text == drafter.request_text == raw

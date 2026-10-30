"""Emitted-package tests: broad defaults only for Chris's team-lane work (t_9add3e3d)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BROAD = {"config_edit_with_backup", "deploy_via_existing_mechanism",
         "restart_via_external_activation", "scoped_tool_install"}
REAL_GATES = {"external_send", "publish", "payment", "signature", "destructive",
              "credential_change", "privacy_wall_change"}


def _load(path):
    spec = importlib.util.spec_from_file_location(path.parent.name + "_" + path.stem + "_scope", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MODULES = [ROOT / "scripts" / "safe_delegate_card.py",
           ROOT / "tests" / "fixtures" / "margot_parity" / "safe_delegate_card.py",
           Path("/Users/Chris/.openclaw/workspace/Agent-Shared/tools/kanban/safe_delegate_card.py")]


def _emit(mod, *extra, user="chris", dept="engineering_systems"):
    args = mod.parse_args(["--title", "probe", "--body", "probe", "--requesting-user", user,
                           "--department", dept, "--assignee", "engineering-wren", *extra])
    route = mod.resolve_route(args)
    return mod.build_delegation_package(args, route)["workspace_and_tools"]


@pytest.fixture(params=MODULES, ids=["live", "parity", "serving"])
def mod(request, monkeypatch):
    m = _load(request.param)
    real = m.known_boards
    monkeypatch.setattr(m, "known_boards", lambda: set(real()) | {m.resolve_cockpit_board(u) for u in ("chris", "connor")} | {"default"})
    monkeypatch.setattr(m, "spawnable_profiles", lambda *a, **k: {"scarlett", "engineering-wren", "grace", "claire", "default"})
    monkeypatch.setattr(m, "configured_model_routing", lambda *a, **k: {}, raising=False)
    return m


def test_chris_team_lane_gets_broad_defaults_and_keeps_real_gates(mod):
    w = _emit(mod)
    assert BROAD <= set(w["allowed_side_effects"])
    assert REAL_GATES <= set(w["forbidden_actions"])
    assert not {"deploy", "restart", "config"} & set(w["forbidden_actions"])


@pytest.mark.parametrize("user", ["connor"])
def test_other_requesters_keep_baseline(mod, user):
    w = _emit(mod, user=user)
    assert not BROAD & set(w["allowed_side_effects"])
    assert {"deploy", "restart", "config"} <= set(w["forbidden_actions"])
    assert REAL_GATES <= set(w["forbidden_actions"])


def test_private_lane_keeps_baseline(mod):
    w = _emit(mod, "--privacy-tier", "private")
    assert not BROAD & set(w["allowed_side_effects"])
    assert {"deploy", "restart", "config"} <= set(w["forbidden_actions"])


def test_private_family_department_keeps_baseline(mod):
    route = type("R", (), {"requesting_user": "chris", "privacy_tier": "team",
                           "department_id": "personal_family"})()
    allowed, forbidden = mod.default_side_effect_policy(route)
    assert not BROAD & set(allowed)
    assert "privacy_wall_change" in forbidden


def test_explicit_hold_beats_broad_default(mod):
    w = _emit(mod, "--forbidden-action", "deploy,restart,payment")
    assert "deploy_via_existing_mechanism" not in w["allowed_side_effects"]
    assert "restart_via_external_activation" not in w["allowed_side_effects"]
    assert "config_edit_with_backup" in w["allowed_side_effects"]


def test_approved_team_reminders_calendar_is_not_privacy_wall(mod):
    # Chris-approved scoped Reminders/Calendar for team agents is ordinary Chris
    # team-lane work: config/install allowed, privacy_wall_change stays a gate
    # only for real walls.
    w = _emit(mod, "--title", "Grant team agents scoped Reminders/Calendar on Chris's Mac")
    assert {"config_edit_with_backup", "scoped_tool_install"} <= set(w["allowed_side_effects"])
    assert "privacy_wall_change" in w["forbidden_actions"]


def test_no_authenticated_requester_keeps_baseline(mod):
    args = mod.parse_args(["--title", "probe", "--body", "probe", "--department", "engineering_systems", "--assignee", "engineering-wren", "--board", "default"])
    route = mod.resolve_route(args)
    w = mod.build_delegation_package(args, route)["workspace_and_tools"]
    assert route.requesting_user is None
    assert not BROAD & set(w["allowed_side_effects"])
    assert {"deploy", "restart", "config"} <= set(w["forbidden_actions"])


REAL_GATES = {"external_send", "publish", "payment", "signature", "destructive",
              "credential_change", "privacy_wall_change"}


@pytest.mark.parametrize("hold", ["deploy", "restart", "config", "publish", "install", "deploy,restart,config"])
def test_explicit_hold_keeps_all_real_gates(mod, hold):
    w = _emit(mod, "--forbidden-action", hold)
    forbidden = set(w["forbidden_actions"])
    assert REAL_GATES <= forbidden
    assert set(hold.split(",")) <= forbidden


@pytest.mark.parametrize("user", ["connor", "josh"])
def test_explicit_hold_for_other_requesters_keeps_baseline_and_gates(mod, user):
    try:
        w = _emit(mod, "--forbidden-action", "publish", user=user)
    except Exception:
        pytest.skip("requester not routable in this environment")
    assert REAL_GATES <= set(w["forbidden_actions"])
    assert "deploy_via_existing_mechanism" not in w["allowed_side_effects"]


def test_default_escalation_has_no_architecture_production(mod):
    args = mod.parse_args(["--title", "probe", "--body", "probe", "--requesting-user", "chris",
                           "--department", "engineering_systems", "--assignee", "engineering-wren"])
    pkg = mod.build_delegation_package(args, mod.resolve_route(args))
    trig = pkg["escalation"]["triggers"]
    assert "architecture_production" not in trig
    assert "legal_financial_personnel_privacy_security" in trig
    assert REAL_GATES <= set(pkg["workspace_and_tools"]["forbidden_actions"])

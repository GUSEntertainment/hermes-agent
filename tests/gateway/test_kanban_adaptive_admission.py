from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.kanban_admission import (
    AdaptiveAdmissionController,
    AdmissionDecision,
    HostPressure,
    MachineGlobalAdmissionBudget,
    dispatch_with_global_budget,
)


def test_starts_at_full_hardware_derived_ceiling():
    controller = AdaptiveAdmissionController(cpu_count=16)
    decision = controller.update(
        HostPressure(0.4, "normal", "normal"), active_workers=0
    )
    assert controller.maximum_capacity == 24
    assert decision == AdmissionDecision(24, 24, "healthy")


@pytest.mark.parametrize(
    ("cpu_count", "requested", "expected"),
    [(4, None, 6), (16, 999, 24), (32, 999, 24), (16, 1, 1)],
)
def test_configured_ceiling_cannot_exceed_hardware(cpu_count, requested, expected):
    assert AdaptiveAdmissionController(
        cpu_count=cpu_count, maximum_capacity=requested
    ).maximum_capacity == expected


def test_pressure_backoff_does_not_request_active_worker_termination():
    controller = AdaptiveAdmissionController(cpu_count=16)
    decision = controller.update(
        HostPressure(1.3, "normal", "normal"), active_workers=11
    )
    assert decision == AdmissionDecision(11, 0, "paused")


@pytest.mark.parametrize(
    "pressure",
    [
        HostPressure(0.1, "critical", "normal"),
        HostPressure(0.1, "normal", "warning"),
        HostPressure(0.95, "normal", "normal"),
    ],
)
def test_any_elevated_host_pressure_immediately_holds_admission(pressure):
    decision = AdaptiveAdmissionController(cpu_count=16).update(
        pressure, active_workers=7
    )
    assert decision.capacity == 7
    assert decision.new_admissions == 0


def test_recovery_is_hysteretic_then_grows_gradually():
    controller = AdaptiveAdmissionController(
        cpu_count=16, recovery_samples=2, growth_step=2
    )
    controller.update(HostPressure(1.3), active_workers=9)
    first = controller.update(HostPressure(0.4), active_workers=9)
    second = controller.update(HostPressure(0.4), active_workers=9)
    third = controller.update(HostPressure(0.4), active_workers=9)
    assert first == AdmissionDecision(9, 0, "recovering")
    assert second == AdmissionDecision(11, 2, "healthy")
    assert third == AdmissionDecision(13, 4, "healthy")


@pytest.mark.parametrize(
    "pressure",
    [
        HostPressure(float("nan"), "normal", "normal"),
        HostPressure("0.1", "normal", "normal"),
        HostPressure(0.1, "unknown", "normal"),
        HostPressure(0.1, "normal", object()),
        object(),
    ],
)
def test_malformed_telemetry_fails_closed_at_controller_boundary(pressure):
    assert AdaptiveAdmissionController(cpu_count=16).update(
        pressure, active_workers=4
    ) == AdmissionDecision(4, 0, "paused")


@pytest.mark.parametrize("active", [None, True, "4", -1, float("inf")])
def test_malformed_active_worker_count_fails_closed(active):
    decision = AdaptiveAdmissionController(cpu_count=16).update(
        HostPressure(0.1), active_workers=active
    )
    assert decision.new_admissions == 0
    assert decision.state == "paused"


def test_machine_global_budget_is_spent_once_across_boards():
    budget = MachineGlobalAdmissionBudget(total_capacity=16, total_active=12)
    assert budget.cap_for_board(board_active=5) == 9
    budget.record_spawned(3)
    assert budget.cap_for_board(board_active=4) == 5
    budget.record_spawned(1)
    assert budget.cap_for_board(board_active=3) == 3


def test_capacity_below_active_workers_never_requests_termination():
    budget = MachineGlobalAdmissionBudget(total_capacity=8, total_active=12)
    assert budget.remaining == 0
    assert budget.cap_for_board(board_active=5) == 5


def test_dispatch_spends_one_machine_budget_in_board_order():
    active = {"alpha": 5, "beta": 4, "gamma": 3}
    spawned = {"alpha": 3, "beta": 1, "gamma": 0}
    calls = []

    def dispatch(slug, cap):
        calls.append((slug, cap))
        return SimpleNamespace(spawned=list(range(spawned[slug])))

    results = dispatch_with_global_budget(
        tuple(active),
        total_capacity=16,
        active_count=active.__getitem__,
        dispatch_board=dispatch,
    )
    assert calls == [("alpha", 9), ("beta", 5), ("gamma", 3)]
    assert [slug for slug, _ in results] == list(active)


def test_dispatch_resolves_all_active_counts_before_any_spawn():
    calls = []

    def active_count(slug):
        if slug == "beta":
            raise RuntimeError("count unavailable")
        return 1

    with pytest.raises(RuntimeError, match="count unavailable"):
        dispatch_with_global_budget(
            ("alpha", "beta"),
            total_capacity=8,
            active_count=active_count,
            dispatch_board=lambda slug, cap: calls.append((slug, cap)),
        )
    assert calls == []


@pytest.mark.parametrize("gate", [0, -1, True, "1", object()])
def test_nonpositive_or_malformed_admission_gate_skips_all_side_effects(gate):
    calls = []
    results = dispatch_with_global_budget(
        ("alpha", "beta"),
        total_capacity=8,
        new_admissions=gate,
        active_count=lambda slug: calls.append(("count", slug)),
        dispatch_board=lambda slug, cap: calls.append(("dispatch", slug, cap)),
    )
    assert calls == []
    assert results == [("alpha", None), ("beta", None)]


def test_malformed_spawn_result_exhausts_remaining_budget():
    calls = []
    dispatch_with_global_budget(
        ("alpha", "beta"),
        total_capacity=2,
        active_count=lambda _slug: 0,
        dispatch_board=lambda slug, cap: calls.append((slug, cap))
        or SimpleNamespace(spawned=7 if slug == "alpha" else []),
    )
    assert calls == [("alpha", 2), ("beta", 0)]


def test_macos_pressure_probe_uses_absolute_tools_and_parses_health(monkeypatch):
    import gateway.kanban_admission as admission

    calls = []
    outputs = {
        "/usr/bin/top": "CPU usage: 10.0% user, 5.0% sys, 85.0% idle",
        "/usr/bin/memory_pressure": "System-wide memory free percentage: 80%",
        "/usr/bin/pmset": "No thermal warning level has been recorded",
    }
    monkeypatch.setattr(admission.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(admission.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        admission,
        "_run_probe",
        lambda argv: calls.append(argv) or outputs[argv[0]],
    )
    pressure = admission.sample_host_pressure(cpu_count=16)
    assert [argv[0] for argv in calls] == [
        "/usr/bin/top",
        "/usr/bin/memory_pressure",
        "/usr/bin/pmset",
    ]
    assert pressure.load_per_cpu == pytest.approx(0.15)
    assert pressure.memory == pressure.thermal == "normal"


def test_malformed_successful_probe_output_fails_closed(monkeypatch):
    import gateway.kanban_admission as admission

    monkeypatch.setattr(admission.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(admission.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(admission, "_run_probe", lambda _argv: "unexpected output")
    pressure = admission.sample_host_pressure(cpu_count=16)
    assert pressure.load_per_cpu >= 1.2
    assert pressure.memory == pressure.thermal == "warning"
    assert AdaptiveAdmissionController(cpu_count=16).update(
        pressure, active_workers=6
    ).new_admissions == 0


def test_duplicate_or_conflicting_probe_markers_fail_closed(monkeypatch):
    import gateway.kanban_admission as admission

    outputs = {
        "/usr/bin/top": "CPU usage: 1% user, 1% sys, 98% idle\nCPU usage: 0% user, 0% sys, 100% idle",
        "/usr/bin/memory_pressure": "System-wide memory free percentage: 80%\nSystem-wide memory free percentage: 81%",
        "/usr/bin/pmset": "No thermal warning level has been recorded\nThermal warning level = 1",
    }
    monkeypatch.setattr(admission.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(admission.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(admission, "_run_probe", lambda argv: outputs[argv[0]])
    pressure = admission.sample_host_pressure(cpu_count=16)
    assert pressure.load_per_cpu >= 1.2
    assert pressure.memory == pressure.thermal == "warning"


def test_probe_output_is_hard_bounded():
    import gateway.kanban_admission as admission

    assert admission._run_probe(
        ["/usr/bin/python3", "-c", "print('x' * 70000)"]
    ) is None

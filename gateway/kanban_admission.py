"""Host-aware admission control for the gateway Kanban dispatcher.

The controller limits *new* worker admissions only. Existing workers are never
terminated when pressure rises or the computed capacity falls.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import platform
import re
import select
import subprocess
import time
from typing import Literal

PressureLevel = Literal["normal", "warning", "critical"]


@dataclass(frozen=True)
class HostPressure:
    load_per_cpu: float
    memory: PressureLevel = "normal"
    thermal: PressureLevel = "normal"


@dataclass(frozen=True)
class AdmissionDecision:
    capacity: int
    new_admissions: int
    state: str


class AdaptiveAdmissionController:
    """Aggressive controller with immediate backoff and hysteretic recovery."""

    def __init__(
        self,
        *,
        cpu_count: int | None = None,
        maximum_capacity: int | None = None,
        recovery_samples: int = 2,
        growth_step: int = 2,
    ) -> None:
        raw_cpu_count = os.cpu_count() if cpu_count is None else cpu_count
        cpus = self._strict_positive_int(raw_cpu_count, default=1, upper=4096)
        hardware_ceiling = min(24, int(math.ceil(cpus * 1.5)))
        if (
            isinstance(maximum_capacity, int)
            and not isinstance(maximum_capacity, bool)
            and maximum_capacity > 0
        ):
            requested_ceiling = maximum_capacity
        elif maximum_capacity is None:
            requested_ceiling = hardware_ceiling
        else:
            requested_ceiling = 1
        self.maximum_capacity = max(1, min(hardware_ceiling, requested_ceiling))
        self._recovery_samples = self._positive_int(recovery_samples, 2)
        self._growth_step = self._positive_int(growth_step, 2)
        self._capacity = self.maximum_capacity
        self._healthy_samples = 0
        self._paused = False
        self._sampled = False

    @staticmethod
    def _strict_positive_int(value, *, default: int, upper: int) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > upper
        ):
            return default
        return value

    @staticmethod
    def _positive_int(value, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def update(self, pressure: HostPressure, *, active_workers: int) -> AdmissionDecision:
        if (
            not isinstance(active_workers, int)
            or isinstance(active_workers, bool)
            or active_workers < 0
        ):
            self._paused = True
            self._healthy_samples = 0
            self._capacity = 0
            return AdmissionDecision(0, 0, "paused")
        active = active_workers
        memory_level = getattr(pressure, "memory", None)
        thermal_level = getattr(pressure, "thermal", None)
        try:
            raw_load = getattr(pressure, "load_per_cpu")
            if not isinstance(raw_load, (int, float)) or isinstance(raw_load, bool):
                raise TypeError("load_per_cpu must be a real number")
            load_value = float(raw_load)
            pressure_valid = math.isfinite(load_value) and load_value >= 0
        except (AttributeError, OverflowError, TypeError, ValueError):
            pressure_valid = False
            load_value = math.inf
        valid_levels = ("normal", "warning", "critical")
        pressure_valid = (
            pressure_valid
            and isinstance(memory_level, str)
            and memory_level in valid_levels
            and isinstance(thermal_level, str)
            and thermal_level in valid_levels
        )
        if not pressure_valid:
            self._paused = True
            self._healthy_samples = 0
            self._capacity = active
            return AdmissionDecision(active, 0, "paused")

        critical = (
            load_value >= 1.20
            or memory_level == "critical"
            or thermal_level != "normal"
        )
        elevated = load_value >= 0.90 or memory_level == "warning"

        if critical or elevated:
            self._paused = True
            self._healthy_samples = 0
            self._capacity = active
            state = "paused" if critical else "holding"
            return AdmissionDecision(active, 0, state)

        if self._paused:
            self._healthy_samples += 1
            if self._healthy_samples < self._recovery_samples:
                return AdmissionDecision(active, 0, "recovering")
            self._capacity = min(
                self.maximum_capacity,
                max(active, self._capacity) + self._growth_step,
            )
            self._paused = False
            self._sampled = True
            return AdmissionDecision(
                self._capacity,
                max(0, self._capacity - active),
                "healthy",
            )

        if not self._sampled:
            self._sampled = True
        else:
            self._capacity = min(
                self.maximum_capacity,
                max(active, self._capacity) + self._growth_step,
            )
        return AdmissionDecision(
            self._capacity,
            max(0, self._capacity - active),
            "healthy",
        )


def _strict_nonnegative_int(value) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        return None
    return value


class MachineGlobalAdmissionBudget:
    """Spend one host-wide admission budget sequentially across all boards."""

    def __init__(self, *, total_capacity: int, total_active: int) -> None:
        capacity = _strict_nonnegative_int(total_capacity)
        active = _strict_nonnegative_int(total_active)
        self.remaining = (
            max(0, capacity - active)
            if capacity is not None and active is not None
            else 0
        )

    def cap_for_board(self, *, board_active: int) -> int:
        active = _strict_nonnegative_int(board_active)
        return active + self.remaining if active is not None else 0

    def record_spawned(self, count: int) -> None:
        spawned = _strict_nonnegative_int(count)
        if spawned is None:
            self.remaining = 0
            return
        self.remaining = max(0, self.remaining - spawned)


def dispatch_with_global_budget(
    board_slugs: tuple[str, ...],
    *,
    total_capacity: int,
    active_count,
    dispatch_board,
    new_admissions: int | None = None,
):
    """Dispatch boards sequentially while sharing one machine-wide budget."""
    if new_admissions is not None:
        if (
            not isinstance(new_admissions, int)
            or isinstance(new_admissions, bool)
            or new_admissions <= 0
        ):
            return [(slug, None) for slug in board_slugs]
    if _strict_nonnegative_int(total_capacity) is None:
        return [(slug, None) for slug in board_slugs]
    active_by_board = {}
    for slug in board_slugs:
        active = _strict_nonnegative_int(active_count(slug))
        if active is None:
            return [(board_slug, None) for board_slug in board_slugs]
        active_by_board[slug] = active
    budget = MachineGlobalAdmissionBudget(
        total_capacity=total_capacity,
        total_active=sum(active_by_board.values()),
    )
    if new_admissions is not None:
        budget.remaining = min(budget.remaining, new_admissions)
    results = []
    for slug in board_slugs:
        result = dispatch_board(
            slug,
            budget.cap_for_board(board_active=active_by_board[slug]),
        )
        results.append((slug, result))
        try:
            spawned = result.spawned
        except Exception:
            budget.record_spawned(None)
            continue
        if not isinstance(spawned, (list, tuple)):
            budget.record_spawned(None)
        else:
            budget.record_spawned(len(spawned))
    return results


def _bounded_float(token: str, *, max_length: int = 8) -> float | None:
    if not isinstance(token, str) or len(token) > max_length:
        return None
    try:
        value = float(token)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _run_probe(argv: list[str]) -> str | None:
    """Run a trusted probe with hard time and captured-output bounds."""
    output_limit = 64 * 1024
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        assert process.stdout is not None
        fd = process.stdout.fileno()
        output = bytearray()
        deadline = time.monotonic() + 2.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, 2)
            readable, _writable, _exceptional = select.select(
                [fd], [], [], remaining
            )
            if not readable:
                raise subprocess.TimeoutExpired(argv, 2)
            chunk = os.read(fd, min(4096, output_limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > output_limit:
                return None
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if returncode != 0:
            return None
        return bytes(output).decode("utf-8", errors="replace")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=0.5)
            except subprocess.SubprocessError:
                pass
            if process.stdout is not None:
                process.stdout.close()


def sample_host_pressure(*, cpu_count: int | None = None) -> HostPressure:
    """Take a bounded, fail-safe host sample suitable for a dispatcher tick."""
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    try:
        raw_load = float(os.getloadavg()[0]) / cpus
        if not math.isfinite(raw_load) or raw_load < 0:
            raise ValueError("non-finite or negative load average")
        load_per_cpu = raw_load
        load_known = True
    except (AttributeError, OSError, TypeError, ValueError):
        load_per_cpu = 1.20
        load_known = False

    memory: PressureLevel = "warning"
    thermal: PressureLevel = "warning"
    if platform.system() == "Darwin":
        top_output = _run_probe(["/usr/bin/top", "-l", "1", "-n", "0"])
        top_known = False
        if top_output:
            cpu_marker_count = len(
                re.findall(r"(?m)^[ \t]*CPU usage:", top_output)
            )
            idle_matches = re.findall(
                r"(?m)^[ \t]*CPU usage:[^\n]*?[\s,]"
                r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))% idle[ \t]*$",
                top_output,
            )
            if cpu_marker_count == 1 and len(idle_matches) == 1:
                idle_percent = _bounded_float(idle_matches[0])
                if idle_percent is not None and 0.0 <= idle_percent <= 100.0:
                    top_known = True
                    instantaneous_use = 1.0 - idle_percent / 100.0
                    load_per_cpu = max(load_per_cpu, instantaneous_use)
        if not top_known:
            load_per_cpu = max(load_per_cpu, 1.20)

        memory_output = _run_probe(["/usr/bin/memory_pressure", "-Q"])
        if memory_output is not None:
            marker_count = len(
                re.findall(
                    r"(?im)^[ \t]*(?:System-wide[ \t]+)?memory free percentage:",
                    memory_output,
                )
            )
            matches = re.findall(
                r"(?im)^[ \t]*(?:System-wide[ \t]+)?memory free percentage:"
                r"[ \t]*(\d+)%[ \t]*$",
                memory_output,
            )
            if marker_count == 1 and len(matches) == 1 and len(matches[0]) <= 3:
                free_percent = int(matches[0])
                if not 0 <= free_percent <= 100:
                    memory = "warning"
                elif free_percent < 5:
                    memory = "critical"
                elif free_percent < 10:
                    memory = "warning"
                else:
                    memory = "normal"

        thermal_output = _run_probe(["/usr/bin/pmset", "-g", "therm"])
        if thermal_output is not None:
            limit_pairs = re.findall(
                r"(?m)^[ \t]*(CPU_Speed_Limit|CPU_Scheduler_Limit)"
                r"[ \t]*=[ \t]*(\d+)[ \t]*$",
                thermal_output,
            )
            limit_marker_count = len(
                re.findall(
                    r"(?m)^[ \t]*(?:CPU_Speed_Limit|CPU_Scheduler_Limit)"
                    r"[ \t]*=",
                    thermal_output,
                )
            )
            limit_keys = [key for key, _value in limit_pairs]
            limit_tokens = [value for _key, value in limit_pairs]
            limits_parseable = all(len(value) <= 3 for value in limit_tokens)
            limits = [int(value) for value in limit_tokens] if limits_parseable else []
            no_warning_count = len(
                re.findall(
                    r"(?im)^[ \t]*(?:Note:[ \t]*)?no thermal warning level"
                    r"(?: has been recorded)?[ \t]*$",
                    thermal_output,
                )
            )
            warning_marker_count = len(
                re.findall(
                    r"(?im)^[ \t]*thermal warning level[ \t]*[:=]",
                    thermal_output,
                )
            )
            warning_tokens = re.findall(
                r"(?im)^[ \t]*thermal warning level[ \t]*[:=]"
                r"[ \t]*(\d+)[ \t]*$",
                thermal_output,
            )
            warning_values_parseable = all(
                len(value) <= 3 for value in warning_tokens
            )
            warning_values = (
                [int(value) for value in warning_tokens]
                if warning_values_parseable
                else []
            )
            duplicate_limits = len(limit_keys) != len(set(limit_keys))
            contradictory = no_warning_count > 0 and bool(warning_values)
            limits_valid = not limits or all(0 <= value <= 100 for value in limits)
            if (
                contradictory
                or no_warning_count > 1
                or len(warning_values) > 1
                or warning_marker_count != len(warning_values)
                or duplicate_limits
                or not limits_parseable
                or not warning_values_parseable
                or limit_marker_count != len(limit_pairs)
                or not limits_valid
            ):
                thermal = "warning"
            else:
                signals: list[PressureLevel] = []
                if limits:
                    if min(limits) < 70:
                        signals.append("critical")
                    elif min(limits) < 100:
                        signals.append("warning")
                    else:
                        signals.append("normal")
                if warning_values:
                    signals.append("warning" if warning_values[0] > 0 else "normal")
                if no_warning_count == 1:
                    signals.append("normal")
                if "critical" in signals:
                    thermal = "critical"
                elif "warning" in signals:
                    thermal = "warning"
                elif signals:
                    thermal = "normal"
    elif load_known:
        # Adaptive mode currently has complete pressure probes only on macOS.
        # A valid load sample is not enough to infer memory or thermal health.
        memory = "warning"
        thermal = "warning"

    return HostPressure(
        load_per_cpu=load_per_cpu,
        memory=memory,
        thermal=thermal,
    )

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path


import pytest

from scripts import run_tests_parallel as runner


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd/process groups")


def _authority(*, pgid: int = 4100, leader_start: str = "leader-start", live: bool = True):
    del live
    return runner._GroupAuthority(
        pgid=pgid,
        leader_pid=pgid,
        leader_start=leader_start,
        leader_pidfd=91,
        owner_pid=os.getpid(),
        domain_fd=-1,
        domain_path=Path("/tmp/test-domain"),
        domain_token="a" * 64,
        domain_stat=(1, 2),
    )


def test_group_auth_accepts_kernel_adopted_descendant_after_leader_exit(monkeypatch):
    authority = _authority(live=False)
    child = authority.pgid + 1
    members = {child: ("child-start", authority.owner_pid, authority.pgid)}
    monkeypatch.setattr(
        runner,
        "_group_snapshots",
        lambda pgid: members,
    )
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: members.get(pid))
    monkeypatch.setattr(
        runner,
        "_registered_domain_identities",
        lambda selected: {pgid + 1: {"child-start"}} if (pgid := selected.pgid) else {},
    )
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: False)

    assert runner._authenticate_group_members(authority) == {
        child: "child-start"
    }


def test_group_auth_accepts_child_of_kernel_adopted_non_python_wrapper(monkeypatch):
    authority = _authority(live=False)
    ancestor = authority.pgid + 1
    child = authority.pgid + 2
    members = {
        ancestor: ("wrapper-start", authority.owner_pid, authority.pgid),
        child: ("helper-start", ancestor, authority.pgid),
    }
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: members)
    monkeypatch.setattr(
        runner,
        "_process_snapshot",
        lambda pid: members.get(pid),
    )
    monkeypatch.setattr(
        runner,
        "_registered_domain_identities",
        lambda selected: {ancestor: {"wrapper-start"}},
    )
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: False)

    assert runner._authenticate_group_members(authority) == {
        ancestor: "wrapper-start",
        child: "helper-start",
    }


def test_group_auth_rejects_reused_pgid_after_original_leader_exit(monkeypatch):
    authority = _authority(live=False)
    monkeypatch.setattr(
        runner,
        "_group_snapshots",
        lambda pgid: {pgid: ("reused-start", 1, pgid)},
    )
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: False)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="reused"):
        runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert delivered == []


def test_group_auth_rejects_foreign_injected_member_without_any_delivery(monkeypatch):
    authority = _authority(live=True)
    members = {
        authority.pgid: (authority.leader_start, 100, authority.pgid),
        authority.pgid + 1: ("foreign-start", 999, authority.pgid),
    }
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: members)
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: False)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="foreign"):
        runner._terminate_authenticated_group(
            authority, getattr(signal, "SIGKILL", signal.SIGTERM)
        )
    assert delivered == []


def test_group_auth_retries_when_member_exits_between_snapshots(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    child = ("child-start", authority.leader_pid, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader, authority.leader_pid + 1: child},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
        ]
    )
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: True)
    monkeypatch.setattr(
        runner,
        "_process_snapshot",
        lambda pid: leader if pid == authority.leader_pid else None,
    )
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert delivered == [(authority.leader_pid, authority.leader_start, signal.SIGKILL)]


def test_group_auth_retries_when_member_exits_during_ancestry_proof(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    child_pid = authority.leader_pid + 1
    child = ("child-start", authority.leader_pid, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader, child_pid: child},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
        ]
    )
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: False)
    monkeypatch.setattr(runner, "_stable_descendant_of_adopted", lambda *args: False)
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: leader)

    assert runner._authenticate_group_members(authority) == {
        authority.leader_pid: authority.leader_start
    }


def test_group_auth_retries_when_leader_exits_and_child_is_adopted(monkeypatch):
    authority = _authority(live=True)
    child_pid = authority.leader_pid + 1
    leader = (authority.leader_start, 100, authority.pgid)
    child = ("child-start", authority.leader_pid, authority.pgid)
    adopted_child = ("child-start", authority.owner_pid, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader, child_pid: child},
            {child_pid: adopted_child},
            {child_pid: adopted_child},
            {child_pid: adopted_child},
        ]
    )
    leader_liveness = iter([True, False])
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: next(leader_liveness))
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: True)
    monkeypatch.setattr(
        runner,
        "_process_snapshot",
        lambda pid: adopted_child if pid == child_pid else None,
    )

    assert runner._authenticate_group_members(authority) == {child_pid: "child-start"}


def test_group_auth_retries_when_authenticated_registered_member_is_already_gone(monkeypatch):
    authority = _authority(live=True)
    registered_pid = authority.leader_pid + 1
    leader = (authority.leader_start, 100, authority.pgid)
    adopted = ("adopted-start", authority.owner_pid, registered_pid)
    group = {authority.leader_pid: leader}
    registered_snapshots = iter([adopted, adopted, adopted, None, None])
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: group)
    monkeypatch.setattr(
        runner,
        "_registered_domain_identities",
        lambda selected: {registered_pid: {"adopted-start"}},
    )
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(
        runner,
        "_process_snapshot",
        lambda pid: (
            leader
            if pid == authority.leader_pid
            else next(registered_snapshots)
        ),
    )

    assert runner._authenticate_group_members(authority) == {
        authority.leader_pid: authority.leader_start
    }


def test_group_auth_rejects_member_added_between_snapshots_without_delivery(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    foreign_pid = authority.leader_pid + 1
    snapshots = iter(
        [
            {authority.leader_pid: leader},
            {
                authority.leader_pid: leader,
                foreign_pid: ("foreign-start", 999, authority.pgid),
            },
        ]
    )
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: leader)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="membership changed"):
        runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert delivered == []


def test_group_auth_rejects_identity_change_between_snapshots_without_delivery(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    changed = ("reused-start", 100, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader},
            {authority.leader_pid: changed},
        ]
    )
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="membership changed"):
        runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert delivered == []


def test_group_auth_rejects_parent_change_without_exit_and_without_delivery(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    child_pid = authority.leader_pid + 1
    child = ("child-start", authority.leader_pid, authority.pgid)
    reparented = ("child-start", 999, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader, child_pid: child},
            {authority.leader_pid: leader, child_pid: reparented},
        ]
    )
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: True)
    monkeypatch.setattr(
        runner,
        "_process_snapshot",
        lambda pid: reparented if pid == child_pid else leader,
    )
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="membership changed"):
        runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert delivered == []


def test_group_auth_continuous_shrink_is_bounded_without_delivery(monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    child_pid = authority.leader_pid + 1
    child = ("child-start", authority.leader_pid, authority.pgid)
    calls = 0

    def changing_snapshots(pgid):
        nonlocal calls
        calls += 1
        if calls % 2:
            return {authority.leader_pid: leader, child_pid: child}
        return {authority.leader_pid: leader}

    monkeypatch.setattr(runner, "_group_snapshots", changing_snapshots)
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: True)
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: True)
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: leader)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup, match="did not stabilize"):
        runner._terminate_authenticated_group(authority, signal.SIGKILL)

    assert calls == runner._GROUP_AUTH_MAX_ATTEMPTS * 2
    assert delivered == []


def test_benign_cleanup_shrink_preserves_pass_classification(tmp_path, monkeypatch):
    authority = _authority(live=True)
    leader = (authority.leader_start, 100, authority.pgid)
    child = ("child-start", authority.leader_pid, authority.pgid)
    snapshots = iter(
        [
            {authority.leader_pid: leader, authority.leader_pid + 1: child},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
            {authority.leader_pid: leader},
        ]
    )

    class CompletedProcess:
        pid = authority.leader_pid
        returncode = 0

        def communicate(self, timeout=None):
            return ("1 passed in 0.01s\n", None)

    def capture(proc, domain_fd, domain_path, domain_token):
        selected = _authority(live=True)
        selected.leader_pidfd = None
        selected.domain_fd = domain_fd
        selected.domain_path = domain_path
        selected.domain_token = domain_token
        domain_stat = os.fstat(domain_fd)
        selected.domain_stat = (domain_stat.st_dev, domain_stat.st_ino)
        return selected

    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(tmp_path))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: CompletedProcess())
    monkeypatch.setattr(runner, "_capture_group_authority", capture)
    monkeypatch.setattr(runner, "_group_snapshots", lambda pgid: next(snapshots))
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_stable_descendant_of", lambda *args: True)
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: leader)
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: None)

    _file, rc, output, summary, _wall = runner._run_one_file_once(
        tmp_path / "test_probe.py", [], tmp_path, file_timeout=1
    )

    assert rc == 0
    assert summary == {"passed": 1}
    assert "runner crashed" not in output
    assert "NO TESTS RAN" not in output


def test_group_auth_rejects_authority_for_foreign_live_gateway(monkeypatch):
    gateway_pid = 15237
    authority = _authority(pgid=gateway_pid)
    authority.leader_pid = gateway_pid + 1
    monkeypatch.setattr(
        runner,
        "_group_snapshots",
        lambda pgid: {gateway_pid: ("gateway-start", 1, gateway_pid)},
    )
    monkeypatch.setattr(runner, "_registered_domain_identities", lambda selected: {})
    monkeypatch.setattr(runner, "_pidfd_is_live", lambda fd: False)
    delivered = []
    monkeypatch.setattr(runner, "_signal_pid_identity", lambda *args: delivered.append(args))

    with pytest.raises(runner.UnsafeProcessGroup):
        runner._terminate_authenticated_group(
            authority, getattr(signal, "SIGKILL", signal.SIGTERM)
        )
    assert delivered == []


@dataclass
class _CaptureFailureProcess:
    pid: int = 424242
    returncode: int | None = None
    terminated: int = 0
    killed: int = 0
    waited: int = 0

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        if self.waited == 1:
            raise subprocess.TimeoutExpired(["probe"], timeout)
        self.returncode = -9
        return self.returncode


def test_capture_failure_wait_after_kill_is_bounded():
    """A broken Popen.wait implementation cannot hang authority-failure cleanup."""
    stalled = threading.Event()

    class StalledAfterKill(_CaptureFailureProcess):
        def wait(self, timeout=None):
            self.waited += 1
            if self.waited == 1:
                raise subprocess.TimeoutExpired(["probe"], timeout)
            stalled.wait(30)
            return -9

    proc = StalledAfterKill()
    cleanup = threading.Thread(
        target=runner._terminate_and_reap_untrusted_child,
        args=(proc,),
        daemon=True,
    )
    started = time.monotonic()
    cleanup.start()
    cleanup.join(timeout=3)

    assert not cleanup.is_alive(), "wait-after-kill exceeded the cleanup bound"
    assert time.monotonic() - started < 3
    assert proc.terminated == 1
    assert proc.killed == 1
    assert proc.waited == 2


def test_capture_failure_terminates_reaps_child_and_closes_domain_fd(
    tmp_path: Path, monkeypatch
):
    """Authority setup failure cannot leak the already-started test child."""
    proc = _CaptureFailureProcess()
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(tmp_path))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        runner,
        "_capture_group_authority",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected capture failure")),
    )

    with pytest.raises(OSError, match="injected capture failure"):
        runner._run_one_file_once(
            tmp_path / "test_probe.py", [], tmp_path, file_timeout=1
        )

    assert proc.terminated == 1
    assert proc.killed == 1
    assert proc.waited == 2
    domain_files = list(tmp_path.glob(".process-domain-*"))
    assert len(domain_files) == 1
    assert all(
        os.readlink(fd) != str(domain_files[0])
        for fd in Path("/proc/self/fd").iterdir()
        if fd.exists()
    )


@pytest.mark.parametrize("failure_point", ["fstat", "append"])
def test_capture_failure_closes_partially_opened_pidfd(
    tmp_path: Path, monkeypatch, failure_point: str
):
    """A pidfd acquired before later capture work fails is always closed."""
    domain = tmp_path / "domain"
    domain_fd = os.open(domain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    pidfd = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(runner, "_open_pidfd", lambda pid: pidfd)
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: ("start", 1, pid))
    if failure_point == "fstat":
        real_fstat = os.fstat
        monkeypatch.setattr(
            runner.os,
            "fstat",
            lambda fd: (
                (_ for _ in ()).throw(OSError("fstat"))
                if fd == domain_fd
                else real_fstat(fd)
            ),
        )
    else:
        monkeypatch.setattr(
            runner,
            "_append_domain_identity",
            lambda *args: (_ for _ in ()).throw(OSError("append")),
        )
    try:
        with pytest.raises(OSError, match=failure_point):
            runner._capture_group_authority(
                _CaptureFailureProcess(), domain_fd, domain, "a" * 64
            )
        with pytest.raises(OSError):
            os.fstat(pidfd)
    finally:
        os.close(domain_fd)


def test_darwin_capture_uses_portable_identity_without_pidfd(tmp_path: Path, monkeypatch):
    """Darwin cannot touch Linux pidfd or /proc primitives."""
    domain = tmp_path / "domain"
    domain_fd = os.open(domain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(
        runner,
        "_open_pidfd",
        lambda pid: (_ for _ in ()).throw(AssertionError("pidfd used on Darwin")),
    )
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: ("12.5", 1, pid))
    try:
        authority = runner._capture_group_authority(
            _CaptureFailureProcess(), domain_fd, domain, "a" * 64
        )
        assert authority.leader_pidfd is None
        assert authority.leader_start == "12.5"
    finally:
        os.close(domain_fd)


def test_portable_capture_fails_closed_without_process_identity(tmp_path: Path, monkeypatch):
    domain = tmp_path / "domain"
    domain_fd = os.open(domain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: None)
    try:
        with pytest.raises(runner.UnsafeProcessGroup, match="identity"):
            runner._capture_group_authority(
                _CaptureFailureProcess(), domain_fd, domain, "a" * 64
            )
    finally:
        os.close(domain_fd)


def test_portable_process_snapshot_uses_psutil_not_proc(monkeypatch):
    """Portable identity is create-time + parent + process group."""
    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 123.25

        def ppid(self):
            return 7

    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=FakeProcess))
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: 88)

    assert runner._process_snapshot(42) == ("123.25", 7, 88)


def test_windows_cleanup_uses_authenticated_native_delivery_not_taskkill(
    tmp_path: Path, monkeypatch
):
    """The startup guard must not be able to block the runner's own cleanup."""
    domain = tmp_path / "domain"
    domain_fd = os.open(domain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    authority = _authority()
    authority.domain_fd = domain_fd
    authority.leader_pidfd = None
    delivered = []
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("taskkill invoked")),
    )
    monkeypatch.setattr(
        runner,
        "_terminate_authenticated_group",
        lambda selected, sig: delivered.append((selected, sig)),
    )

    runner._kill_tree(_CaptureFailureProcess(), authority)

    assert delivered and delivered[0][0] is authority
    with pytest.raises(OSError):
        os.fstat(domain_fd)


@pytest.mark.parametrize("current", ["77.5", "changed"])
def test_windows_native_delivery_checks_identity_before_kill(monkeypatch, current: str):
    killed = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return float(current) if current != "changed" else 99.0

        def kill(self):
            killed.append(self.pid)

    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=FakeProcess))
    monkeypatch.setattr(runner, "_process_snapshot", lambda pid: (current, 1, 0))

    if current == "changed":
        with pytest.raises(runner.UnsafeProcessGroup, match="identity changed"):
            runner._signal_pid_identity(42, "77.5", signal.SIGKILL)
        assert killed == []
    else:
        runner._signal_pid_identity(42, "77.5", signal.SIGKILL)
        assert killed == [42]


def test_quick_file_survives_namespace_wrapper_exec(tmp_path: Path):
    probe = tmp_path / "test_quick.py"
    probe.write_text("def test_quick():\n    assert True\n", encoding="utf-8")
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update(
        HERMES_PYTHON="/opt/data/forwardport/v0205/control/test-env-c5b0cfd9/bin/python",
        HERMES_TEST_WORKERS="1",
        HERMES_TEST_FILE_RETRIES="0",
    )
    proc = subprocess.run(
        [str(repo_root / "scripts" / "run_tests.sh"), str(probe), "-q"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stdout
    assert "runner crashed" not in proc.stdout
    assert "1 tests passed" in proc.stdout


def test_timeout_kills_authenticated_descendant_and_collects_output(tmp_path: Path):
    probe = tmp_path / "test_timeout_descendant.py"
    handoff = tmp_path / "child.pid"
    probe.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"HANDOFF = Path({str(handoff)!r})\n"
        "def test_timeout():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "    HANDOFF.write_text(str(child.pid))\n"
        "    time.sleep(600)\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update(
        HERMES_PYTHON="/opt/data/forwardport/v0205/control/test-env-c5b0cfd9/bin/python",
        HERMES_TEST_WORKERS="1",
        HERMES_TEST_FILE_RETRIES="0",
        HERMES_TEST_FILE_TIMEOUT="2",
    )
    proc = subprocess.run(
        [str(repo_root / "scripts" / "run_tests.sh"), str(probe), "-q"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, proc.stdout
    assert "runner crashed" not in proc.stdout
    assert "process tree SIGKILL'd" in proc.stdout
    child_pid = int(handoff.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists(), proc.stdout


@pytest.mark.live_system_guard_bypass
@pytest.mark.linux_only
def test_happy_path_kills_authenticated_detached_descendant(tmp_path: Path):
    """A registered child that creates a new session cannot escape cleanup."""
    probe = tmp_path / "test_detached_descendant.py"
    handoff = tmp_path / "detached.pid"
    probe.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        f"HANDOFF = Path({str(handoff)!r})\n"
        "def test_detaches():\n"
        "    child = subprocess.Popen(\n"
        "        [sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    )\n"
        "    HANDOFF.write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update(
        HERMES_PYTHON="/opt/data/forwardport/v0205/control/test-env-c5b0cfd9/bin/python",
        HERMES_TEST_WORKERS="1",
        HERMES_TEST_FILE_RETRIES="0",
    )
    proc = subprocess.run(
        [str(repo_root / "scripts" / "run_tests.sh"), str(probe), "-q"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    child_pid = int(handoff.read_text(encoding="utf-8"))
    try:
        assert proc.returncode == 0, proc.stdout
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
            time.sleep(0.05)
        assert not Path(f"/proc/{child_pid}").exists(), proc.stdout
    finally:
        if Path(f"/proc/{child_pid}").exists():
            os.kill(child_pid, signal.SIGKILL)

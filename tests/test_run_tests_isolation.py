"""Safety contracts for the canonical suite runner boundary."""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_RUNNER = REPO_ROOT / "scripts" / "run_tests.sh"
PINNED_PYTHON = Path("/opt/data/forwardport/v0205/control/test-env-c5b0cfd9/bin/python")


def _isolation_module():
    return importlib.import_module("scripts.test_suite_isolation")


def test_suite_home_rejects_live_and_nested_candidates(tmp_path: Path) -> None:
    isolation = _isolation_module()
    live = tmp_path / "operator-home"
    live.mkdir()

    with pytest.raises(ValueError, match="forbidden"):
        isolation.validate_suite_home(live, [live])
    nested = live / "nested-test-home"
    nested.mkdir()
    with pytest.raises(ValueError, match="forbidden"):
        isolation.validate_suite_home(nested, [live])


def test_cleanup_refuses_changed_ownership_marker(tmp_path: Path) -> None:
    isolation = _isolation_module()
    suite_home, token = isolation.create_suite_home(tmp_path, [])
    marker = suite_home / isolation.OWNER_MARKER
    marker.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ownership marker"):
        isolation.cleanup_suite_home(suite_home, token, [])
    assert suite_home.is_dir()


def test_owner_marker_does_not_disclose_cleanup_authority(tmp_path: Path) -> None:
    isolation = _isolation_module()
    suite_home, token = isolation.create_suite_home(tmp_path, [])
    marker = suite_home / isolation.OWNER_MARKER
    marker_value = marker.read_text(encoding="utf-8").strip()

    assert token not in marker_value
    with pytest.raises(RuntimeError, match="ownership marker"):
        isolation.cleanup_suite_home(suite_home, marker_value, [])
    assert suite_home.is_dir()
    isolation.cleanup_suite_home(suite_home, token, [])


def test_cleanup_refuses_empty_authority(tmp_path: Path) -> None:
    isolation = _isolation_module()
    suite_home, _ = isolation.create_suite_home(tmp_path, [])

    with pytest.raises(RuntimeError, match="ownership marker"):
        isolation.cleanup_suite_home(suite_home, "", [])
    assert suite_home.is_dir()


def test_service_control_classifier_blocks_all_real_managers() -> None:
    isolation = _isolation_module()
    blocked = [
        ["/command/s6-svc", "-d", "/run/service/gateway-default"],
        ["systemctl", "--user", "restart", "hermes-gateway"],
        ["launchctl", "kickstart", "gui/501/ai.hermes.gateway"],
        ["sc.exe", "stop", "HermesGateway"],
        ["taskkill", "/PID", "1", "/F"],
    ]
    for command in blocked:
        assert isolation.is_real_service_control(command), command
    assert not isolation.is_real_service_control(["python", "-m", "pytest"])


def _run_canonical_probe(
    tmp_path: Path,
    source: str,
    *,
    python: Path = PINNED_PYTHON,
) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_process_domain_probe.py"
    probe.write_text(textwrap.dedent(source), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "HERMES_PYTHON": str(python),
            "HERMES_TEST_WORKERS": "1",
            "HERMES_TEST_FILE_RETRIES": "0",
        }
    )
    return subprocess.run(
        [str(CANONICAL_RUNNER), str(probe), "-q", "--file-timeout", "30"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _python_with_forced_suite_token(tmp_path: Path, token: str) -> Path:
    """Return a Python shim that controls only suite-allocation output."""
    wrapper = tmp_path / "python-with-forced-suite-token"
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!{PINNED_PYTHON}
            import os
            import sys

            real_python = {str(PINNED_PYTHON)!r}
            if len(sys.argv) >= 3 and sys.argv[1].endswith("test_suite_isolation.py") and sys.argv[2] == "create":
                from scripts import test_suite_isolation as isolation

                base = None
                forbidden = []
                args = iter(sys.argv[3:])
                for argument in args:
                    value = next(args)
                    if argument == "--base":
                        base = value
                    elif argument == "--forbid":
                        forbidden.append(value)
                    else:
                        raise AssertionError(argument)
                isolation.secrets.token_urlsafe = lambda _size: {token!r}
                suite_home, _ = isolation.create_suite_home(base, forbidden)
                print(f"{{suite_home}}\\t" + {token!r})
                raise SystemExit(0)
            os.execv(real_python, [real_python, *sys.argv[1:]])
            """
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def test_nested_canonical_runner_cleans_leading_dash_owner_token(
    tmp_path: Path,
) -> None:
    forced_token = "-deterministic-owner-token"
    python = _python_with_forced_suite_token(tmp_path, forced_token)
    observed = tmp_path / "inner-home"
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        from pathlib import Path

        def test_nested_success():
            inner_home = Path(os.environ["HERMES_HOME"])
            Path({str(observed)!r}).write_text(str(inner_home), encoding="utf-8")
            assert "SUITE_OWNER_TOKEN" not in os.environ
            assert os.environ["HERMES_TEST_ISOLATION"] == str(inner_home)
            marker = (inner_home / ".hermes-test-suite-owner").read_text(encoding="utf-8").strip()
            assert marker != {forced_token!r}
        """,
        python=python,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "arguments are required: token" not in proc.stderr
    inner_home = Path(observed.read_text(encoding="utf-8"))
    from tests.conftest import _PRE_SANDBOX_HERMES_HOME

    assert _PRE_SANDBOX_HERMES_HOME is not None
    outer_home = Path(_PRE_SANDBOX_HERMES_HOME)
    assert inner_home != outer_home
    assert not inner_home.exists()
    assert (outer_home / _isolation_module().OWNER_MARKER).is_file()


def test_nested_canonical_runner_cleans_owned_home_after_test_failure(
    tmp_path: Path,
) -> None:
    observed = tmp_path / "failed-inner-home"
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        from pathlib import Path

        HOME = Path(os.environ["HERMES_HOME"])
        Path({str(observed)!r}).write_text(str(HOME), encoding="utf-8")

        def test_nested_failure():
            assert False, "intentional nested failure"
        """,
    )

    assert proc.returncode != 0
    assert "intentional nested failure" in proc.stdout
    assert not Path(observed.read_text(encoding="utf-8")).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal contract")
def test_nested_canonical_runner_cleans_owned_home_after_signal(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_signal_probe.py"
    probe.write_text("def test_wait():\n    pass\n", encoding="utf-8")
    log_path = tmp_path / "nested-signal.log"
    env = os.environ.copy()
    env.update(
        {
            "HERMES_PYTHON": str(PINNED_PYTHON),
            "HERMES_TEST_WORKERS": "1",
            "HERMES_TEST_FILE_RETRIES": "0",
        }
    )
    with log_path.open("w", encoding="utf-8") as log:
        runner = subprocess.Popen(
            [str(CANONICAL_RUNNER), str(probe), "-q"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        suite_home = None
        output = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            output = log_path.read_text(encoding="utf-8")
            for line in output.splitlines():
                prefix = "  (suite HERMES_HOME="
                if line.startswith(prefix):
                    suite_home = Path(line.removeprefix(prefix).split(";", 1)[0])
                    break
            if suite_home is not None:
                break
            if runner.poll() is not None:
                break
            time.sleep(0.01)
        assert suite_home is not None, output
        os.kill(runner.pid, signal.SIGTERM)
        assert runner.wait(timeout=30) != 0

    assert not suite_home.exists(), log_path.read_text(encoding="utf-8")


@pytest.mark.linux_only
def test_canonical_process_domain_allows_registered_reparented_descendant(
    tmp_path: Path,
) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        """
        import os
        import signal
        import subprocess
        import sys
        import time

        def test_registered_direct_child_can_be_signalled():
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            os.kill(child.pid, signal.SIGTERM)
            assert child.wait(timeout=10) != 0

        def test_registered_reparented_descendant_can_be_signalled():
            child_source = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "[time.sleep(0.1) for _ in iter(int, 1)]"
            )
            parent_source = (
                "import subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
                "print(child.pid,flush=True)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_source, child_source],
                stdout=subprocess.PIPE,
                text=True,
            )
            descendant_pid = int(parent.stdout.readline())
            assert parent.wait(timeout=10) == 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    status = open(f"/proc/{descendant_pid}/status", encoding="utf-8").read()
                except FileNotFoundError:
                    break
                ppid = next(
                    int(line.split()[1]) for line in status.splitlines()
                    if line.startswith("PPid:")
                )
                if ppid != parent.pid:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("descendant was not reparented")

            os.kill(descendant_pid, signal.SIGKILL)
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="process-group guard is Linux-only")
def test_canonical_process_domain_allows_owned_shell_group_probe_and_termination(
    tmp_path: Path,
) -> None:
    """A registered shell leader owns its non-Python group descendants."""
    proc = _run_canonical_probe(
        tmp_path,
        """
        import os
        import signal
        import subprocess

        def test_owned_shell_group_can_be_probed_and_terminated():
            shell = subprocess.Popen(
                ["/bin/sh", "-c", "sleep 30 & echo $!; wait"],
                start_new_session=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            descendant_pid = int(shell.stdout.readline())
            pgid = os.getpgid(shell.pid)
            assert descendant_pid != shell.pid
            assert os.getpgid(descendant_pid) == pgid
            os.killpg(pgid, 0)
            os.killpg(pgid, signal.SIGTERM)
            assert shell.wait(timeout=10) != 0
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_canonical_group_signal_zero_preserves_native_results_without_authority(
    tmp_path: Path,
) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        import runpy
        import signal
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})

        def test_signal_zero_delegates_before_group_authentication(monkeypatch):
            native_calls = []

            def fake_killpg(pgid, sig):
                native_calls.append((pgid, sig))
                if pgid == 51002:
                    raise ProcessLookupError(pgid)
                if pgid == 51003:
                    raise PermissionError(pgid)
                return None

            original_guard = os.killpg
            monkeypatch.setattr(os, "killpg", fake_killpg)
            namespace = runpy.run_path(str(SITE_GUARD))
            nested_guard = namespace["_guarded_killpg"]
            guard_globals = nested_guard.__globals__
            monkeypatch.setattr(os, "killpg", original_guard)
            guard_globals["_authenticate_group_members"] = lambda pgid: (_ for _ in ()).throw(
                AssertionError("signal-0 probe attempted destructive authentication")
            )

            assert nested_guard(51001, 0) is None
            with pytest.raises(ProcessLookupError):
                nested_guard(51002, 0)
            with pytest.raises(PermissionError):
                nested_guard(51003, 0)

            guard_globals["_authenticate_group_members"] = lambda pgid: None
            with pytest.raises(RuntimeError, match="live/foreign process group"):
                nested_guard(51001, signal.SIGTERM)
            assert native_calls == [(51001, 0), (51002, 0), (51003, 0)]
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group contract")
def test_foreign_group_probe_and_forged_registration_never_confer_authority(
    tmp_path: Path,
) -> None:
    sibling = subprocess.Popen(
        [str(PINNED_PYTHON), "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        proc = _run_canonical_probe(
            tmp_path,
            f"""
            import os
            import signal
            import sitecustomize

            import pytest

            FOREIGN_PGID = {sibling.pid}

            def test_foreign_group_probe_does_not_authorize_later_delivery():
                os.killpg(FOREIGN_PGID, 0)
                register = getattr(sitecustomize, "_register_owned_pid", None)
                if register is not None:
                    register(FOREIGN_PGID)
                os.killpg(FOREIGN_PGID, 0)
                with pytest.raises(RuntimeError, match="live/foreign process group"):
                    os.killpg(FOREIGN_PGID, signal.SIGTERM)
            """,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert sibling.poll() is None, "probe or forged registration signalled sibling group"
    finally:
        if sibling.poll() is None:
            sibling.kill()
        sibling.wait(timeout=10)


def test_canonical_process_domain_denies_foreign_live_pid_without_delivery(
    tmp_path: Path,
) -> None:
    from gateway.status import get_running_pid
    from tests.conftest import _PRE_SANDBOX_HERMES_HOME

    if not _PRE_SANDBOX_HERMES_HOME:
        pytest.skip("no pre-sandbox live Hermes home is available")
    live_pid_path = Path(_PRE_SANDBOX_HERMES_HOME) / "gateway.pid"
    gateway_pid_before = get_running_pid(
        pid_path=live_pid_path,
        cleanup_stale=False,
    )
    if gateway_pid_before is None:
        pytest.skip("no live gateway PID is available for the read-only denial probe")
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        import runpy
        import signal
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})
        LIVE_GATEWAY_PID = {gateway_pid_before}

        def test_live_gateway_pid_is_denied_before_signal_delivery(monkeypatch):
            delivered = []

            def fake_kill(pid, sig, *args, **kwargs):
                delivered.append((pid, sig))

            original_guard = os.kill
            monkeypatch.setattr(os, "kill", fake_kill)
            runpy.run_path(str(SITE_GUARD))
            nested_guard = os.kill
            monkeypatch.setattr(os, "kill", original_guard)

            with pytest.raises(RuntimeError, match="live/foreign PID"):
                nested_guard(LIVE_GATEWAY_PID, signal.SIGTERM)
            assert delivered == []
        """,
    )

    gateway_pid_after = get_running_pid(
        pid_path=live_pid_path,
        cleanup_stale=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert gateway_pid_after == gateway_pid_before


def test_canonical_process_domain_denies_foreign_live_group_without_delivery(
    tmp_path: Path,
) -> None:
    from gateway.status import get_running_pid
    from tests.conftest import _PRE_SANDBOX_HERMES_HOME

    if not _PRE_SANDBOX_HERMES_HOME:
        pytest.skip("no pre-sandbox live Hermes home is available")
    live_pid_path = Path(_PRE_SANDBOX_HERMES_HOME) / "gateway.pid"
    gateway_pid_before = get_running_pid(pid_path=live_pid_path, cleanup_stale=False)
    if gateway_pid_before is None:
        pytest.skip("no live gateway PID is available for the read-only denial probe")
    assert gateway_pid_before is not None
    gateway_pgid = os.getpgid(gateway_pid_before)
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        import runpy
        import signal
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})
        LIVE_GATEWAY_PGID = {gateway_pgid}

        def test_live_gateway_group_is_denied_before_signal_delivery(monkeypatch):
            delivered = []

            def fake_killpg(pgid, sig, *args, **kwargs):
                delivered.append((pgid, sig))

            original_guard = os.killpg
            monkeypatch.setattr(os, "killpg", fake_killpg)
            runpy.run_path(str(SITE_GUARD))
            nested_guard = os.killpg
            monkeypatch.setattr(os, "killpg", original_guard)

            with pytest.raises(RuntimeError, match="live/foreign process group"):
                nested_guard(LIVE_GATEWAY_PGID, signal.SIGTERM)
            assert delivered == []
        """,
    )

    gateway_pid_after = get_running_pid(pid_path=live_pid_path, cleanup_stale=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert gateway_pid_after == gateway_pid_before


@pytest.mark.skipif(sys.platform != "linux", reason="Linux ancestry/subreaper contract")
def test_forged_registration_cannot_confer_cleanup_ownership(tmp_path: Path) -> None:
    """Test-held registry authority cannot make a disposable sibling signalable."""
    sibling = subprocess.Popen([str(PINNED_PYTHON), "-c", "import time; time.sleep(60)"])
    try:
        proc = _run_canonical_probe(
            tmp_path,
            f"""
            import os
            import signal
            import sitecustomize

            import pytest

            FOREIGN_SIBLING = {sibling.pid}

            def test_forged_registration_is_not_signal_authority():
                register = getattr(sitecustomize, "_register_owned_pid", None)
                if register is not None:
                    register(FOREIGN_SIBLING)
                with pytest.raises(RuntimeError, match="live/foreign PID"):
                    os.kill(FOREIGN_SIBLING, signal.SIGTERM)
            """,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert sibling.poll() is None, "forged registration killed an unrelated sibling"
    finally:
        if sibling.poll() is None:
            sibling.kill()
        sibling.wait(timeout=10)


def test_portable_pid_guard_preserves_esrch_and_rejects_changed_identity(
    tmp_path: Path,
) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import runpy
        import signal
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})

        def _load_guard():
            namespace = runpy.run_path(str(SITE_GUARD))
            return namespace["_guarded_kill"].__globals__

        def test_absent_pid_preserves_native_esrch_semantics():
            guard = _load_guard()
            guard["_process_snapshot"] = lambda pid: None
            guard["_belongs_to_guard_tree"] = lambda pid: False

            with pytest.raises(ProcessLookupError):
                guard["_signal_pid_identity"](51001, "gone-start", signal.SIGTERM)
            with pytest.raises(ProcessLookupError):
                guard["_guarded_kill"](51001, signal.SIGTERM)

        def test_present_mismatched_or_foreign_pid_still_fails_closed():
            guard = _load_guard()
            guard["_process_snapshot"] = lambda pid: ("changed-start", 1, 1)
            guard["_belongs_to_guard_tree"] = lambda pid: False

            with pytest.raises(RuntimeError, match="changed process identity"):
                guard["_signal_pid_identity"](51002, "expected-start", signal.SIGTERM)
            with pytest.raises(RuntimeError, match="live/foreign PID"):
                guard["_guarded_kill"](51002, signal.SIGTERM)
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr



def test_portable_guard_prohibits_unprovable_session_detach(tmp_path: Path) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import runpy
        import subprocess
        import sys
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})

        def test_darwin_detach_is_denied_before_launch(monkeypatch):
            monkeypatch.setattr(sys, "platform", "darwin")
            namespace = runpy.run_path(str(SITE_GUARD))
            guarded_popen = namespace["_GuardedPopen"]
            with pytest.raises(RuntimeError, match="detachment"):
                guarded_popen(["/definitely/not/a/command"], start_new_session=True)
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="process-group guard is Linux-only")
def test_canonical_process_group_identity_and_membership_races_fail_closed(
    tmp_path: Path,
) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        f"""
        import os
        import runpy
        import secrets
        import signal
        from pathlib import Path

        import pytest

        SITE_GUARD = Path({str(REPO_ROOT / "scripts" / "test_site" / "sitecustomize.py")!r})

        def _load_guard(monkeypatch):
            token = secrets.token_hex(32)
            domain = Path(os.environ["HERMES_TEST_ISOLATION"]) / f"race-domain-{{token}}"
            domain.touch(mode=0o600)
            monkeypatch.setenv("HERMES_TEST_PROCESS_DOMAIN", str(domain))
            monkeypatch.setenv("HERMES_TEST_PROCESS_DOMAIN_TOKEN", token)
            delivered = []
            monkeypatch.setattr(os, "killpg", lambda pgid, sig: delivered.append((pgid, sig)))
            namespace = runpy.run_path(str(SITE_GUARD))
            guard_globals = namespace["_guarded_killpg"].__globals__
            return guard_globals, delivered

        def test_owned_group_probe_is_non_delivery_and_termination_is_per_pid(monkeypatch):
            guard, delivered = _load_guard(monkeypatch)
            pgid = 41000
            guard["_authenticate_group_members"] = lambda target: {{
                target: "leader-start",
                target + 1: "child-start",
            }}
            guard["_signal_pid_identity"] = (
                lambda pid, start, sig: delivered.append((pid, start, sig))
            )

            guard["_guarded_killpg"](pgid, 0)
            assert delivered == [(pgid, 0)]
            guard["_guarded_killpg"](pgid, signal.SIGTERM)
            assert delivered == [
                (pgid, 0),
                (pgid, "leader-start", signal.SIGTERM),
                (pgid + 1, "child-start", signal.SIGTERM),
            ]

        def test_exited_leader_pgid_reuse_is_denied(monkeypatch):
            guard, delivered = _load_guard(monkeypatch)
            pgid = 41001
            guard["_owned_process_groups"][pgid] = "old-leader-start"
            guard["_group_snapshots"] = lambda target: {{
                pgid: ("reused-leader-start", 1, target)
            }}
            with pytest.raises(RuntimeError, match="live/foreign process group"):
                guard["_guarded_killpg"](pgid, signal.SIGTERM)
            assert delivered == []

        def test_unregistered_injected_group_member_denies_all_delivery(monkeypatch):
            guard, delivered = _load_guard(monkeypatch)
            pgid = 41002
            guard["_owned_process_groups"][pgid] = "leader-start"
            guard["_group_snapshots"] = lambda target: {{
                pgid: ("leader-start", 1, target),
                pgid + 1: ("injected-start", 1, target),
            }}
            guard["_registered_identity_status"] = (
                lambda pid, *args: "owned" if pid == pgid else "unregistered"
            )
            guard["_stable_descendant_of"] = lambda *args: False
            guard["_signal_pid_identity"] = lambda *args: delivered.append(args)
            with pytest.raises(RuntimeError, match="live/foreign process group"):
                guard["_guarded_killpg"](pgid, signal.SIGKILL)
            assert delivered == []

        def test_descendant_ancestry_change_during_authentication_is_denied(monkeypatch):
            guard, _ = _load_guard(monkeypatch)
            pgid = 41003
            observations = {{
                pgid + 1: [
                    ("child-start", pgid, pgid),
                    ("child-start", 1, pgid),
                ],
                pgid: [
                    ("leader-start", 1, pgid),
                    ("leader-start", 1, pgid),
                ],
            }}

            def changing_snapshot(pid):
                values = observations[pid]
                return values.pop(0) if len(values) > 1 else values[0]

            guard["_process_snapshot"] = changing_snapshot
            assert guard["_stable_descendant_of"](pgid + 1, pgid, pgid) is False
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.linux_only
def test_canonical_process_domain_uses_ancestry_not_forged_marker(
    tmp_path: Path,
) -> None:
    proc = _run_canonical_probe(
        tmp_path,
        """
        import hashlib
        import hmac
        import json
        import os
        import signal
        import subprocess
        import sys
        from pathlib import Path

        import pytest

        def _spawn_reparented_descendant():
            child_source = "import time; [time.sleep(0.1) for _ in iter(int, 1)]"
            parent_source = (
                "import subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
                "print(child.pid,flush=True)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_source, child_source],
                stdout=subprocess.PIPE,
                text=True,
            )
            descendant_pid = int(parent.stdout.readline())
            assert parent.wait(timeout=10) == 0
            return descendant_pid

        def _start_identity(pid):
            fields = open(f"/proc/{pid}/stat", encoding="ascii").read().rsplit(")", 1)[1].split()
            return fields[19]

        def test_runtime_marker_forgery_cannot_change_ancestry_authority(monkeypatch):
            pid = _spawn_reparented_descendant()
            domain = os.environ["HERMES_TEST_PROCESS_DOMAIN"]
            assert "HERMES_TEST_PROCESS_DOMAIN_TOKEN" not in os.environ
            current_start = _start_identity(pid)
            real_read_text = Path.read_text

            def mismatched_proc_start(path, *args, **kwargs):
                raw = real_read_text(path, *args, **kwargs)
                if path == Path(f"/proc/{pid}/stat"):
                    head, tail = raw.rsplit(")", 1)
                    fields = tail.split()
                    fields[19] = str(int(current_start) + 1)
                    return head + ") " + " ".join(fields)
                return raw

            with monkeypatch.context() as mismatch:
                mismatch.setattr(Path, "read_text", mismatched_proc_start)
                os.kill(pid, 0)

            valid_domain_records = Path(domain).read_text(encoding="ascii")
            forged_token = "0" * 64
            forged_payload = f"{pid}:{current_start}".encode("ascii")
            forged_record = {
                "pid": pid,
                "start": current_start,
                "mac": hmac.new(
                    forged_token.encode("ascii"), forged_payload, hashlib.sha256
                ).hexdigest(),
            }
            try:
                open(domain, "w", encoding="ascii").write(
                    json.dumps(forged_record) + "\\n"
                )
                os.environ["HERMES_TEST_PROCESS_DOMAIN_TOKEN"] = forged_token
                os.kill(pid, 0)
            finally:
                Path(domain).write_text(valid_domain_records, encoding="ascii")
                os.kill(pid, signal.SIGKILL)
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_canonical_runner_isolates_collection_and_cleans_owned_home(
    tmp_path: Path,
) -> None:
    pretend_live = tmp_path / "pretend-live"
    pretend_live.mkdir()
    sentinel = pretend_live / "collection-sentinel"
    sentinel.write_text("operator-state\n", encoding="utf-8")
    observed = tmp_path / "observed-home"
    probe = tmp_path / "test_collection_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import os
            from pathlib import Path

            HOME = Path(os.environ["HERMES_HOME"])
            Path({str(observed)!r}).write_text(str(HOME), encoding="utf-8")
            (HOME / "collection-sentinel").write_text("test-state\\n", encoding="utf-8")

            def test_probe():
                assert os.environ["HERMES_TEST_ISOLATION"] == str(HOME)
                assert os.environ["HERMES_TEST_NO_SERVICE_CONTROL"] == "1"
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(pretend_live),
            "HERMES_PYTHON": str(PINNED_PYTHON),
            "HERMES_TEST_WORKERS": "1",
            "HERMES_TEST_FILE_RETRIES": "0",
        }
    )

    proc = subprocess.run(
        [str(CANONICAL_RUNNER), str(probe), "-q", "--file-timeout", "30"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sentinel.read_text(encoding="utf-8") == "operator-state\n"
    suite_home = Path(observed.read_text(encoding="utf-8"))
    assert suite_home != pretend_live
    assert pretend_live not in suite_home.parents
    assert not suite_home.exists(), "marker-owned suite home must be cleaned"


@pytest.mark.skipif(sys.platform != "linux", reason="hosted s6 canary is Linux-only")
def test_canonical_runner_hides_real_s6_from_collection(tmp_path: Path) -> None:
    """Collection sees an empty shadow, never the hosted /run/service tree."""
    if not Path("/run/service").is_dir():
        pytest.skip("host is not running the s6 layout")
    observed = tmp_path / "observed-s6"
    probe = tmp_path / "test_s6_collection_canary.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import os
            import signal
            import subprocess
            from pathlib import Path

            try:
                os.kill(2_147_483_647, signal.SIGTERM)
            except RuntimeError as exc:
                assert "canonical test guard blocked signal" in str(exc)
            else:
                raise AssertionError("collection-time PID guard was not installed")

            try:
                subprocess.run(
                    ["/command/s6-svc", "-d", "/run/service/gateway-default"],
                    check=False,
                )
            except RuntimeError as exc:
                assert "canonical test guard blocked" in str(exc)
            else:
                raise AssertionError("collection-time service guard was not installed")

            Path({str(observed)!r}).write_text(
                repr(sorted(path.name for path in Path("/run/service").iterdir())),
                encoding="utf-8",
            )
            def test_shadowed():
                assert not list(Path("/run/service").iterdir())
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"HERMES_PYTHON": str(PINNED_PYTHON), "HERMES_TEST_WORKERS": "1"})
    proc = subprocess.run(
        [str(CANONICAL_RUNNER), str(probe), "-q", "--file-timeout", "30"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert observed.read_text(encoding="utf-8") == "[]"

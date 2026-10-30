"""Python-startup guard inherited by every canonical test descendant."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


if os.environ.get("HERMES_TEST_NO_SERVICE_CONTROL") == "1" and os.environ.get(
    "HERMES_TEST_CONTROL_PLANE_LANE"
) not in {"fake", "disposable"}:
    scripts_dir = Path(__file__).resolve().parent.parent
    if str(scripts_dir.parent) not in sys.path:
        sys.path.insert(0, str(scripts_dir.parent))
    from scripts.test_suite_isolation import is_real_service_control

    _real_popen = subprocess.Popen
    if sys.platform.startswith("linux"):
        # The nearest living test process adopts orphaned descendants; when it
        # exits, the outer runner subreaper adopts them in turn.  This creates
        # kernel ancestry evidence that test-held registry data cannot forge.
        try:
            import ctypes

            _libc = ctypes.CDLL(None, use_errno=True)
            if _libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
        except (AttributeError, OSError) as exc:
            raise RuntimeError(
                "canonical Linux test isolation requires child-subreaper containment"
            ) from exc
    # pgid -> kernel start identity of the group leader. A bare PGID is not
    # ownership evidence because an unrelated future leader can reuse it.
    _owned_process_groups = {}

    _domain_path = os.environ.get("HERMES_TEST_PROCESS_DOMAIN", "")
    _domain_token = os.environ.get("HERMES_TEST_PROCESS_DOMAIN_TOKEN", "")
    _domain_stat = None
    try:
        _isolation_root = Path(os.environ["HERMES_TEST_ISOLATION"]).resolve()
        _resolved_domain = Path(_domain_path).resolve(strict=True)
        _candidate_stat = _resolved_domain.stat()
        if (
            _resolved_domain.parent == _isolation_root
            and stat.S_ISREG(_candidate_stat.st_mode)
            and not (_candidate_stat.st_mode & 0o077)
            and len(_domain_token) == 64
            and all(character in "0123456789abcdef" for character in _domain_token)
            and (
                not hasattr(os, "getuid")
                or _candidate_stat.st_uid == os.getuid()  # windows-footgun: ok
            )
        ):
            _domain_path = str(_resolved_domain)
            _domain_stat = (_candidate_stat.st_dev, _candidate_stat.st_ino)
        else:
            _domain_path = ""
    except (KeyError, OSError, ValueError):
        _domain_path = ""
    finally:
        # The runner-issued authenticator is startup authority, not a public
        # marker tests may opt into.  Descendant launches receive it from this
        # closure and their own startup guard removes it before user code runs.
        os.environ.pop("HERMES_TEST_PROCESS_DOMAIN_TOKEN", None)

    def _process_start_identity(pid):
        """Return a kernel-backed process identity, never PID alone."""
        if pid <= 0:
            return None
        if sys.platform.startswith("linux"):
            try:
                fields = (
                    Path(f"/proc/{pid}/stat")
                    .read_text(encoding="ascii")
                    .rsplit(")", 1)[1]
                    .split()
                )
                return fields[19]
            except (OSError, IndexError):
                return None
        try:
            import psutil

            return repr(psutil.Process(pid).create_time())
        except Exception:
            return None

    def _process_snapshot(pid):
        """Return one kernel snapshot of ``(start, ppid, pgid)``, or ``None``."""
        if int(pid) <= 0:
            return None
        if sys.platform.startswith("linux"):
            try:
                fields = (
                    Path(f"/proc/{int(pid)}/stat")
                    .read_text(encoding="ascii")
                    .rsplit(")", 1)[1]
                    .split()
                )
                return fields[19], int(fields[1]), int(fields[2])
            except (OSError, IndexError, ValueError):
                return None
        try:
            import psutil

            process = psutil.Process(int(pid))
            return repr(process.create_time()), process.ppid(), os.getpgid(int(pid))
        except Exception:
            return None

    def _domain_signature(pid, start_identity, token=None):
        payload = f"{pid}:{start_identity}".encode("ascii")
        signing_token = _domain_token if token is None else token
        return hmac.new(signing_token.encode("ascii"), payload, hashlib.sha256).hexdigest()

    def _domain_authority_from_values(path, token):
        try:
            resolved = Path(path).resolve(strict=True)
            candidate = resolved.stat()
            if (
                resolved.parent == _isolation_root
                and stat.S_ISREG(candidate.st_mode)
                and not (candidate.st_mode & 0o077)
                and len(token) == 64
                and all(character in "0123456789abcdef" for character in token)
                and (not hasattr(os, "getuid") or candidate.st_uid == os.getuid())
            ):
                return str(resolved), token, (candidate.st_dev, candidate.st_ino)
        except (OSError, TypeError, ValueError):
            pass
        return None

    _domain_authority = (
        (_domain_path, _domain_token, _domain_stat) if _domain_path and _domain_stat else None
    )

    def _open_domain(flags, authority=None):
        selected = _domain_authority if authority is None else authority
        if selected is None:
            return None
        domain_path, _token, domain_stat = selected
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(domain_path, flags | nofollow)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != domain_stat or not stat.S_ISREG(opened.st_mode):
                os.close(fd)
                return None
            return fd
        except OSError:
            return None

    def _register_owned_pid(pid, authority=None):
        selected = _domain_authority if authority is None else authority
        if selected is None:
            return False
        start_identity = _process_start_identity(int(pid))
        if start_identity is None:
            return False
        record = {
            "pid": int(pid),
            "start": start_identity,
            "mac": _domain_signature(int(pid), start_identity, selected[1]),
        }
        fd = _open_domain(os.O_WRONLY | os.O_APPEND, selected)
        if fd is None:
            return False
        try:
            encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode(
                "ascii"
            )
            os.write(fd, encoded)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def _registered_identity_status(pid, authority=None):
        """Return ``owned``, ``gone``, ``mismatch``, or ``unregistered``."""
        selected = _domain_authority if authority is None else authority
        if selected is None:
            return "unregistered"
        fd = _open_domain(os.O_RDONLY, selected)
        if fd is None:
            return "unregistered"
        try:
            data = os.read(fd, 1024 * 1024).decode("ascii")
        except (OSError, UnicodeDecodeError):
            return "unregistered"
        finally:
            os.close(fd)

        valid_starts = set()
        for line in data.splitlines():
            try:
                record = json.loads(line)
                record_pid = record["pid"]
                record_start = record["start"]
                record_mac = record["mac"]
                if (
                    type(record_pid) is int
                    and record_pid == int(pid)
                    and type(record_start) is str
                    and type(record_mac) is str
                    and set(record) == {"pid", "start", "mac"}
                    and hmac.compare_digest(
                        record_mac,
                        _domain_signature(record_pid, record_start, selected[1]),
                    )
                ):
                    valid_starts.add(record_start)
            except (TypeError, ValueError, KeyError):
                continue
        if not valid_starts:
            return "unregistered"
        current = _process_start_identity(int(pid))
        if current is None:
            return "gone"
        if current in valid_starts:
            return "owned"
        return "mismatch"

    def _signal_pid_identity(pid, expected_start, sig, *args, **kwargs):
        """Deliver only to the pidfd bound to ``pid`` with ``expected_start``."""
        if sys.platform.startswith("linux"):
            try:
                import ctypes

                libc = ctypes.CDLL(None, use_errno=True)
                pidfd = libc.pidfd_open(int(pid), 0)
                if pidfd < 0:
                    error = ctypes.get_errno()
                    if error == errno.ESRCH:
                        raise ProcessLookupError(pid)
                    raise OSError(error, os.strerror(error))
                try:
                    # Bind first, then re-check PID+start. If reuse happened
                    # before pidfd_open, the identity check rejects it; if the
                    # process exits afterward, the pidfd cannot retarget.
                    snapshot = _process_snapshot(pid)
                    if snapshot is None:
                        raise ProcessLookupError(pid)
                    if snapshot[0] != expected_start:
                        raise RuntimeError(
                            "canonical test guard refused changed process identity"
                        )
                    result = libc.pidfd_send_signal(pidfd, int(sig), None, 0)
                    if result < 0:
                        error = ctypes.get_errno()
                        if error == errno.ESRCH:
                            raise ProcessLookupError(pid)
                        raise OSError(error, os.strerror(error))
                    return None
                finally:
                    os.close(pidfd)
            except AttributeError:
                # An older libc without pidfd support cannot provide the
                # no-reuse guarantee, so fail closed instead of using kill(2).
                raise RuntimeError(
                    "canonical test guard requires pidfd signal delivery on Linux"
                )
        snapshot = _process_snapshot(pid)
        if snapshot is None:
            raise ProcessLookupError(pid)
        if snapshot[0] != expected_start:
            raise RuntimeError("canonical test guard refused changed process identity")
        return _real_kill(pid, sig, *args, **kwargs)

    def _signal_registered_pid(pid, sig, *args, **kwargs):
        """Signal the registered identity currently bound to *pid*."""
        if _registered_identity_status(pid) != "owned":
            raise RuntimeError("canonical test guard refused changed process identity")
        start_identity = _process_start_identity(int(pid))
        if start_identity is None:
            raise ProcessLookupError(pid)
        return _signal_pid_identity(pid, start_identity, sig, *args, **kwargs)

    class _GuardedPopen(_real_popen):
        def __init__(self, command, *args, **kwargs):
            if is_real_service_control(command):
                raise RuntimeError(
                    "canonical test guard blocked real service-manager/control-plane "
                    f"command: {command!r}; use fakes/fixtures or the explicit "
                    "fake/disposable control-plane integration lane"
                )
            starts_session = bool(kwargs.get("start_new_session"))
            creationflags = int(kwargs.get("creationflags", 0))
            portable_detach_flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            )
            if not sys.platform.startswith("linux") and (
                starts_session or creationflags & portable_detach_flags
            ):
                raise RuntimeError(
                    "canonical test guard blocked unprovable process detachment"
                )
            requested_env = kwargs.get("env")
            requested_authority = None
            if requested_env is not None:
                requested_authority = _domain_authority_from_values(
                    requested_env.get("HERMES_TEST_PROCESS_DOMAIN", ""),
                    requested_env.get("HERMES_TEST_PROCESS_DOMAIN_TOKEN", ""),
                )
            if _domain_path and requested_authority is None:
                child_env = dict(
                    os.environ if requested_env is None else requested_env
                )
                child_env["HERMES_TEST_PROCESS_DOMAIN"] = _domain_path
                child_env["HERMES_TEST_PROCESS_DOMAIN_TOKEN"] = _domain_token
                child_env["HERMES_TEST_ISOLATION"] = str(_isolation_root)
                child_env["HERMES_TEST_NO_SERVICE_CONTROL"] = "1"
                kwargs["env"] = child_env
            spawn_env = os.environ if kwargs.get("env") is None else kwargs["env"]
            spawn_authority = _domain_authority_from_values(
                spawn_env.get("HERMES_TEST_PROCESS_DOMAIN", ""),
                spawn_env.get("HERMES_TEST_PROCESS_DOMAIN_TOKEN", ""),
            )
            super().__init__(command, *args, **kwargs)
            _register_owned_pid(self.pid)
            if starts_session and sys.platform != "win32":
                try:
                    pgid = os.getpgid(self.pid)
                    leader_start = _process_start_identity(self.pid)
                    if pgid == self.pid and leader_start is not None:
                        _owned_process_groups[pgid] = (leader_start, spawn_authority)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    subprocess.Popen = _GuardedPopen

    _real_system = os.system
    _real_popen_os = os.popen

    def _guarded_system(command):
        if is_real_service_control(command):
            raise RuntimeError(f"canonical test guard blocked service control: {command!r}")
        return _real_system(command)

    def _guarded_os_popen(command, *args, **kwargs):
        if is_real_service_control(command):
            raise RuntimeError(f"canonical test guard blocked service control: {command!r}")
        return _real_popen_os(command, *args, **kwargs)

    os.system = _guarded_system
    os.popen = _guarded_os_popen

    _real_kill = os.kill
    _real_killpg = getattr(os, "killpg", None)
    _guard_root_pid = os.getpid()

    def _belongs_to_guard_tree(pid):
        if pid == _guard_root_pid:
            return True
        if pid <= 0:
            return False
        if not sys.platform.startswith("linux"):
            try:
                import psutil

                owned = any(
                    parent.pid == _guard_root_pid
                    for parent in psutil.Process(pid).parents()
                )
                return owned
            except Exception:
                return False
        seen = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            try:
                status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
                parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
                pid = int(parent_line.split()[1])
            except (OSError, StopIteration, ValueError):
                return False
            if pid == _guard_root_pid:
                return True
        return False

    def _guarded_kill(pid, signal, *args, **kwargs):
        pid = int(pid)
        if int(signal) == 0:
            return _real_kill(pid, signal, *args, **kwargs)
        if _process_snapshot(pid) is None:
            raise ProcessLookupError(pid)
        if _belongs_to_guard_tree(pid):
            if pid == _guard_root_pid:
                return _real_kill(pid, signal, *args, **kwargs)
            start_identity = _process_start_identity(pid)
            if start_identity is None:
                raise ProcessLookupError(pid)
            return _signal_pid_identity(pid, start_identity, signal, *args, **kwargs)
        raise RuntimeError(
            f"canonical test guard blocked signal {signal} to live/foreign PID {pid}"
        )

    def _group_snapshots(pgid):
        """Snapshot every current member without trusting a group-wide syscall."""
        candidate_pids = []
        if sys.platform.startswith("linux"):
            try:
                candidate_pids = [
                    int(entry.name)
                    for entry in Path("/proc").iterdir()
                    if entry.name.isdigit()
                ]
            except OSError:
                return {}
        elif sys.platform != "win32":
            try:
                import psutil

                candidate_pids = psutil.pids()
            except Exception:
                return {}
        members = {}
        for pid in candidate_pids:
            snapshot = _process_snapshot(pid)
            if snapshot is not None and snapshot[2] == int(pgid):
                members[pid] = snapshot
        return members

    def _stable_descendant_of(pid, leader_pid, pgid):
        """Prove a stable ancestry chain from ``pid`` to the owned leader."""
        observed = {}
        current = int(pid)
        seen = set()
        while current not in seen and current > 1:
            seen.add(current)
            snapshot = _process_snapshot(current)
            if snapshot is None or snapshot[2] != int(pgid):
                return False
            observed[current] = snapshot
            if current == int(leader_pid):
                break
            current = snapshot[1]
        else:
            return False
        if current != int(leader_pid):
            return False
        # Re-read the complete chain before granting ownership. A descendant
        # that exits/reparents while ancestry is inspected must fail closed.
        return all(_process_snapshot(member) == snapshot for member, snapshot in observed.items())

    def _stable_adopted_child(pid):
        snapshot = _process_snapshot(pid)
        return (
            sys.platform.startswith("linux")
            and snapshot is not None
            and snapshot[1] == _guard_root_pid
            and _process_snapshot(pid) == snapshot
        )

    def _authenticate_group_members(pgid):
        pgid = int(pgid)
        group_record = _owned_process_groups.get(pgid)
        if group_record is None:
            return None
        if isinstance(group_record, tuple):
            leader_start, _authority = group_record
        else:  # compatibility for direct safety probes
            leader_start, _authority = group_record, _domain_authority
        members = _group_snapshots(pgid)
        if not members:
            raise ProcessLookupError(pgid)

        leader = members.get(pgid)
        if leader is not None:
            if leader[0] != leader_start:
                return None
            leader_live = True
        else:
            # An exited leader is not authority. Surviving members are owned
            # only if the kernel reparented them directly to this subreaper.
            leader_live = False
            if any(not _stable_adopted_child(pid) for pid in members):
                return None

        expected = {}
        for pid, snapshot in members.items():
            if pid == pgid and leader_live:
                expected[pid] = snapshot[0]
                continue
            if leader_live and _stable_descendant_of(pid, pgid, pgid):
                expected[pid] = snapshot[0]
                continue
            if not leader_live and _stable_adopted_child(pid):
                expected[pid] = snapshot[0]
                continue
            else:
                return None

        # Close enumeration/ancestry races before delivery. Per-PID delivery
        # below binds each identity again with pidfd.
        current = _group_snapshots(pgid)
        if {pid: snap[0] for pid, snap in current.items()} != expected:
            return None
        return expected

    def _guarded_killpg(pgid, signal):
        if _real_killpg is None:
            raise RuntimeError("canonical test guard: os.killpg is unavailable")  # windows-footgun: ok
        pgid = int(pgid)
        if int(signal) == 0:
            # Signal 0 performs no delivery. Delegate directly so arbitrary
            # liveness probes preserve native success/ESRCH/EPERM semantics;
            # this path records no ownership and grants no later authority.
            return _real_killpg(pgid, signal)
        expected = _authenticate_group_members(pgid)
        if expected is None:
            raise RuntimeError(
                f"canonical test guard blocked signal {signal} to live/foreign process group {pgid}"
            )
        for pid, start_identity in expected.items():
            try:
                _signal_pid_identity(pid, start_identity, signal)
            except ProcessLookupError:
                continue
        return None

    os.kill = _guarded_kill
    if _real_killpg is not None:
        os.killpg = _guarded_killpg  # windows-footgun: ok

"""Fail-closed task workspace confinement for explicitly opted-in profiles."""
from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class WorkspaceConfinementError(PermissionError):
    """The configured task workspace cannot safely authorize an operation."""


def _dispatcher_workspace(_turn_task_id: str) -> Optional[Path]:
    """Return workspace from authenticated durable admission, never env.

    ``_turn_task_id`` isolates per-turn tool environments and is not the
    durable Kanban task id. The authenticated worker contract already binds
    this process to one task and workspace, so path authorization must use the
    contract rather than compare these unrelated identifier namespaces.
    """
    from agent.private_worker_mode import get_authenticated_worker_contract

    contract = get_authenticated_worker_contract()
    if not contract:
        return None
    workspace_raw = contract["workspace"]["path"]
    workspace = Path(os.path.expanduser(workspace_raw))
    if not workspace.is_absolute() or not workspace.is_dir():
        raise WorkspaceConfinementError(
            "active worker has an invalid task workspace"
        )
    try:
        return workspace.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceConfinementError(
            "active worker has an invalid task workspace"
        ) from exc


def _load_security_config() -> dict:
    from hermes_cli.config import load_config

    return load_config() or {}


def _active_profile_name() -> str:
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name()


def is_workspace_confinement_enabled() -> bool:
    try:
        from agent.private_worker_mode import get_authenticated_worker_contract

        contract = get_authenticated_worker_contract()
        return bool(contract and contract.get("workspace_confinement") is True)
    except Exception:
        if os.environ.get("HERMES_WORKER_ADMISSION_FD", "").strip():
            raise
        return False



def _ava_shared_read_roots() -> tuple[Path, ...]:
    """Chris (2026-10-11): workers must be able to READ the files they need.
    Read-only, never write. Owner-private profiles (Josh/Connor) only get their own areas."""
    home = Path.home()
    prof = ""
    try:
        prof = str(_active_profile_name() or "")
    except Exception:
        pass
    owner = None
    for o in ("josh", "connor"):
        if prof.startswith(o):
            owner = o
    if owner == "josh":
        cands = [home / ".hermes/profiles/joshgarcia/hook_outputs", home / ".hermes/kanban/boards/josh-home/workspaces"]
    elif owner == "connor":
        cands = [home / ".hermes/profiles/connor/hook_outputs", home / ".hermes/kanban/boards/connor-home/workspaces"]
    else:
        cands = [home / "projects", home / ".openclaw/workspace", home / ".hermes/kanban/boards",
                 home / ".hermes/skills", home / ".hermes/scripts", home / ".hermes/logs",
                 home / "Documents", home / "Downloads", home / "Desktop"]
    out = []
    for c in cands:
        try:
            r = c.resolve(strict=True)
            if r.is_dir():
                out.append(r)
        except OSError:
            pass
    return tuple(out)


def configured_read_only_roots() -> tuple[Path, ...]:
    base = _configured_read_only_roots_base()
    if not base and not _confinement_contract_present():
        return base
    return tuple(dict.fromkeys((*base, *_ava_shared_read_roots())))


def _confinement_contract_present() -> bool:
    try:
        from agent.private_worker_mode import get_authenticated_worker_contract
        return bool(get_authenticated_worker_contract())
    except Exception:
        return False

def _configured_read_only_roots_base() -> tuple[Path, ...]:
    """Return identity-pinned roots from trusted project/runtime provenance."""
    from agent.private_worker_mode import get_authenticated_worker_contract
    contract = get_authenticated_worker_contract()
    if not contract:
        return ()
    raw_roots = contract["workspace_read_only_roots"]
    identities = contract["workspace_read_only_root_identities"]
    if not isinstance(raw_roots, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_roots
    ):
        raise WorkspaceConfinementError(
            "workspace confinement read_only_roots must be a list of absolute paths"
        )
    if not isinstance(identities, list) or len(identities) != len(raw_roots):
        raise WorkspaceConfinementError(
            "workspace confinement read-only root identities are invalid"
        )
    workspace = Path(contract["workspace"]["path"]).resolve(strict=True)
    profile_home = Path(contract["profile_home"]).resolve(strict=True)
    board_path = Path(contract["board_database"]["path"]).resolve(strict=True)
    roots: list[Path] = []
    for raw, expected_identity in zip(raw_roots, identities):
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            raise WorkspaceConfinementError(
                "workspace confinement read_only_roots must contain only absolute paths"
            )
        try:
            root = Path(expanded).resolve(strict=True)
            info = root.stat()
        except OSError as exc:
            raise WorkspaceConfinementError(
                f"workspace confinement read-only root is unavailable: {raw}"
            ) from exc
        current_identity = {
            "path": str(root), "device": int(info.st_dev), "inode": int(info.st_ino)
        }
        if current_identity != expected_identity:
            raise WorkspaceConfinementError(
                f"workspace confinement read-only root identity changed: {raw}"
            )
        if not root.is_dir():
            raise WorkspaceConfinementError(
                f"workspace confinement read-only root is not a directory: {root}"
            )
        if not _is_trusted_read_only_root(
            root,
            workspace=workspace,
            profile_home=profile_home,
            board_path=board_path,
        ):
            raise WorkspaceConfinementError(
                f"workspace confinement read-only root is not trusted: {root}"
            )
        roots.append(root)
    return tuple(dict.fromkeys(roots))


def _contains_or_is_contained(path: Path, protected: Path) -> bool:
    """Return whether either canonical path contains the other."""
    try:
        path.relative_to(protected)
        return True
    except ValueError:
        pass
    try:
        protected.relative_to(path)
        return True
    except ValueError:
        return False


def _runtime_read_roots() -> tuple[Path, ...]:
    candidates = [Path("/usr"), Path(sys.prefix), Path(sys.base_prefix)]
    return tuple(
        dict.fromkeys(
            candidate.resolve()
            for candidate in candidates
            if candidate.is_absolute() and candidate.exists()
        )
    )


def _is_trusted_read_only_root(
    root: Path,
    *,
    workspace: Path,
    profile_home: Path,
    board_path: Path,
) -> bool:
    """Accept only runtime closure or an explicit external project root."""
    host_data = Path("/opt/data").resolve()
    protected = [profile_home, board_path, board_path.parent]
    raw_home = os.environ.get("HERMES_HOME", "").strip()
    if raw_home:
        protected.append(Path(raw_home).expanduser().resolve())
    user_home = Path.home().resolve()
    protected.extend(
        user_home / name
        for name in (".hermes", ".ssh", ".aws", ".config", ".gnupg", ".kube", ".docker")
    )
    if root == Path("/") or root == host_data or host_data.is_relative_to(root):
        return False
    if any(_contains_or_is_contained(root, item) for item in protected):
        return False
    # An ancestor exposes the workspace's siblings; a direct sibling is itself
    # an unrelated authority. The workspace remains independently writable.
    if workspace.is_relative_to(root) or root.parent == workspace.parent:
        return False
    for runtime_root in _runtime_read_roots():
        if root == runtime_root or root.is_relative_to(runtime_root):
            return True
    # Explicit project roots carry local provenance rather than accepting an
    # arbitrary directory merely because an untrusted profile listed it.
    return (root / ".git").is_dir() or (root / ".git").is_file()


def _validate_named_profile() -> str:
    profile = _active_profile_name()
    if profile in {"", "default", "custom", None}:
        raise WorkspaceConfinementError(
            "workspace confinement requires a valid named profile"
        )
    try:
        from hermes_cli.profiles import validate_profile_name

        validate_profile_name(profile)
    except Exception as exc:
        raise WorkspaceConfinementError(
            f"workspace confinement profile is invalid: {profile!r}"
        ) from exc
    return profile


def _authoritative_workspace(task_id: str) -> Optional[str]:
    """Return the immutable task/session workspace anchor.

    Never use the terminal environment's live cwd here: a confined command may
    successfully ``cd`` to a readable system directory, and that mutable cwd
    must not become the next file tool's authority.
    """
    from tools.file_tools import (
        _configured_terminal_cwd,
        _registered_task_cwd_override,
    )

    return _registered_task_cwd_override(task_id) or _configured_terminal_cwd()


def resolve_workspace(*, task_id: str = "default", workspace: Optional[str] = None) -> Optional[Path]:
    """Return the canonical workspace, or ``None`` when confinement is disabled."""
    dispatcher_root = _dispatcher_workspace(task_id)
    if dispatcher_root is not None:
        return dispatcher_root
    if not is_workspace_confinement_enabled():
        return None
    _validate_named_profile()
    raw = workspace if workspace is not None else _authoritative_workspace(task_id)
    if not raw or not os.path.isabs(os.path.expanduser(str(raw))):
        raise WorkspaceConfinementError(
            "workspace confinement requires an absolute authoritative task workspace"
        )
    root = Path(os.path.expanduser(str(raw))).resolve()
    if not root.is_dir():
        raise WorkspaceConfinementError(
            f"workspace confinement requires an existing directory: {root}"
        )
    return root


def resolve_confined_path(
    path: str | os.PathLike[str],
    *,
    task_id: str = "default",
    workspace: Optional[str] = None,
    access: str = "write",
) -> Path:
    """Resolve a path and enforce workspace plus scoped read-only authority."""
    root = resolve_workspace(task_id=task_id, workspace=workspace)
    expanded = Path(os.path.expanduser(os.fspath(path)))
    if root is None:
        return expanded.resolve()
    if access not in {"read", "write", "search", "patch"}:
        raise WorkspaceConfinementError(f"invalid confined path access mode: {access!r}")
    read_only_roots = configured_read_only_roots()
    candidate = expanded.resolve() if expanded.is_absolute() else (root / expanded).resolve()
    try:
        candidate.relative_to(root)
        return candidate
    except ValueError:
        pass
    for read_root in read_only_roots:
        try:
            candidate.relative_to(read_root)
        except ValueError:
            continue
        if access == "read":
            return candidate
        raise WorkspaceConfinementError(
            f"path is inside a configured read-only root: {path!r}"
        )
    raise WorkspaceConfinementError(
        (
            f"path resolves outside the task workspace: {path!r}"
            if os.environ.get("HERMES_KANBAN_TASK", "").strip()
            else f"path resolves outside the authoritative task workspace: {path!r}"
        )
    )


def read_confined_bytes(
    path: str | os.PathLike[str],
    *,
    task_id: str = "default",
    max_bytes: int | None = None,
) -> bytes:
    """Read through an operation-bound descriptor beneath an authorized root."""
    root = resolve_workspace(task_id=task_id)
    if root is None:
        data = Path(path).read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            raise WorkspaceConfinementError("confined read exceeds size limit")
        return data

    expanded = Path(os.path.expanduser(os.fspath(path)))
    candidate = Path(os.path.abspath(expanded if expanded.is_absolute() else root / expanded))
    authorities = (root, *configured_read_only_roots())
    authority = None
    relative = None
    for allowed_root in authorities:
        try:
            relative = candidate.relative_to(allowed_root)
            authority = allowed_root
            break
        except ValueError:
            continue
    if authority is None or relative is None:
        raise WorkspaceConfinementError(
            f"path is outside the authorized confined read roots: {path!r}"
        )

    fd = _open_confined_descriptor(authority, relative)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(None if max_bytes is None else max_bytes + 1)
    finally:
        os.close(fd)
    if max_bytes is not None and len(data) > max_bytes:
        raise WorkspaceConfinementError("confined read exceeds size limit")
    return data


def _open_confined_descriptor(authority: Path, relative: Path) -> int:
    """Open one file beneath *authority* without a pathname race."""
    import errno

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if platform.system() == "Linux":
        import ctypes

        class OpenHow(ctypes.Structure):
            _fields_ = [
                ("flags", ctypes.c_ulonglong),
                ("mode", ctypes.c_ulonglong),
                ("resolve", ctypes.c_ulonglong),
            ]

        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        # openat2 is syscall 437 on every Linux architecture supported here.
        no_magiclinks = 0x02
        no_symlinks = 0x04
        beneath = 0x08

        def openat2(dir_fd: int, name: str, open_flags: int, resolve: int) -> int:
            how = OpenHow(open_flags, 0, resolve)
            result = syscall(
                437,
                dir_fd,
                os.fsencode(name),
                ctypes.byref(how),
                ctypes.sizeof(how),
            )
            if result < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), name)
            return int(result)

        filesystem_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            authority_fd = openat2(
                filesystem_fd,
                str(authority).lstrip("/"),
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
                no_magiclinks | no_symlinks | beneath,
            )
        except OSError as exc:
            os.close(filesystem_fd)
            if exc.errno == errno.ENOSYS:
                raise WorkspaceConfinementError(
                    "operation-bound confinement requires openat2; refusing unsafe fallback"
                ) from exc
            raise WorkspaceConfinementError(f"confined authority open failed: {exc}") from exc
        os.close(filesystem_fd)
        try:
            return openat2(authority_fd, str(relative) or ".", flags, no_magiclinks | beneath)
        except OSError as exc:
            raise WorkspaceConfinementError(f"confined file open denied: {exc}") from exc
        finally:
            os.close(authority_fd)

    # Conservative portable fallback: walk every authority and target component
    # from a root descriptor with O_NOFOLLOW. This rejects symlinks rather than
    # resolving then reopening them.
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in authority.relative_to("/").parts:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        parts = relative.parts
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return os.open(
            parts[-1] if parts else ".",
            flags | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current,
        )
    except OSError as exc:
        raise WorkspaceConfinementError(f"confined file open denied: {exc}") from exc
    finally:
        os.close(current)


@dataclass(frozen=True)
class SandboxLaunch:
    args: list[str]
    cwd: str
    env: dict[str, str]
    profile: str



_SAFE_RUN_TESTS_FLAGS_WITH_VALUE = {
    "-k",
    "-j",
    "--jobs",
    "--paths",
    "--slice",
    "--file-timeout",
    "--file-retries",
    "--tb",
    "--maxfail",
}
_SAFE_RUN_TESTS_VALUE_PREFIXES = ("--tb=", "--maxfail=", "--jobs=", "--file-timeout=", "--file-retries=", "--slice=")
_SAFE_RUN_TESTS_BARE_FLAGS = {
    "-q",
    "-v",
    "-vv",
    "-s",
    "--quiet",
    "--verbose",
    "--disable-warnings",
    "--showlocals",
}


def _command_mentions_shell_control(command: str) -> bool:
    """Return true for shell constructs outside the single supported test shape."""
    scrubbed = command.replace("&&", "")
    return any(ch in scrubbed for ch in ";|&()<>\n\r`$")


def _declared_run_tests_runners_for_worker_contract(workspace: Path) -> tuple[Path, ...]:
    try:
        from agent.private_worker_mode import get_authenticated_worker_contract

        contract = get_authenticated_worker_contract()
    except Exception:
        return ()
    if not contract:
        return ()
    contract_workspace = ((contract.get("workspace") or {}).get("path") or "")
    if contract_workspace:
        try:
            if Path(os.path.expanduser(str(contract_workspace))).resolve(strict=False) != workspace.resolve(strict=False):
                return ()
        except OSError:
            return ()
    readiness = (contract.get("skill_dependencies") or {}).get("readiness") or {}
    requirements = readiness.get("requirements") or {}
    commands = requirements.get("commands") or []
    authorities = (workspace, *configured_read_only_roots())
    declared: list[Path] = []
    for command in commands:
        raw = str(command).strip()
        if not raw or any(ch in raw for ch in "\x00;&|()<>`$\n\r"):
            continue
        try:
            parts = shlex.split(raw, posix=True)
        except ValueError:
            continue
        if len(parts) != 1:
            continue
        runner = _resolve_under_authorities(parts[0], workspace, authorities)
        if runner is None or runner.parts[-2:] != ("scripts", "run_tests.sh"):
            continue
        declared.append(runner)
    return tuple(dict.fromkeys(declared))


def _resolve_under_authorities(raw: str, cwd: Path, authorities: tuple[Path, ...]) -> Path | None:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    if any(resolved == root or root in resolved.parents for root in authorities):
        return resolved
    return None


def _run_tests_args_are_bounded(args: list[str], cwd: Path) -> bool:
    expect_value = False
    seen_separator = False
    for arg in args:
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            return False
        if any(ch in arg for ch in ";|&()<>`$"):
            return False
        if expect_value:
            expect_value = False
            if arg.startswith("-") or ".." in Path(arg).parts:
                return False
            continue
        if arg == "--":
            if seen_separator:
                return False
            seen_separator = True
            continue
        if arg in _SAFE_RUN_TESTS_FLAGS_WITH_VALUE:
            expect_value = True
            continue
        if arg in _SAFE_RUN_TESTS_BARE_FLAGS or arg.startswith(_SAFE_RUN_TESTS_VALUE_PREFIXES):
            continue
        if arg.startswith("-"):
            return False
        path = Path(arg)
        if path.is_absolute() or ".." in path.parts:
            return False
        if not (path.parts and path.parts[0] == "tests"):
            return False
        resolved = (cwd / path).resolve(strict=False)
        if not (resolved == cwd or cwd in resolved.parents):
            return False
    return not expect_value


def _is_declared_workspace_run_tests_command(command: str, workspace: Path) -> bool:
    """Allow only admitted repo-local canonical tests through broad CLI regexes."""
    declared_runners = _declared_run_tests_runners_for_worker_contract(workspace)
    if not declared_runners:
        return False
    if command.count("&&") > 1 or "||" in command or _command_mentions_shell_control(command):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    cwd = workspace
    if tokens[:1] == ["cd"]:
        if len(tokens) < 4 or tokens[2] != "&&":
            return False
        if tokens[1].startswith("-"):
            return False
        cd_target = _resolve_under_authorities(tokens[1], workspace, (workspace,))
        if cd_target is None or not cd_target.is_dir():
            return False
        cwd = cd_target
        tokens = tokens[3:]
    elif "&&" in tokens:
        return False
    runner_raw = tokens[0]
    if Path(runner_raw).name != "run_tests.sh":
        return False
    runner = _resolve_under_authorities(runner_raw, cwd, (workspace, *configured_read_only_roots()))
    if runner is None or runner.parts[-2:] != ("scripts", "run_tests.sh"):
        return False
    if runner not in declared_runners:
        return False
    if not runner.is_file() or not os.access(runner, os.X_OK):
        return False
    return _run_tests_args_are_bounded(tokens[1:], runner.parents[1])

_CONTROL_PLANE_COMMANDS = (
    re.compile(r"(?:^|[;&|()\n]\s*)\s*(?:[^\s;&|()]*/)?hermes(?:\s|$)"),
    re.compile(r"\bhermes_cli(?:\.[A-Za-z0-9_.-]+)?\b"),
)


def _sbpl_quote(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


_MACOS_RUNTIME_READ_ROOTS = (
    Path("/System"),
    Path("/usr/bin"),
    Path("/usr/lib"),
    Path("/usr/libexec"),
    Path("/usr/share"),
    Path("/bin"),
    Path("/sbin"),
    Path("/Library/Apple"),
    Path("/Library/Frameworks"),
    Path("/private/var/db"),
    Path("/private/var/select"),
    Path("/dev"),
    Path("/opt/homebrew/bin"),
    Path("/opt/homebrew/sbin"),
    Path("/opt/homebrew/lib"),
    Path("/opt/homebrew/Cellar"),
    Path("/opt/homebrew/opt"),
    Path("/opt/homebrew/share"),
    Path("/usr/local/bin"),
    Path("/usr/local/sbin"),
    Path("/usr/local/lib"),
    Path("/usr/local/Cellar"),
    Path("/usr/local/opt"),
    Path("/usr/local/share"),
)


_LINUX_RUNTIME_READ_ROOTS = (Path("/usr"),)


def _linux_private_root_setup(
    workspace: Path,
    read_only_roots: tuple[Path, ...],
) -> tuple[str, Path]:
    """Build the mount script for a minimal chroot-backed Linux namespace."""
    if any(path.resolve() == Path("/") for path in read_only_roots):
        raise WorkspaceConfinementError(
            "workspace confinement refuses to bind the host root"
        )
    sandbox_root = workspace / ".hermes-sandbox-root"
    sandbox_root.mkdir(mode=0o700, exist_ok=True)
    roots = tuple(
        path
        for path in dict.fromkeys((*_LINUX_RUNTIME_READ_ROOTS, *_runtime_read_roots(), *read_only_roots))
        if path.exists()
    )

    def q(value: Path | str) -> str:
        return shlex.quote(str(value))

    def bind_read_only(source: Path) -> str:
        destination = sandbox_root / source.relative_to("/")
        return (
            f"mkdir -p {q(destination)}; "
            f"mount --rbind {q(source)} {q(destination)}; "
            f"mount -o remount,ro,bind {q(destination)}; "
        )

    setup = (
        "set -eu; mount --make-rprivate /; "
        f"mount -t tmpfs -o mode=0755,size=16m tmpfs {q(sandbox_root)}; "
        + "".join(bind_read_only(root) for root in roots)
    )
    # Preserve merged-/usr compatibility links without exposing the host root.
    for legacy in (Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
        destination = sandbox_root / legacy.relative_to("/")
        if legacy.is_symlink():
            setup += f"ln -s {q(os.readlink(legacy))} {q(destination)}; "
        elif legacy.exists():
            setup += bind_read_only(legacy)

    for device in ("null", "zero", "random", "urandom"):
        source = Path("/dev") / device
        destination = sandbox_root / "dev" / device
        if source.exists():
            setup += (
                f"mkdir -p {q(destination.parent)}; touch {q(destination)}; "
                f"mount --bind {q(source)} {q(destination)}; "
            )
    setup += (
        f"mkdir -p {q(sandbox_root / 'proc')}; "
        f"mount -t proc proc {q(sandbox_root / 'proc')}; "
        f"ln -s /proc/self/fd {q(sandbox_root / 'dev' / 'fd')}; "
    )
    for runtime_file in (Path("/etc/ld.so.cache"), Path("/etc/localtime")):
        if runtime_file.is_file():
            destination = sandbox_root / runtime_file.relative_to("/")
            setup += (
                f"mkdir -p {q(destination.parent)}; touch {q(destination)}; "
                f"mount --bind {q(runtime_file)} {q(destination)}; "
                f"mount -o remount,ro,bind {q(destination)}; "
            )

    destination = sandbox_root / workspace.relative_to("/")
    setup += (
        f"mkdir -p {q(destination)}; "
        f"mount --bind {q(workspace)} {q(destination)}; "
        f"mount -o remount,rw,bind {q(destination)}; "
        f"exec chroot {q(sandbox_root)} /bin/bash -c "
        + q('cd "$1"; shift; exec /usr/bin/env -i "$@"')
        + f" sandbox {q(workspace)} \"${{@:2}}\""
    )
    return setup, sandbox_root


def build_macos_sandbox_profile(
    workspace: Path | str,
    read_only_roots: tuple[Path, ...] | None = None,
) -> str:
    root = Path(workspace).resolve()
    quoted = _sbpl_quote(root)
    configured_roots = tuple(Path(item).resolve() for item in (read_only_roots or ()))
    read_roots = tuple(
        dict.fromkeys((root, *configured_roots, *_MACOS_RUNTIME_READ_ROOTS, *_runtime_read_roots()))
    )
    # Path lookup needs to traverse/list each ancestor directory. Grant only
    # the ancestor directory itself (``literal``), never its contents; file
    # bytes remain limited to explicit ``subpath`` roots.
    ancestor_literals = sorted(
        {str(parent) for read_root in read_roots for parent in read_root.parents}
    )
    read_scope = "".join(
        f'(subpath "{_sbpl_quote(read_root)}")' for read_root in read_roots
    ) + "".join(
        f'(literal "{_sbpl_quote(parent)}")' for parent in ancestor_literals
    )
    write_scope = (
        f'(subpath "{quoted}")'
        '(literal "/dev/null")(literal "/dev/tty")(subpath "/dev/fd")'
    )
    return (
        '(version 1)'
        '(allow default)'
        f'(deny file-read* (require-not (require-any {read_scope})))'
        f'(deny file-write* (require-not (require-any {write_scope})))'
        '(deny network*)'
    )



def _pytest_capable_python(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        probe = subprocess.run(
            [str(path), "-c", "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pytest') else 1)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    # Preserve venv identity for scripts/run_tests.sh.  Resolving all the way
    # to the Homebrew/framework binary loses pyvenv.cfg and therefore pytest.
    for parent_name in {"bin", "Scripts"}:
        if path.parent.name == parent_name and path.parent.parent.is_dir():
            root = path.parent.parent.resolve()
            candidate = root / path.parent.name / path.name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return path


def _symlink_chain_roots(path: Path) -> list[Path]:
    roots: list[Path] = []
    current = path
    seen: set[Path] = set()
    for _ in range(8):
        roots.append(current)
        if current in seen or not current.is_symlink():
            break
        seen.add(current)
        try:
            target_raw = os.readlink(current)
        except OSError:
            break
        target = Path(target_raw)
        current = target if target.is_absolute() else current.parent / target
    return roots


def _python_runtime_root(python: Path | None) -> tuple[Path, ...]:
    if python is None:
        return ()
    # POSIX venv: <venv>/bin/python; native Windows/MSYS: <venv>/Scripts/python.exe.
    for parent_name in {"bin", "Scripts"}:
        if python.parent.name == parent_name and python.parent.parent.exists():
            roots: list[Path] = []
            roots.extend(_symlink_chain_roots(python.parent.parent))
            try:
                target = python.resolve(strict=True)
            except OSError:
                target = None
            if target is not None:
                roots.append(target.parent.resolve())
                if target.parent.parent.exists():
                    roots.append(target.parent.parent.resolve())
            expanded: list[Path] = []
            for root in roots:
                expanded.append(root.absolute())
                expanded.append(root.resolve(strict=False))
            return tuple(dict.fromkeys(expanded))
    return (python.parent.absolute(), python.parent.resolve(strict=False))


def prepare_sandbox_launch(command: str, workspace: Path | str) -> SandboxLaunch:
    """Construct and probe a fail-closed macOS or Linux OS sandbox."""
    root = Path(workspace)
    if not root.is_absolute():
        raise WorkspaceConfinementError("sandbox workspace must be absolute")
    root = root.resolve()
    if not root.is_dir():
        raise WorkspaceConfinementError("sandbox workspace must be an existing directory")
    if any(pattern.search(command) for pattern in _CONTROL_PLANE_COMMANDS):
        if not _is_declared_workspace_run_tests_command(command, root):
            raise WorkspaceConfinementError(
                "workspace-confined terminal blocks Hermes/Kanban/profile/config control-plane commands"
            )
    temp_dir = root / ".hermes-tmp"
    terminal_home = root / ".hermes-terminal-home"
    temp_dir.mkdir(mode=0o700, exist_ok=True)
    terminal_home.mkdir(mode=0o700, exist_ok=True)
    safe_names = {"LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL"}
    env = {name: value for name, value in os.environ.items() if name in safe_names}
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["HOME"] = str(terminal_home)
    env["TMPDIR"] = str(temp_dir)
    requested_test_python = os.environ.get("HERMES_PYTHON")
    requested_test_python_path = _pytest_capable_python(requested_test_python)
    test_python = requested_test_python_path or _pytest_capable_python(sys.executable)
    if test_python is not None:
        env["HERMES_PYTHON"] = str(test_python)
    extra_runtime_roots = _python_runtime_root(test_python)
    env_assignments = [f"{name}={value}" for name, value in sorted(env.items())]

    system = platform.system()
    if system == "Darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise WorkspaceConfinementError(
                "workspace-confined terminal requires sandbox-exec; refusing unsandboxed fallback"
            )
        profile = build_macos_sandbox_profile(root, (*configured_read_only_roots(), *extra_runtime_roots))
        args = [sandbox_exec, "-p", profile, "/usr/bin/env", "-i", *env_assignments, "/bin/bash", "-c", command]
        probe_args = [sandbox_exec, "-p", profile, "/usr/bin/true"]
    elif system == "Linux":
        unshare = shutil.which("unshare")
        if not unshare:
            raise WorkspaceConfinementError(
                "workspace-confined terminal requires unshare; refusing unsandboxed fallback"
            )
        # Build a new empty root and bind only runtime dependencies, explicit
        # read-only roots, and the admitted workspace into it. Merely remounting
        # the host root read-only prevents mutation but leaks every host file.
        setup, sandbox_root = _linux_private_root_setup(
            root, (*configured_read_only_roots(), *extra_runtime_roots)
        )
        namespace = [unshare, "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--mount-proc"]
        args = [*namespace, "/bin/bash", "-c", setup, "sandbox", str(sandbox_root), *env_assignments, "/bin/bash", "-c", command]
        probe_args = [*namespace, "/bin/bash", "-c", setup, "sandbox", str(sandbox_root), *env_assignments, "/usr/bin/true"]
        profile = "linux:private-root+user+mount+network"
    else:
        raise WorkspaceConfinementError(
            "workspace-confined terminal has no supported OS boundary"
        )

    try:
        probe = subprocess.run(
            probe_args, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceConfinementError(f"sandbox failed to start: {exc}") from exc
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip()
        raise WorkspaceConfinementError(
            f"sandbox failed to start: {detail or f'exit {probe.returncode}'}"
        )
    return SandboxLaunch(args=args, cwd=str(root), env=env, profile=profile)

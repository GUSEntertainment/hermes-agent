"""Behavior tests for Kanban worker filesystem confinement."""

import platform
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.workspace_confinement import (
    SandboxLaunch,
    WorkspaceConfinementError,
    build_macos_sandbox_profile,
    prepare_sandbox_launch,
    resolve_confined_path,
)


def _enable_worker_confinement(
    monkeypatch,
    workspace: Path,
    task_id: str = "t-worker",
    read_only_roots=(),
    board_path: Path | None = None,
    readiness: dict | None = None,
):
    from agent import private_worker_mode
    home = workspace.parent / "profiles" / "terra-maker-alpha"
    home.mkdir(parents=True, exist_ok=True)
    board = board_path or workspace.parent / "board.db"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.touch(exist_ok=True)
    board_info = board.stat()
    roots = [Path(item) for item in read_only_roots]
    contract = {
        "version": "hermes.worker-admission/v1", "task": task_id,
        "admission_nonce": "workspace-test-admission",
        "board_database": {
            "path": str(board.resolve()), "device": board_info.st_dev,
            "inode": board_info.st_ino,
        },
        "profile": "terra-maker-alpha", "profile_home": str(home),
        "profile_config_digest": "a" * 64, "provider": "test", "model": "test",
        "fallbacks": [], "reasoning": None, "toolsets": [], "skills": [],
        "skill_dependencies": {"loaded": [], "unavailable": [], **({"readiness": readiness} if readiness is not None else {})}, "privacy": {},
        "tenant": None,
        "workspace": {"kind": "dir", "path": str(workspace), "branch": None, "project": None},
        "workspace_confinement": True,
        "workspace_read_only_roots": [str(item) for item in roots],
        "workspace_read_only_root_identities": [
            {
                "path": str(item.resolve()), "device": item.stat().st_dev,
                "inode": item.stat().st_ino,
            }
            for item in roots
        ],
    }
    private_worker_mode._reset_private_worker_contract_for_tests()
    private_worker_mode.install_authenticated_worker_contract(
        contract, durable_validator=lambda _contract, _digest: True
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))


@pytest.fixture(autouse=True)
def _reset_contract():
    from agent import private_worker_mode
    private_worker_mode._reset_private_worker_contract_for_tests()
    yield
    private_worker_mode._reset_private_worker_contract_for_tests()


def test_non_worker_preserves_cwd_relative_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert resolve_confined_path("nested/file.txt", task_id="default") == (
        tmp_path / "nested" / "file.txt"
    )


def test_matching_worker_task_allows_workspace_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace)
    monkeypatch.chdir(workspace)

    assert resolve_confined_path("images/example.png", task_id="t-worker") == (
        workspace / "images" / "example.png"
    )


def test_matching_worker_task_rejects_absolute_escape(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace)

    with pytest.raises(WorkspaceConfinementError, match="outside the task workspace"):
        resolve_confined_path(tmp_path / "outside.png", task_id="t-worker")


def test_matching_worker_task_rejects_symlink_escape(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not read")
    link = workspace / "link.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    _enable_worker_confinement(monkeypatch, workspace)

    with pytest.raises(WorkspaceConfinementError, match="outside the task workspace"):
        resolve_confined_path(link, task_id="t-worker")


@pytest.mark.parametrize("sensitive", [Path("/"), Path.home().parent])
def test_configured_read_root_rejects_host_sensitive_roots(
    tmp_path, monkeypatch, sensitive
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace, read_only_roots=[sensitive])

    with pytest.raises(WorkspaceConfinementError, match="not trusted"):
        resolve_confined_path(sensitive, task_id="t-worker", access="read")


def test_configured_read_root_rejects_profile_board_and_workspace_topology(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "profiles" / "terra-maker-alpha"
    profile_home.mkdir(parents=True)
    board_parent = tmp_path / "boards"
    board_parent.mkdir()
    board = board_parent / "board.db"
    board.touch()

    for sensitive in (profile_home, board_parent, workspace.parent):
        from agent import private_worker_mode

        private_worker_mode._reset_private_worker_contract_for_tests()
        _enable_worker_confinement(
            monkeypatch,
            workspace,
            read_only_roots=[sensitive],
            board_path=board if sensitive == board_parent else None,
        )
        with pytest.raises(WorkspaceConfinementError, match="not trusted"):
            resolve_confined_path(sensitive, task_id="t-worker", access="read")


def test_configured_read_root_allows_explicit_external_project(
    tmp_path, monkeypatch
):
    workspace_parent = tmp_path / "workspaces"
    workspace_parent.mkdir()
    workspace = workspace_parent / "workspace"
    workspace.mkdir()
    project = tmp_path / "trusted" / "project"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    source = project / "README.md"
    source.write_text("trusted-project", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace, read_only_roots=[project])

    assert resolve_confined_path(source, task_id="t-worker", access="read") == source


def test_configured_read_root_rejects_symlink_to_sensitive_home(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "profiles" / "terra-maker-alpha"
    profile_home.mkdir(parents=True)
    link = tmp_path / "trusted-link"
    link.symlink_to(profile_home, target_is_directory=True)
    _enable_worker_confinement(monkeypatch, workspace, read_only_roots=[link])

    with pytest.raises(WorkspaceConfinementError, match="not trusted"):
        resolve_confined_path(link, task_id="t-worker", access="read")


def test_configured_read_root_replacement_race_fails_closed(tmp_path, monkeypatch):
    import tools.workspace_confinement as confinement

    workspace = tmp_path / "workspaces" / "workspace"
    workspace.mkdir(parents=True)
    project = tmp_path / "trusted" / "project"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "safe.txt").write_text("safe", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace, read_only_roots=[project])
    assert confinement.configured_read_only_roots() == (project.resolve(),)

    original = project.with_name("project-original")
    project.rename(original)
    project.mkdir()
    (project / ".git").mkdir()
    (project / "safe.txt").write_text("replacement-secret", encoding="utf-8")

    with pytest.raises(WorkspaceConfinementError, match="identity changed"):
        confinement.read_confined_bytes(
            project / "safe.txt", task_id="t-worker"
        )


def test_worker_turn_id_is_independent_from_authenticated_task_id(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace)

    assert resolve_confined_path(
        workspace / "ok.png", task_id="per-turn-uuid"
    ) == (workspace / "ok.png")


def test_forged_worker_environment_does_not_enable_confinement(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-worker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "relative-workspace")

    assert resolve_confined_path(tmp_path / "ok.png", task_id="t-worker") == (
        tmp_path / "ok.png"
    )


def test_admission_bearing_process_never_falls_back_to_unconfined_paths(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"malformed")
    os.close(write_fd)
    monkeypatch.setenv("HERMES_WORKER_ADMISSION_FD", str(read_fd))

    with pytest.raises(PermissionError, match="malformed"):
        resolve_confined_path(tmp_path / "must-not-open.txt", task_id="t-worker")


def test_linux_launch_builds_private_root_not_read_only_host_root(tmp_path, monkeypatch):
    import tools.workspace_confinement as confinement

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    monkeypatch.setattr(confinement.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        confinement.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    launch = prepare_sandbox_launch("/usr/bin/true", workspace)
    setup = launch.args[launch.args.index("-c") + 1]
    assert "chroot" in setup
    assert "mount -t tmpfs" in setup
    assert "remount,ro,bind /;" not in setup


def test_linux_private_root_builder_never_accepts_host_root(tmp_path):
    import tools.workspace_confinement as confinement

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(WorkspaceConfinementError, match="host root"):
        confinement._linux_private_root_setup(workspace, (Path("/"),))


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux namespace test")
def test_linux_sandbox_denies_outside_reads_and_network_but_keeps_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    sibling = tmp_path / "sibling" / "private.txt"
    sibling.parent.mkdir()
    sibling.write_text("sibling-secret", encoding="utf-8")
    profile_secret = tmp_path / "profiles" / "other" / "auth.json"
    profile_secret.parent.mkdir(parents=True)
    profile_secret.write_text("credential-secret", encoding="utf-8")
    monkeypatch.setenv("PARENT_SECRET_CANARY", "do-not-inherit")
    _enable_worker_confinement(monkeypatch, workspace)

    command = (
        "set -eu; "
        "test -z \"${PARENT_SECRET_CANARY:-}\"; "
        "test \"$HOME\" = \"$PWD/.hermes-terminal-home\"; "
        "test \"$TMPDIR\" = \"$PWD/.hermes-tmp\"; "
        "test \"$(cat inside.txt)\" = inside; "
        "printf ok > written.txt; "
        f"if cat {sibling} >/dev/null 2>&1; then exit 90; fi; "
        f"if cat {profile_secret} >/dev/null 2>&1; then exit 91; fi; "
        "if cat /etc/passwd >/dev/null 2>&1; then exit 92; fi; "
        "if test -e /opt/data; then exit 93; fi; "
        "/usr/bin/python3 -c 'import json, pathlib; assert pathlib.Path(\"inside.txt\").read_text() == \"inside\"'; "
        "! /usr/bin/python3 -c 'import socket; socket.create_connection((\"1.1.1.1\",53),.2)'"
    )
    try:
        launch = prepare_sandbox_launch(command, workspace)
    except WorkspaceConfinementError as exc:
        # Kernels/container policies may disable user namespaces. The boundary
        # must refuse to launch rather than run the command without isolation.
        assert "sandbox failed to start" in str(exc)
        assert not (workspace / "written.txt").exists()
        return

    result = subprocess.run(
        launch.args, cwd=launch.cwd, env=launch.env,
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "written.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(
    platform.system() == "Windows", reason="BASH_ENV is a POSIX shell startup hook"
)
def test_confined_foreground_installs_sanitized_env_before_first_shell(
    tmp_path, monkeypatch
):
    """Host shell startup hooks must not run ahead of the sandbox launcher."""
    import json

    import tools.terminal_tool as terminal_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    startup_sentinel = outside / "pre-confinement-startup"
    bash_env = outside / "host-bash-env"
    bash_env.write_text(
        f"printf '%s' \"$HOST_CREDENTIAL_CANARY\" > {startup_sentinel}\n",
        encoding="utf-8",
    )
    task_id = "t-worker"
    _enable_worker_confinement(monkeypatch, workspace, task_id=task_id)

    safe_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace / ".hermes-terminal-home"),
        "TMPDIR": str(workspace / ".hermes-tmp"),
    }
    Path(safe_env["HOME"]).mkdir()
    Path(safe_env["TMPDIR"]).mkdir()

    def fake_sandbox_launch(command, root):
        assert Path(root) == workspace
        assignments = [f"{name}={value}" for name, value in sorted(safe_env.items())]
        # Keep the nested env -i used by the real launcher.  The regression is
        # specifically that an ambient outer bash starts before this argv.
        return SandboxLaunch(
            args=["/usr/bin/env", "-i", *assignments, "/bin/bash", "-c", command],
            cwd=str(workspace),
            env=safe_env,
            profile="test:first-process-boundary",
        )

    monkeypatch.setenv("BASH_ENV", str(bash_env))
    monkeypatch.setenv("ENV", str(bash_env))
    monkeypatch.setenv("PYTHONSTARTUP", str(bash_env))
    monkeypatch.setenv("HOST_CREDENTIAL_CANARY", "host-secret")
    monkeypatch.setattr(terminal_module, "_active_environments", {})
    monkeypatch.setattr(terminal_module, "_last_activity", {})
    monkeypatch.setattr(terminal_module, "_session_cwd", {})
    monkeypatch.setattr(terminal_module, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_module,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "cwd": str(workspace),
            "timeout": 15,
            "lifetime_seconds": 3600,
        },
    )
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_module, "prepare_sandbox_launch", fake_sandbox_launch)
    guarded_popen = subprocess.Popen

    def spawn_foreground_probe(*args, **kwargs):
        # The assertion concerns the first shell's environment. macOS's test
        # guard disallows detached processes, so keep this bounded shell owned
        # by the test runner while retaining the real environment and argv.
        if platform.system() == "Darwin":
            assert kwargs["start_new_session"] is True
            kwargs["start_new_session"] = False
        return guarded_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn_foreground_probe)
    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    command = (
        "set -eu; "
        "test \"$PATH\" = /usr/bin:/bin; "
        "test \"$HOME\" = \"$PWD/.hermes-terminal-home\"; "
        "test \"$TMPDIR\" = \"$PWD/.hermes-tmp\"; "
        "test -z \"${BASH_ENV:-}${ENV:-}${PYTHONSTARTUP:-}${HOST_CREDENTIAL_CANARY:-}\"; "
        "printf confined-ok"
    )
    try:
        result = json.loads(
            terminal_module.terminal_tool(command=command, task_id=task_id)
        )
    finally:
        created_env = terminal_module._active_environments.get(task_id)
        if created_env is not None:
            created_env.cleanup()

    assert result["exit_code"] == 0, result
    assert result["output"] == "confined-ok"
    assert not startup_sentinel.exists(), (
        "ambient BASH_ENV ran in LocalEnvironment's outer shell before confinement"
    )


def test_confined_read_final_component_swap_cannot_leak_outside(tmp_path, monkeypatch):
    import json
    import tools.file_tools as file_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace)

    original = file_tools._workspace_confinement_error
    swapped = False

    def validate_then_swap(path, task_id, *, access="write"):
        nonlocal swapped
        error = original(path, task_id, access=access)
        if error is None and not swapped:
            swapped = True
            target.rename(workspace / "note.safe")
            target.symlink_to(outside)
        return error

    monkeypatch.setattr(file_tools, "_workspace_confinement_error", validate_then_swap)
    result = json.loads(file_tools.read_file_tool(str(target), task_id="t-worker"))
    assert "OUTSIDE-SECRET" not in result.get("content", "")
    assert "error" in result or result.get("success") is False


def test_confined_write_final_component_swap_fails_closed(tmp_path, monkeypatch):
    import json
    import tools.file_tools as file_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace)

    original = file_tools._workspace_confinement_error
    swapped = False

    def validate_then_swap(path, task_id, *, access="write"):
        nonlocal swapped
        error = original(path, task_id, access=access)
        if error is None and not swapped:
            swapped = True
            target.rename(workspace / "note.safe")
            target.symlink_to(outside)
        return error

    monkeypatch.setattr(file_tools, "_workspace_confinement_error", validate_then_swap)
    result = json.loads(
        file_tools.write_file_tool(str(target), "changed", task_id="t-worker")
    )
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"
    assert "error" in result
    assert target.is_symlink()


def test_confined_patch_ancestor_swap_cannot_edit_outside(tmp_path, monkeypatch):
    import json
    import tools.file_tools as file_tools

    workspace = tmp_path / "workspace"
    directory = workspace / "docs"
    directory.mkdir(parents=True)
    target = directory / "note.txt"
    target.write_text("safe", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "note.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace)

    original = file_tools._workspace_confinement_error
    swapped = False

    def validate_then_swap(path, task_id, *, access="write"):
        nonlocal swapped
        error = original(path, task_id, access=access)
        if error is None and not swapped:
            swapped = True
            directory.rename(workspace / "docs.safe")
            directory.symlink_to(outside_dir, target_is_directory=True)
        return error

    monkeypatch.setattr(file_tools, "_workspace_confinement_error", validate_then_swap)
    result = json.loads(
        file_tools.patch_tool(
            mode="replace",
            path=str(target),
            old_string="safe",
            new_string="changed",
            task_id="t-worker",
        )
    )
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"
    assert "error" in result


def test_confined_v4a_patch_final_swap_cannot_edit_outside(tmp_path, monkeypatch):
    import json
    import tools.file_tools as file_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace)

    original = file_tools._workspace_confinement_error
    swapped = False

    def validate_then_swap(path, task_id, *, access="write"):
        nonlocal swapped
        error = original(path, task_id, access=access)
        if error is None and not swapped:
            swapped = True
            target.rename(workspace / "note.safe")
            target.symlink_to(outside)
        return error

    monkeypatch.setattr(file_tools, "_workspace_confinement_error", validate_then_swap)
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {target}\n"
        "@@\n"
        "-safe\n"
        "+changed\n"
        "*** End Patch\n"
    )
    result = json.loads(
        file_tools.patch_tool(mode="patch", patch=patch, task_id="t-worker")
    )
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET\n"
    assert "error" in result


@pytest.mark.asyncio
async def test_confined_media_final_component_swap_cannot_leak_outside(tmp_path, monkeypatch):
    import tools.image_source as image_source
    import tools.workspace_confinement as confinement

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "image.png"
    target.write_bytes(png)
    outside = tmp_path / "secret.png"
    outside.write_bytes(png + b"OUTSIDE-MEDIA-SECRET")
    _enable_worker_confinement(monkeypatch, workspace)
    monkeypatch.setenv("TERMINAL_ENV", "local")

    original = confinement.resolve_confined_path
    swapped = False

    def validate_then_swap(*args, **kwargs):
        nonlocal swapped
        resolved = original(*args, **kwargs)
        if not swapped:
            swapped = True
            target.rename(workspace / "image.safe")
            target.symlink_to(outside)
        return resolved

    monkeypatch.setattr(confinement, "resolve_confined_path", validate_then_swap)
    with pytest.raises(image_source.SourceUnsafe):
        await image_source.resolve_image_source(
            str(target), image_source.ResolveContext(task_id="t-worker")
        )


@pytest.mark.asyncio
async def test_confined_media_ancestor_swap_cannot_leak_outside(tmp_path, monkeypatch):
    import tools.image_source as image_source
    import tools.workspace_confinement as confinement

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    workspace = tmp_path / "workspace"
    directory = workspace / "images"
    directory.mkdir(parents=True)
    target = directory / "image.png"
    target.write_bytes(png)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "image.png").write_bytes(png + b"OUTSIDE-MEDIA-SECRET")
    _enable_worker_confinement(monkeypatch, workspace)
    monkeypatch.setenv("TERMINAL_ENV", "local")

    original = confinement.resolve_confined_path
    swapped = False

    def validate_then_swap(*args, **kwargs):
        nonlocal swapped
        resolved = original(*args, **kwargs)
        if not swapped:
            swapped = True
            directory.rename(workspace / "images.safe")
            directory.symlink_to(outside_dir, target_is_directory=True)
        return resolved

    monkeypatch.setattr(confinement, "resolve_confined_path", validate_then_swap)
    with pytest.raises(image_source.SourceUnsafe):
        await image_source.resolve_image_source(
            str(target), image_source.ResolveContext(task_id="t-worker")
        )


def test_confined_file_tools_preserve_workspace_read_write_and_patch(tmp_path, monkeypatch):
    import json
    import tools.file_tools as file_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(tmp_path))
    _enable_worker_confinement(monkeypatch, workspace)

    written = json.loads(
        file_tools.write_file_tool("note.txt", "before\n", task_id="t-worker")
    )
    assert "error" not in written
    read = json.loads(file_tools.read_file_tool("note.txt", task_id="t-worker"))
    assert read["content"] == "1|before"  # upstream v0.21.5: no phantom trailing line (cat -n parity)
    patched = json.loads(
        file_tools.patch_tool(
            mode="replace",
            path="note.txt",
            old_string="before",
            new_string="after",
            task_id="t-worker",
        )
    )
    assert "error" not in patched
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "after\n"


@pytest.mark.parametrize("background", [False, True])
def test_terminal_foreground_and_background_fail_closed_without_os_boundary(
    tmp_path, monkeypatch, background
):
    import json
    import tools.terminal_tool as terminal_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace)
    monkeypatch.setattr(
        terminal_module,
        "prepare_sandbox_launch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WorkspaceConfinementError("required OS boundary unavailable")
        ),
    )
    result = json.loads(
        terminal_module.terminal_tool(
            "printf unsafe", background=background, task_id="t-worker"
        )
    )
    assert result["status"] == "blocked"
    assert result["exit_code"] == -1
    assert "OS boundary unavailable" in result["error"]


def test_declared_workspace_run_tests_bypasses_broad_hermes_cli_control_regex(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "active-source-v4"
    runner = source / "scripts" / "run_tests.sh"
    test_file = source / "tests" / "hermes_cli" / "test_review_phase.py"
    runner.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    _enable_worker_confinement(
        monkeypatch,
        workspace,
        readiness={"requirements": {"commands": ["active-source-v4/scripts/run_tests.sh"]}},
    )

    import tools.workspace_confinement as confinement

    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    monkeypatch.setattr(confinement.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        confinement.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    launch = prepare_sandbox_launch(
        "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py -q",
        workspace,
    )
    assert isinstance(launch, SandboxLaunch)



def test_declared_workspace_run_tests_requires_exact_declared_runner(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "active-source-v4"
    other = workspace / "other-source"
    for root in (source, other):
        runner = root / "scripts" / "run_tests.sh"
        test_file = root / "tests" / "hermes_cli" / "test_review_phase.py"
        runner.parent.mkdir(parents=True)
        test_file.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    _enable_worker_confinement(
        monkeypatch,
        workspace,
        readiness={"requirements": {"commands": ["other-source/scripts/run_tests.sh"]}},
    )

    with pytest.raises(WorkspaceConfinementError, match="control-plane"):
        prepare_sandbox_launch(
            "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py -q",
            workspace,
        )


def test_declared_workspace_run_tests_accepts_exact_declared_nested_runner(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "active-source-v4"
    runner = source / "scripts" / "run_tests.sh"
    test_file = source / "tests" / "hermes_cli" / "test_review_phase.py"
    runner.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    _enable_worker_confinement(
        monkeypatch,
        workspace,
        readiness={"requirements": {"commands": ["active-source-v4/scripts/run_tests.sh"]}},
    )

    import tools.workspace_confinement as confinement

    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    monkeypatch.setattr(confinement.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        confinement.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    launch = prepare_sandbox_launch(
        "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py -q",
        workspace,
    )
    assert isinstance(launch, SandboxLaunch)

def test_workspace_run_tests_stays_blocked_without_admission_requirement(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "active-source-v4"
    runner = source / "scripts" / "run_tests.sh"
    test_file = source / "tests" / "hermes_cli" / "test_review_phase.py"
    runner.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    _enable_worker_confinement(monkeypatch, workspace)

    with pytest.raises(WorkspaceConfinementError, match="control-plane"):
        prepare_sandbox_launch(
            "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py -q",
            workspace,
        )


@pytest.mark.parametrize(
    "command",
    [
        "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py; hermes kanban list",
        "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/test_review_phase.py && hermes kanban list",
        "cd active-source-v4 && scripts/run_tests.sh tests/hermes_cli/../secret.py",
        "cd active-source-v4 && scripts/run_tests.sh --live-system-guard-bypass tests/hermes_cli/test_review_phase.py",
    ],
)
def test_declared_workspace_run_tests_exception_rejects_unbounded_shell_shapes(tmp_path, monkeypatch, command):
    workspace = tmp_path / "workspace"
    source = workspace / "active-source-v4"
    runner = source / "scripts" / "run_tests.sh"
    test_file = source / "tests" / "hermes_cli" / "test_review_phase.py"
    runner.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    _enable_worker_confinement(
        monkeypatch,
        workspace,
        readiness={"requirements": {"commands": ["active-source-v4/scripts/run_tests.sh"]}},
    )

    with pytest.raises(WorkspaceConfinementError, match="control-plane"):
        prepare_sandbox_launch(command, workspace)


def test_confined_terminal_forwards_only_pytest_capable_hermes_python(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _enable_worker_confinement(monkeypatch, workspace)
    import tools.workspace_confinement as confinement

    good_python = tmp_path / "runtime" / "bin" / "python"
    bad_python = tmp_path / "bad" / "bin" / "python"
    good_python.parent.mkdir(parents=True)
    bad_python.parent.mkdir(parents=True)
    good_python.write_text("", encoding="utf-8")
    bad_python.write_text("", encoding="utf-8")
    good_python.chmod(0o755)
    bad_python.chmod(0o755)
    monkeypatch.setenv("HERMES_PYTHON", str(good_python))
    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    monkeypatch.setattr(confinement.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, **kwargs):
        if args[0] == str(good_python):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == str(bad_python):
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(confinement.subprocess, "run", fake_run)
    launch = prepare_sandbox_launch("/usr/bin/true", workspace)
    assert f"HERMES_PYTHON={good_python}" in launch.args
    setup = launch.args[launch.args.index("-c") + 1]
    assert str(good_python.parent.parent.resolve()) in setup

    monkeypatch.setenv("HERMES_PYTHON", str(bad_python))
    launch = prepare_sandbox_launch("/usr/bin/true", workspace)
    assert f"HERMES_PYTHON={bad_python}" not in launch.args
    rejected_root = str(bad_python.parent.parent.resolve())
    assert rejected_root not in "\n".join(launch.args)


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="requires macOS sandbox-exec",
)
def test_macos_sandbox_allows_workspace_io_and_denies_sibling_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    outside_write = tmp_path / "outside-write.txt"
    profile = build_macos_sandbox_profile(workspace)
    prefix = ["/usr/bin/sandbox-exec", "-p", profile]

    read_inside = subprocess.run(
        [*prefix, "/bin/cat", str(inside)], cwd=workspace,
        capture_output=True, text=True, check=False,
    )
    read_outside = subprocess.run(
        [*prefix, "/bin/cat", str(outside)], cwd=workspace,
        capture_output=True, text=True, check=False,
    )
    write_inside = subprocess.run(
        [*prefix, "/bin/sh", "-c", "printf changed > inside-write.txt"],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    write_outside = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"printf escaped > {outside_write}"],
        cwd=workspace, capture_output=True, text=True, check=False,
    )

    assert read_inside.returncode == 0
    assert read_inside.stdout == "inside"
    assert read_outside.returncode != 0
    assert write_inside.returncode == 0
    assert (workspace / "inside-write.txt").read_text(encoding="utf-8") == "changed"
    assert write_outside.returncode != 0
    assert not outside_write.exists()

#!/usr/bin/env python3
"""Offline runtime preflight in fresh homes; never operates on the live board."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def require(value, message):
    if not value:
        raise RuntimeError(message)


def verify_manifest(path, roots):
    manifest = json.loads(path.read_text())
    require(manifest.get("format") == 1, "Unsupported overlay format")
    for lane, root in roots.items():
        record = manifest["lanes"][lane]
        patch = path.parent / record["patch"]
        require(patch.resolve().is_relative_to(path.parent.resolve()), "Patch escapes package")
        require(hashlib.sha256(patch.read_bytes()).hexdigest() == record["patch_sha256"],
                f"{lane}: patch digest mismatch")
        for entry in record["files"]:
            source = root / entry["path"]
            require(source.resolve().is_relative_to(root), "Source escapes candidate")
            actual = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
            require(actual == entry["after_sha256"], f"{lane}: candidate drift: {entry['path']}")


def controller_probe():
    from dataclasses import asdict
    from hermes_cli import kanban_db as kb
    from hermes_cli.board_owner_manage import describe, execute

    home = Path(os.environ["HERMES_HOME"])
    kb.init_db()
    with kb.connect_closing() as conn:
        workspace = home / "kanban" / "workspaces" / "owner-preflight"
        workspace.mkdir(parents=True)
        artifact = workspace / "preserved.txt"
        artifact.write_text("unfinished evidence\n")
        task = kb.create_task(conn, title="Obsolete preflight fixture", assignee="maker", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
                         (str(workspace), task))
        receipt = execute(dict(action="archive", task_id=task,
                               reason="obsolete isolated preflight fixture", **describe(conn, task)))
        require(receipt.get("ok") and receipt.get("status") == "archived", "Native archive failed")
        require(artifact.read_text() == "unfinished evidence\n", "Archive deleted evidence")
        require(kb.get_task(conn, task).completed_at is None, "Archive fabricated completion")
        receipt = execute(dict(action="restore", task_id=task,
                               reason="owner restores fixture", **describe(conn, task)))
        require(receipt.get("ok") and receipt.get("status") == "triage", "Native restore lost lane")
        kb.recompute_ready(conn)
        require(kb.get_task(conn, task).status == "triage", "Restore resumed paused work")

        parent = kb.create_task(conn, title="Required input", assignee="maker")
        child = kb.create_task(conn, title="Waiting fixture", assignee="maker", parents=[parent])
        require(kb.get_task(conn, child).status == "todo", "Unfinished prerequisite did not gate child")
        try:
            kb.archive_task(conn, parent, reason="obsolete isolated fixture")
        except PermissionError:
            pass
        else:
            raise RuntimeError("Unfinished live prerequisite was archivable")
        # Fixture state stands for an already verified completion; no worker/model is run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', completed_at=1 WHERE id=?", (parent,))
        kb.recompute_ready(conn)
        require(kb.get_task(conn, child).status == "ready", "Completed prerequisite did not release child")
        require(kb.recompute_ready(conn) == 0, "Dependency reconciliation was not idempotent")

        triage = kb.create_task(conn, title="Routine local correction", assignee="maker", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind='capability', consecutive_failures=1 WHERE id=?", (triage,))
            kb._append_event(conn, triage, "blocked", {"reason": "Local tool unavailable"})
            conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=? AND kind='blocked'", (triage,))
        stop = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='blocked'", (triage,)).fetchone()[0]
        kb.add_comment(conn, triage, "owner", "Resume authorized local correction; tool repair verified")
        latest = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (triage,)).fetchone()[0]
        evidence = dict(expected_stop_event_id=stop, authorization_comment_id=latest,
                        expected_latest_comment_id=latest,
                        expected_task_sha256=kb.triage_snapshot_sha(asdict(kb.get_task(conn, triage))),
                        author="owner", reason="Tool repair verified")
        operation = dict(action="resume_triage", task_id=triage, reason="Tool repair verified",
                         expected_state=describe(conn, triage)["expected_state"], evidence=evidence)
        receipt = execute(dict(operation, dry_run=True))
        require(receipt.get("ok") and receipt.get("dry_run"), "Native continuation dry-run failed")
        require(kb.get_task(conn, triage).status == "triage", "Dry-run mutated task")
        receipt = execute(operation)
        require(receipt.get("ok") and receipt.get("status") == "ready", "Native continuation failed")
        require(kb.get_task(conn, triage).consecutive_failures == 1, "Continuation erased failure history")
    return ["native_archive_preserves_evidence", "native_restore_preserves_pause", "dependency_release",
            "unfinished_prerequisite_retained", "native_evidence_bound_continuation"]


def gateway_probe():
    import tools.kanban_tools  # actual module import registers the tool
    from tools.registry import registry

    entry = registry.get_entry("kanban_manage")
    require(entry is not None, "kanban_manage is not registered")
    require(callable(entry.handler), "kanban_manage has no runtime handler")
    require(entry.check_fn is not None and entry.check_fn(), "Owner cannot discover kanban_manage")
    definitions = registry.get_definitions({"kanban_manage"}, quiet=True)
    require(len(definitions) == 1, "Owner schema resolution did not expose kanban_manage")
    return ["native_manage_registration", "owner_tool_discovery"]


def isolated_child(lane, root):
    # Hard fail network/process attempts. Hooks are installed before release imports.
    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind",
                     "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.exec"}:
            raise RuntimeError(f"Preflight prohibits {event}")
    sys.addaudithook(audit)
    sys.path.insert(0, str(root))
    checks = gateway_probe() if lane == "gateway" else controller_probe()
    print(json.dumps({"lane": lane, "ok": True, "checks": checks}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path)
    parser.add_argument("--controller", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--_probe", choices=("gateway", "controller"), help=argparse.SUPPRESS)
    parser.add_argument("--_root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._probe:
        isolated_child(args._probe, args._root)
        return
    roots = {lane: path.resolve(strict=True) for lane in ("gateway", "controller")
             if (path := getattr(args, lane)) is not None}
    require(roots, "Specify at least one gateway/controller candidate")
    require(1 <= args.timeout <= 60, "Timeout must be 1 to 60 seconds per lane")
    if args.manifest:
        verify_manifest(args.manifest.resolve(strict=True), roots)
    results = []
    for lane, root in roots.items():
        with tempfile.TemporaryDirectory(prefix="hermes-board-preflight-") as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
            environment = {"PATH": "/usr/bin:/bin", "HOME": tmp, "HERMES_HOME": tmp,
                           "HERMES_KANBAN_DB": str(home / "board.db"),
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                           "LANG": "C.UTF-8", "TZ": "UTC"}
            # Preserve the venv launcher path; resolving its symlink loses pyvenv.cfg.
            result = subprocess.run([str(args.python.absolute()), "-P", str(Path(__file__).resolve()),
                                     "--_probe", lane, "--_root", str(root)],
                                    cwd=tmp, env=environment, capture_output=True, text=True,
                                    timeout=args.timeout, check=False)
            if result.returncode:
                # The environment is secret-free; retain bounded diagnostics for repair.
                raise RuntimeError(f"{lane} preflight failed:\n{result.stderr[-4000:]}\n{result.stdout[-1000:]}")
            lines = result.stdout.strip().splitlines()
            require(lines, f"{lane} preflight returned no receipt")
            results.append(json.loads(lines[-1]))
    print(json.dumps({"ok": True, "offline": True, "lanes": results}, indent=2))


if __name__ == "__main__":
    main()

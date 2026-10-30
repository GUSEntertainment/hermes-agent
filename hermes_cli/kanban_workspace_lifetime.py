"""Read-only lifetime checks for task-owned Kanban workspaces.

A path reference prevents deletion; it never grants access, copies files, or
changes a dependency. Owners use the existing scoped preparation handoff to
replace references with verified task-owned input snapshots.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TERMINAL = ("done", "archived", "failed", "cancelled")


def _path_forms(value: str, base: str | None = None) -> set[Path]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        if not base:
            return set()
        path = Path(base).expanduser() / path
    forms = {Path(os.path.abspath(path))}
    try:
        forms.add(path.resolve(strict=False))
    except (OSError, RuntimeError):
        # Keep the lexical form for missing paths or symlink loops. Failure to
        # resolve a path inside the candidate workspace must not permit deletion.
        pass
    return forms


def active_references(conn, task_id: str, workspace: str) -> list[str]:
    """Find active tasks using an exact workspace or a path beneath it.

    Scans the board, rather than direct children alone: shared-workspace children
    can create grandchildren, and context links need the same file lifetime.
    An ancestor such as a broad home workspace is not an exact reference and
    does not retain every scratch directory on the board.
    """
    from hermes_cli.worker_readiness import requirements

    roots = _path_forms(workspace)
    references = []
    rows = conn.execute(
        "SELECT id,title,body,workspace_path FROM tasks WHERE id != ? "
        "AND status NOT IN ('done','archived','failed','cancelled')",
        (task_id,),
    )
    for row in rows:
        base = row["workspace_path"]
        paths = [(base, None)] if base else []
        try:
            inputs = requirements(row["title"] or "", row["body"] or "")
            paths.extend((value, base) for key in ("read_paths", "write_paths")
                         for value in inputs.get(key, []))
            if inputs.get("source") != "declared":
                for root in roots:
                    if re.search(re.escape(str(root)) + r"(?=$|[/\\\s`\"'])", row["body"] or ""):
                        paths.append((str(root), None))
        except (ValueError, TypeError):
            # A malformed requirement cannot turn an exact named source into
            # permission to delete it. This is retention only, not admission.
            for root in roots:
                if re.search(re.escape(str(root)) + r"(?=$|[/\\\s`\"'])", row["body"] or ""):
                    paths.append((str(root), None))
        if any(path == root or path.is_relative_to(root)
               for value, base in paths if value
               for path in _path_forms(value, base) for root in roots):
            references.append(row["id"])
    return sorted(references)

"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kbw._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def test_preclaim_cas_race_removes_new_materialization(kanban_home, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    kb.write_board_metadata("default", default_workdir=str(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="raced", assignee="worker", workspace_kind="worktree"
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        real_resolve = kb._resolve_worktree_workspace

        def resolve_then_race(candidate, **kwargs):
            resolved = real_resolve(candidate, **kwargs)
            other = kb.connect()
            try:
                with kb.write_txn(other):
                    other.execute(
                        "UPDATE tasks SET model_override='concurrent-model' WHERE id=?",
                        (tid,),
                    )
            finally:
                other.close()
            return resolved

        admission_calls = []
        spawn_calls = []
        monkeypatch.setattr(kb, "_resolve_worktree_workspace", resolve_then_race)
        monkeypatch.setattr(
            kb, "_build_worker_admission", lambda *a, **k: admission_calls.append(1)
        )

        def spawn(*args, admission_contract=None, **kwargs):
            spawn_calls.append(1)

        result = kb.dispatch_once(conn, spawn_fn=spawn)
        current = kb.get_task(conn, tid)
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()[0]
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?", (tid,)
            ).fetchall()
        ]

    target = repo / ".worktrees" / tid
    assert result.spawned == []
    assert admission_calls == []
    assert spawn_calls == []
    assert current is not None and current.status == "ready"
    assert current.current_run_id is None
    assert run_count == 0
    assert "claimed" not in event_kinds
    assert "spawned" not in event_kinds
    assert not target.exists()
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", f"wt/{tid}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == ""


def test_failed_worktree_add_cleans_partial_target_and_branch(
    kanban_home, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    target = repo / ".worktrees" / "partial"
    task = kb.Task(
        id="partial",
        title="partial",
        body=None,
        assignee="worker",
        status="ready",
        priority=0,
        created_by="user",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=str(target),
        branch_name="wt/partial",
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )

    def partial_add(repo_root, worktree_target, branch_name):
        worktree_target.mkdir(parents=True)
        (worktree_target / "partial-file").write_text("partial", encoding="utf-8")
        _git(repo_root, "branch", branch_name)
        raise RuntimeError("injected partial add")

    monkeypatch.setattr(kb, "_run_git_worktree_add", partial_add)
    with pytest.raises(RuntimeError, match="injected partial add"):
        kb._resolve_worktree_workspace(task)

    assert not target.exists()
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "wt/partial"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == ""





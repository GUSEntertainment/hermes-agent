"""Regression tests for dependency-gated Kanban progression."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _payloads(conn, task_id: str, kind: str) -> list[dict]:
    return [event.payload or {} for event in kb.list_events(conn, task_id) if event.kind == kind]


def test_done_parent_satisfies_dependency_gate(kanban_home, all_assignees_spawnable) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent prerequisite", assignee="owner")
        child = kb.create_task(conn, title="child follow-up", assignee="worker", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        assert kb.complete_task(conn, parent, summary="artifact delivered")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child, claimer="worker") is not None


def test_archived_parent_preserves_existing_terminal_dependency_contract(
    kanban_home, all_assignees_spawnable
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="obsolete prerequisite", assignee="owner")
        child = kb.create_task(conn, title="child follow-up", assignee="worker", parents=[parent])
        assert kb.archive_task(conn, parent)

        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child, claimer="worker") is not None


@pytest.mark.parametrize("block_kind", ["needs_input", "dependency"])
def test_accountable_blocked_parent_stays_unsatisfied_and_records_root(
    kanban_home, block_kind: str
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent prerequisite", assignee="owner")
        child = kb.create_task(conn, title="child follow-up", assignee="worker", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        if block_kind == "needs_input":
            assert kb.block_task(
                conn,
                parent,
                reason="waiting on accountable operator input",
                kind="needs_input",
            )
        else:
            # Legacy boards can contain dependency-typed blocked parents even
            # though current block_task routes dependency waits to todo.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', block_kind = 'dependency' WHERE id = ?",
                    (parent,),
                )

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child, claimer="worker") is None
        events = _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)
        assert len(events) == 1
        assert events[0]["roots"] == [
            {
                "root_id": parent,
                "reason": f"blocked_{block_kind}",
                "owner": "owner",
                "status": "blocked",
                "block_kind": block_kind,
                "edge_role": "dependency",
                "path": [child, parent],
            }
        ]


def test_dispatch_does_not_promote_child_after_accountable_parent_block(
    kanban_home, all_assignees_spawnable
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="needs human answer", assignee="owner")
        child = kb.create_task(conn, title="continue after answer request", assignee="worker", parents=[parent])
        assert kb.block_task(conn, parent, reason="needs_input: user choice", kind="needs_input")

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace_path, **kwargs: None,
            dry_run=True,
            max_spawn=4,
        )

        assert result.promoted == 0
        assert result.spawned == []
        assert kb.get_task(conn, child).status == "todo"
        assert _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)


def test_capability_parent_stays_unsatisfied_and_records_deduped_root(
    kanban_home,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="missing credential", assignee="owner")
        child = kb.create_task(conn, title="downstream work", assignee="worker", parents=[parent])
        assert kb.block_task(conn, parent, reason="missing token", kind="capability")

        assert kb.recompute_ready(conn) == 0
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"

        events = _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)
        assert len(events) == 1
        roots = events[0]["roots"]
        assert roots == [
            {
                "root_id": parent,
                "reason": "blocked_capability",
                "owner": "owner",
                "status": "blocked",
                "block_kind": "capability",
                "edge_role": "dependency",
                "path": [child, parent],
            }
        ]
        assert events[0]["truncated"] is False
        assert events[0]["signature"]



def test_hardening_gate_is_a_completion_dependency(kanban_home, all_assignees_spawnable) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="hardening audit", assignee="owner")
        child = kb.create_task(
            conn,
            title="release after hardening",
            assignee="worker",
            parents=[parent],
            parent_edge_role="hardening_gate",
        )

        assert kb.get_task(conn, child).status == "todo"
        assert kb.block_task(conn, parent, reason="needs hardening evidence", kind="needs_input")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        assert _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)[0]["roots"][0]["edge_role"] == "hardening_gate"

        assert kb.unblock_task(conn, parent)
        assert kb.complete_task(conn, parent, summary="hardening evidence delivered")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child, claimer="worker") is not None

def test_legacy_delegation_links_preserve_prerequisite_readiness(
    kanban_home, all_assignees_spawnable
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="maker owner", assignee="owner")
        child = kb.create_task(
            conn,
            title="delegated maker lane",
            assignee="worker",
            parents=[parent],
            parent_edge_role="delegation_maker",
        )

        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, child, claimer="worker") is None


def test_linking_legacy_blocked_delegation_parent_demotes_ready_child(
    kanban_home,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="delegation owner", assignee="owner")
        child = kb.create_task(conn, title="ready independent work", assignee="worker")
        assert kb.block_task(conn, parent, reason="owner needs input", kind="needs_input")
        assert kb.get_task(conn, child).status == "ready"

        kb.link_tasks(conn, parent, child, edge_role="delegation_reviewer")

        assert kb.get_task(conn, child).status == "todo"
        assert not _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)


def test_linking_blocked_dependency_parent_demotes_ready_child(
    kanban_home,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="real prerequisite", assignee="owner")
        child = kb.create_task(conn, title="ready until linked", assignee="worker")
        assert kb.block_task(conn, parent, reason="owner needs input", kind="needs_input")

        kb.link_tasks(conn, parent, child, edge_role="dependency")

        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child, claimer="worker") is None


def test_dangling_dependency_edge_never_counts_as_satisfied_and_names_missing_root(
    kanban_home,
) -> None:
    with kb.connect_closing() as conn:
        child = kb.create_task(conn, title="legacy child", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id, edge_role) VALUES (?, ?, ?)",
                ("t_missing_parent", child, "dependency"),
            )

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        events = _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)
        assert len(events) == 1
        assert events[0]["roots"][0] == {
            "root_id": "t_missing_parent",
            "reason": "missing_parent",
            "owner": None,
            "status": None,
            "block_kind": None,
            "edge_role": "dependency",
            "path": [child, "t_missing_parent"],
        }


def test_dangling_delegation_edge_fails_closed(kanban_home) -> None:
    with kb.connect_closing() as conn:
        child = kb.create_task(conn, title="legacy delegated child", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id, edge_role) VALUES (?, ?, ?)",
                ("t_missing_parent", child, "delegation_maker"),
            )

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        assert _payloads(conn, child, kb.DEPENDENCY_STALE_CHAIN_EVENT)


def test_invalid_link_roles_are_rejected_before_persistence(kanban_home) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="owner")
        child = kb.create_task(conn, title="child", assignee="worker")

        with pytest.raises(ValueError, match="edge_role"):
            kb.link_tasks(conn, parent, child, edge_role="notify")
        with pytest.raises(ValueError, match="edge_role"):
            kb.create_task(
                conn,
                title="bad edge child",
                assignee="worker",
                parents=[parent],
                parent_edge_role="notify",
            )
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE edge_role = 'notify'").fetchone()[0] == 0


def test_manual_dependency_cycle_is_reported_without_promotion(kanban_home) -> None:
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="owner")
        second = kb.create_task(conn, title="second", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id IN (?, ?)", (first, second))
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id, edge_role) VALUES (?, ?, 'dependency')",
                (first, second),
            )
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id, edge_role) VALUES (?, ?, 'dependency')",
                (second, first),
            )

        assert kb.recompute_ready(conn) == 0
        for task_id in (first, second):
            task = kb.get_task(conn, task_id)
            assert task.status == "todo"
            roots = _payloads(conn, task_id, kb.DEPENDENCY_STALE_CHAIN_EVENT)[0]["roots"]
            assert roots[0]["reason"] == "cycle"
            assert roots[0]["root_id"] == task_id


def test_verified_owner_cell_delegation_can_start_before_owner_done(kanban_home, all_assignees_spawnable):
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="owner", assignee="scarlett", owner_cell_contract=contract(command_board_flow=True, delegation_roles={"maker": "engineering-wren", "reviewer": "grace"}))
        child = kb.create_task(conn, title="maker", assignee="engineering-wren", parents=[parent], parent_edge_role="delegation_maker")
        assert kb.get_task(conn, child).status == "ready"
        assert kb._parents_satisfied(conn, child)
        assert kb.claim_task(conn, child, claimer="engineering-wren") is not None


def test_verified_fanout_stays_released_during_kernel_recorded_review(kanban_home, all_assignees_spawnable):
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract
    with kb.connect_closing() as conn:
        parent=kb.create_task(conn,title='owner',assignee='scarlett',owner_cell_contract=contract(command_board_flow=True,delegation_roles={'maker':'engineering-wren','reviewer':'grace'}))
        child=kb.create_task(conn,title='maker',assignee='engineering-wren',parents=[parent],parent_edge_role='delegation_maker')
        run=kb.claim_task(conn,child,claimer='engineering-wren')
        assert kb.request_review(conn,child,summary='Maker artifact and tests complete',reviewer='grace',expected_run_id=run.current_run_id)
        assert kb.get_task(conn,child).assignee=='grace'
        assert kb._parents_satisfied(conn,child), 'future parent completion must not re-block its valid maker review'


def test_role_reassignment_without_review_provenance_does_not_bypass_parent(kanban_home,all_assignees_spawnable):
    from tests.hermes_cli.test_owner_cell_qa_regressions import contract
    with kb.connect_closing() as conn:
        parent=kb.create_task(conn,title='owner',assignee='scarlett',owner_cell_contract=contract(command_board_flow=True,delegation_roles={'maker':'engineering-wren','reviewer':'grace'}))
        child=kb.create_task(conn,title='maker',assignee='engineering-wren',parents=[parent],parent_edge_role='delegation_maker')
        assert kb.reassign_task(conn,child,'grace')
        assert not kb._parents_satisfied(conn,child)

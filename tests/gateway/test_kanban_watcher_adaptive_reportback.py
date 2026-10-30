from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class _DeskCheckingAdapter:
    def __init__(self, desk: Path):
        self.desk = desk
        self.sent: list[str] = []

    async def send(self, chat_id, text, metadata=None):
        assert list(self.desk.glob("*.json")), "Desk materialization must precede send"
        self.sent.append(text)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _routing_body(**overrides):
    metadata = {
        "audience": "chris",
        "requesting_user": "chris",
        "cockpit_board": "default",
        "source_session_key": "agent-os:cockpit:chris",
        "source_surface": "agent-os:cockpit:voice",
        "return_surface": "agent-os:cockpit:desk",
        "department_id": "ops",
    }
    metadata.update(overrides)
    return "## Routing metadata\n```json\n" + json.dumps(metadata) + "\n```\n"


def _report_config(*, malformed=False, external=False):
    report_back = {
        "enabled": True,
        "dry_run": False,
        "principal_registry": {
            "principals": {
                "auth:chris": {"audience": "chris", "boards": ["default"]}
            }
        },
        "department_registry": {
            "departments": {
                "ops": {
                    "privacy_tier": "team",
                    "report_back_policy": {
                        "mode": "telegram_department_topic" if external else "desk_only"
                    },
                    "department_destination": {
                        "telegram_department_topic": {
                            "enabled": external,
                            "chat_id": "chat-1",
                        }
                    },
                }
            }
        },
    }
    if malformed:
        report_back.pop("principal_registry")
    return {
        "kanban": {
            "command_boards": ["default"],
            "report_back": report_back,
        }
    }


def test_notifier_materializes_root_report_before_single_human_delivery(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "report-notifier.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="private root",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-2",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, task_id, summary="done for person@example.com")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: _report_config(external=True)
    )
    adapter = _DeskCheckingAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "[REDACTED_EMAIL]" in adapter.sent[0]
    assert "person@example.com" not in adapter.sent[0]
    records = list(desk.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text())
    assert payload["package"]["audience"] == "chris"
    assert "person@example.com" not in payload["package"]["summary"]
    verify = kb.connect()
    try:
        kinds = [row[0] for row in verify.execute(
            "SELECT kind FROM task_events WHERE kind LIKE 'report_back_%' ORDER BY id"
        )]
        assert kinds == ["report_back_desk_updated", "report_back_telegram_claimed"]
        cursors = verify.execute(
            "SELECT chat_id, last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? ORDER BY chat_id",
            (task_id,),
        ).fetchall()
        completed_id = verify.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        ).fetchone()[0]
        assert [(row[0], row[1] >= completed_id) for row in cursors] == [
            ("chat-1", True),
            ("chat-2", True),
        ]
    finally:
        verify.close()


def test_desk_write_failure_rewinds_and_retry_materializes_before_delivery(
    tmp_path, monkeypatch
):
    import gateway.kanban_report_back as report_back
    import hermes_cli.config as config_module

    db_path = tmp_path / "desk-write-retry.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="root", body=_routing_body(), assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn, task_id=task_id, platform="telegram", chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, task_id, summary="durable first")
    finally:
        conn.close()

    monkeypatch.setattr(config_module, "load_config", lambda: _report_config(external=True))
    real_write = report_back._write_desk_record
    writes = 0

    def fail_first_write(decision, board):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("injected Desk write failure")
        return real_write(decision, board)

    monkeypatch.setattr(report_back, "_write_desk_record", fail_first_write)
    adapter = _DeskCheckingAdapter(desk)
    runner = _runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.sent == []
    assert not list(desk.glob("*.json"))
    verify = kb.connect()
    try:
        assert verify.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind LIKE 'report_back_%'"
        ).fetchone()[0] == 0
        assert verify.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'report_back_claims'"
        ).fetchone()[0] == 0
    finally:
        verify.close()

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert writes == 2
    assert len(adapter.sent) == 1
    assert len(list(desk.glob("*.json"))) == 1


def test_duplicate_claim_with_missing_desk_record_rematerializes_without_raw_fallback(
    tmp_path, monkeypatch
):
    import hermes_cli.config as config_module

    db_path = tmp_path / "missing-desk-duplicate-claim.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="root", body=_routing_body(), assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        for chat_id in ("chat-1", "chat-2"):
            kb.add_notify_sub(
                conn, task_id=task_id, platform="telegram", chat_id=chat_id,
                delivery_metadata={"authenticated_principal": "auth:chris"},
            )
        kb.complete_task(conn, task_id, summary="canonical only")
    finally:
        conn.close()

    monkeypatch.setattr(config_module, "load_config", lambda: _report_config())

    class NoRawFallbackAdapter(_DeskCheckingAdapter):
        async def send(self, chat_id, text, metadata=None):
            raise AssertionError("Desk-only retry must not use the legacy raw formatter")

    adapter = NoRawFallbackAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    records = list(desk.glob("*.json"))
    assert len(records) == 1
    records[0].unlink()

    rewind = kb.connect()
    try:
        rewind.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 0 WHERE task_id = ?",
            (task_id,),
        )
        rewind.commit()
    finally:
        rewind.close()

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(list(desk.glob("*.json"))) == 1
    verify = kb.connect()
    try:
        kinds = [row[0] for row in verify.execute(
            "SELECT kind FROM task_events WHERE kind LIKE 'report_back_%' ORDER BY id"
        )]
        assert kinds == ["report_back_desk_updated", "report_back_telegram_skipped"]
        completed_id = verify.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        ).fetchone()[0]
        assert all(row[0] >= completed_id for row in verify.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
        ))
    finally:
        verify.close()


def test_crash_after_desk_replace_before_db_claim_reuses_artifact_on_retry(
    tmp_path, monkeypatch
):
    import gateway.kanban_report_back as report_back

    db_path = tmp_path / "desk-before-claim-crash.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="root", body=_routing_body(), assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.complete_task(conn, task_id, summary="survives boundary")
        task = kb.get_task(conn, task_id)
        event = kb.list_events(conn, task_id)[-1]
        settings = _report_config(external=True)["kanban"]["report_back"]
        real_claim = report_back._claim_and_record

        def crash_before_claim(*args, **kwargs):
            raise RuntimeError("injected crash after Desk replace")

        monkeypatch.setattr(report_back, "_claim_and_record", crash_before_claim)
        with pytest.raises(RuntimeError, match="after Desk replace"):
            report_back.record_report_back_dry_run(
                conn, task=task, event=event, board="default",
                authenticated_principal="auth:chris",
                principal_registry=settings["principal_registry"],
                department_registry=settings["department_registry"], dry_run=False,
            )
        assert len(list(desk.glob("*.json"))) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind LIKE 'report_back_%'"
        ).fetchone()[0] == 0

        monkeypatch.setattr(report_back, "_claim_and_record", real_claim)
        report_back.record_report_back_dry_run(
            conn, task=task, event=event, board="default",
            authenticated_principal="auth:chris",
            principal_registry=settings["principal_registry"],
            department_registry=settings["department_registry"], dry_run=False,
        )
        assert len(list(desk.glob("*.json"))) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind LIKE 'report_back_%'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_malformed_cockpit_dependencies_rewind_before_desk_send_or_finalize(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "malformed-report.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="root",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, task_id, summary="done")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: _report_config(malformed=True)
    )
    adapter = _DeskCheckingAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert not desk.exists()
    verify = kb.connect()
    try:
        cursor = verify.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        completed_id = verify.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        ).fetchone()[0]
        assert cursor < completed_id
        assert verify.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind LIKE 'report_back_%'"
        ).fetchone()[0] == 0
    finally:
        verify.close()


def test_ambiguous_human_send_is_never_attempted_twice(tmp_path, monkeypatch):
    db_path = tmp_path / "ambiguous-send.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="root",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, task_id, summary="done")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: _report_config(external=True)
    )

    class AmbiguousAdapter(_DeskCheckingAdapter):
        async def send(self, chat_id, text, metadata=None):
            assert list(self.desk.glob("*.json"))
            self.sent.append(text)
            raise RuntimeError("connection lost after remote acceptance")

    adapter = AmbiguousAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    verify = kb.connect()
    try:
        last_event_id = verify.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        completed_id = verify.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        ).fetchone()[0]
        assert last_event_id >= completed_id
    finally:
        verify.close()


def test_enabled_report_back_acknowledges_child_without_external_delivery(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "child-report.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        kb.complete_task(conn, parent, summary="parent done")
        child = kb.create_task(
            conn,
            title="child",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
            parents=[parent],
        )
        kb.add_notify_sub(
            conn,
            task_id=child,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, child, summary="child done")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(config_module, "load_config", lambda: _report_config())

    class _PlainAdapter(_DeskCheckingAdapter):
        async def send(self, chat_id, text, metadata=None):
            self.sent.append(text)

    adapter = _PlainAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.sent == []
    assert not desk.exists()
    verify = kb.connect()
    try:
        cursor = verify.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (child,)
        ).fetchone()[0]
        completed_id = verify.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (child,),
        ).fetchone()[0]
        assert cursor >= completed_id
    finally:
        verify.close()


def test_disabled_report_back_preserves_legacy_child_notification(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-child.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        kb.complete_task(conn, parent, summary="parent done")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        kb.add_notify_sub(conn, task_id=child, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, child, summary="legacy child detail")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {}})

    class _PlainAdapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, metadata=None):
            self.sent.append(text)

    adapter = _PlainAdapter()
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "legacy child detail" in adapter.sent[0]


def test_trusted_block_reason_is_redacted_before_external_delivery(tmp_path, monkeypatch):
    db_path = tmp_path / "blocked-redaction.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="root",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.block_task(conn, task_id, reason="contact person@example.com")
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: _report_config(external=True)
    )
    adapter = _DeskCheckingAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "[REDACTED_EMAIL]" in adapter.sent[0]
    assert "person@example.com" not in adapter.sent[0]


def test_trusted_task_result_fallback_is_redacted_before_external_delivery(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "result-redaction.db"
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="root",
            body=_routing_body(),
            assignee="worker",
            tenant="agent-os:cockpit:voice",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"authenticated_principal": "auth:chris"},
        )
        kb.complete_task(conn, task_id, result="result for person@example.com")
        conn.execute(
            "UPDATE task_events SET payload = '{}' "
            "WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: _report_config(external=True)
    )
    adapter = _DeskCheckingAdapter(desk)
    runner = _runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "[REDACTED_EMAIL]" in adapter.sent[0]
    assert "person@example.com" not in adapter.sent[0]


def test_dispatcher_samples_once_and_spends_one_global_allowance_across_command_boards(
    tmp_path, monkeypatch
):
    import gateway.kanban_admission as admission
    import gateway.kanban_watchers as watchers
    import hermes_cli.config as config_module

    config = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": False,
            "command_boards": ["alpha", "beta"],
            "max_in_progress": 99,
            "adaptive_concurrency": {
                "enabled": True,
                "maximum_capacity": 4,
                "recovery_samples": 2,
                "growth_step": 1,
            },
        }
    }
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (object(), "held")
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    samples = []
    monkeypatch.setattr(
        admission,
        "sample_host_pressure",
        lambda: samples.append(True) or admission.HostPressure(0.1, "normal", "normal"),
    )
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _conn: False)
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("allowlist bypassed")),
    )

    active = {"alpha": 1, "beta": 1}
    calls = []

    class FakeConn:
        def __init__(self, slug):
            self.slug = slug

        def execute(self, _sql):
            return SimpleNamespace(fetchone=lambda: (active[self.slug],))

        def close(self):
            pass

    monkeypatch.setattr(kb, "connect", lambda *, board=None: FakeConn(board))

    def dispatch_once(_conn, *, board, max_in_progress, **_kwargs):
        calls.append((board, max_in_progress))
        spawned = ["a", "b"] if board == "alpha" else []
        return SimpleNamespace(
            spawned=spawned,
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    monkeypatch.setattr(kb, "dispatch_once", dispatch_once)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True

    real_sleep = asyncio.sleep
    sleeps = []

    async def fake_sleep(_delay):
        sleeps.append(True)
        if len(sleeps) > 1:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(samples) == 1
    assert calls == [("alpha", 3), ("beta", 1)]


def test_malformed_enabled_adaptive_config_fails_closed_before_dispatch(
    tmp_path, monkeypatch
):
    import gateway.kanban_watchers as watchers
    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "adaptive_concurrency": {"enabled": True, "maximum_capacity": "many"},
            }
        },
    )
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (object(), "held")
    )
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(kb, "dispatch_once", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "list_boards", lambda **_kwargs: [])
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    real_sleep = asyncio.sleep
    sleeps = []

    async def fake_sleep(_delay):
        sleeps.append(True)
        if len(sleeps) > 1:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_dispatcher_watcher())
    assert calls == []

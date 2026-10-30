import json
import threading
from types import SimpleNamespace

import pytest

import agent.redact as canonical_redact
from hermes_cli import kanban_db as kb


_REPORT_CREDENTIALS = (
    "sk-proj-" + "A" * 24,
    "sk-or-v1-" + "B" * 24,
    "sk-" + "C" * 24,
    "sk-ant-api03-" + "D" * 24,
    "ghp_" + "E" * 24,
    "github_pat_" + "F" * 24,
    "sk_" + "G" * 24,
    "xoxb-" + "H" * 24,
)


def _body(metadata):
    return "## Routing metadata\n```json\n" + json.dumps(metadata) + "\n```\n"


def _event(task_id, *, event_id=7, summary="done"):
    return SimpleNamespace(
        id=event_id,
        task_id=task_id,
        kind="completed",
        payload={"summary": summary},
        created_at=123,
        run_id=None,
    )


def _registry():
    return {
        "principals": {
            "auth:chris": {"audience": "chris", "boards": ["chris-home"]},
            "auth:connor": {"audience": "connor", "boards": ["connor-home"]},
        }
    }


def _task(task_id="t_report", metadata=None):
    return SimpleNamespace(
        id=task_id,
        title="Private report",
        body=_body(metadata or {}),
        result="done",
        assignee="scarlett",
        tenant="agent-os:cockpit:voice",
        security_realm="team",
    )


def test_cockpit_audience_is_bound_to_authenticated_principal_not_task_metadata():
    from gateway.kanban_report_back import ReportBackPolicyError, resolve_report_back_decision

    task = _task(metadata={
        "audience": "connor",
        "requesting_user": "connor",
        "cockpit_board": "connor-home",
        "source_session_key": "agent-os:cockpit:connor",
        "source_surface": "agent-os:cockpit:voice",
    })

    with pytest.raises(ReportBackPolicyError, match="authenticated principal"):
        resolve_report_back_decision(
            task=task,
            event=_event(task.id),
            board="connor-home",
            authenticated_principal="auth:chris",
            principal_registry=_registry(),
        )


def test_cockpit_report_fails_closed_when_principal_registry_missing_or_malformed(tmp_path, monkeypatch):
    from gateway.kanban_report_back import ReportBackPolicyError, record_report_back_dry_run

    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    db = tmp_path / "report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    task = _task(metadata={
        "audience": "chris",
        "cockpit_board": "chris-home",
        "source_session_key": "agent-os:cockpit:chris",
        "source_surface": "agent-os:cockpit:voice",
    })
    try:
        for registry in (None, {}, {"principals": []}, {"principals": {"auth:chris": "bad"}}):
            with pytest.raises(ReportBackPolicyError):
                record_report_back_dry_run(
                    conn,
                    task=task,
                    event=_event(task.id),
                    board="chris-home",
                    authenticated_principal="auth:chris",
                    principal_registry=registry,
                )
        assert not desk.exists()
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_privacy_and_destination_are_registry_owned_not_reporter_spoofed():
    from gateway.kanban_report_back import resolve_report_back_decision

    task = _task(metadata={
        "department_id": "legal",
        "privacy_tier": "public",
        "department_destination": {
            "telegram_department_topic": {"enabled": True, "chat_id": "attacker", "topic_id": 999}
        },
    })
    task.tenant = "manual"
    registry = {
        "departments": {
            "legal": {
                "privacy_tier": "legal",
                "department_owner": "Counsel",
                "report_back_policy": {"mode": "desk_only"},
                "department_destination": {
                    "telegram_department_topic": {"enabled": False, "chat_id": "trusted", "topic_id": 4}
                },
            }
        }
    }

    decision = resolve_report_back_decision(
        task=task,
        event=_event(task.id, summary="email person@example.com password=hunter2"),
        board="legacy",
        department_registry=registry,
    )

    assert decision.privacy_tier == "legal"
    assert decision.telegram_destination["chat_id"] == "trusted"
    assert decision.telegram_allowed is False
    assert "person@example.com" not in decision.redacted_summary
    assert "hunter2" not in decision.redacted_summary


@pytest.mark.parametrize("privacy_tier", ["public", "team", "legal"])
def test_report_summary_forces_canonical_credential_redaction_across_privacy_tiers(
    monkeypatch, privacy_tier
):
    from gateway.kanban_report_back import resolve_report_back_decision

    monkeypatch.setattr(canonical_redact, "_REDACT_ENABLED", False)
    task = _task()
    task.tenant = "manual"
    task.security_realm = privacy_tier
    summary = "credentials " + " ".join(_REPORT_CREDENTIALS) + "\nprivate follow-up detail"

    decision = resolve_report_back_decision(
        task=task,
        event=_event(task.id, summary=summary),
        board="legacy",
    )

    assert decision is not None
    assert all(secret not in decision.redacted_summary for secret in _REPORT_CREDENTIALS)
    if privacy_tier in {"public", "team"}:
        assert "private follow-up detail" in decision.redacted_summary
    else:
        assert "private follow-up detail" not in decision.redacted_summary
    assert decision.telegram_allowed is False
    assert decision.telegram_reason == "sensitive report-back remains Desk-only"


def test_canonical_redaction_reaches_summary_artifact_and_event_preview_boundaries(
    tmp_path, monkeypatch
):
    from gateway.kanban_report_back import record_report_back_dry_run

    monkeypatch.setattr(canonical_redact, "_REDACT_ENABLED", False)
    desk = tmp_path / "desk"
    monkeypatch.setenv("HERMES_DESK_DIR", str(desk))
    db = tmp_path / "report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    task = _task()
    task.tenant = "manual"
    summary = "credentials " + " ".join(_REPORT_CREDENTIALS) + " password=hunter2"
    try:
        decision = record_report_back_dry_run(
            conn,
            task=task,
            event=_event(task.id, summary=summary),
            board="legacy",
            dry_run=False,
        )

        assert decision is not None
        serialized_summary = decision.redacted_summary
        artifact = json.loads(next(desk.glob("*.json")).read_text(encoding="utf-8"))
        artifact_summary = artifact["package"]["summary"]
        previews = [
            json.loads(row[0])["message_preview"]
            for row in conn.execute(
                "SELECT payload FROM task_events WHERE kind LIKE 'report_back_%' ORDER BY id"
            )
        ]
        assert artifact_summary == serialized_summary
        assert previews == [serialized_summary, serialized_summary]
        for produced_text in (serialized_summary, artifact_summary, *previews):
            assert "hunter2" not in produced_text
            assert all(secret not in produced_text for secret in _REPORT_CREDENTIALS)
    finally:
        conn.close()


def test_concurrent_duplicate_report_records_one_desk_and_one_preview(tmp_path, monkeypatch):
    from gateway.kanban_report_back import record_report_back_dry_run

    db = tmp_path / "concurrent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    setup = kb.connect()
    task_id = kb.create_task(setup, title="dedupe", assignee="scarlett")
    task = kb.get_task(setup, task_id)
    setup.close()
    event = _event(task_id)
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        conn = kb.connect()
        try:
            barrier.wait()
            record_report_back_dry_run(conn, task=task, event=event, board="legacy")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    verify = kb.connect()
    try:
        kinds = [row[0] for row in verify.execute(
            "SELECT kind FROM task_events WHERE kind LIKE 'report_back_%' ORDER BY id"
        )]
        assert kinds == ["report_back_desk_updated", "report_back_telegram_skipped"]
        claims = verify.execute("SELECT target, state FROM report_back_claims ORDER BY target").fetchall()
        assert [(row[0], row[1]) for row in claims] == [
            ("desk", "recorded"),
            ("telegram_department_topic", "recorded"),
        ]
    finally:
        verify.close()


def test_report_back_module_has_no_platform_adapter_send_surface():
    import gateway.kanban_report_back as report_back

    source = open(report_back.__file__, encoding="utf-8").read()
    assert "platforms" not in source
    assert ".send(" not in source
    assert "send_message" not in source

import json

import gateway.kanban_report_back as rb
from gateway.kanban_report_back import (
    is_reportable_event,
    record_report_back_dry_run,
    redact_report_text,
    resolve_report_back_decision,
)
from hermes_cli import kanban_db as kb


def _event_rows(conn, task_id):
    return conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()


def _events(conn, task_id):
    rows = _event_rows(conn, task_id)
    out = []
    for row in rows:
        out.append((row["kind"], json.loads(row["payload"]) if row["payload"] else {}))
    return out


def _last_source_event(conn, task_id, kind="completed"):
    row = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"]) if row["payload"] else None
    return kb.Event(
        id=row["id"],
        task_id=row["task_id"],
        kind=row["kind"],
        payload=payload,
        created_at=row["created_at"],
        run_id=row["run_id"],
    )


def _routing_body(meta):
    return (
        "Fixture task.\n\n## Routing metadata\n```json\n"
        + json.dumps(meta, separators=(",", ":"))
        + "\n```\n"
    )


def _base_live_meta(department_id="engineering_systems", assignee="scarlett", privacy_tier="normal"):
    return {
        "department_id": department_id,
        "department_owner": assignee.title(),
        "runtime_assignee": assignee,
        "privacy_tier": privacy_tier,
        "report_back_policy": {"mode": "milestone_only", "include": ["completed", "blocked", "needs_approval"]},
        "department_destination": {
            "telegram_department_topic": {
                "enabled": True,
                "dry_run_only": False,
                "chat_id": "dept-chat",
                "topic_id": 42,
                "topic_label": department_id,
            }
        },
    }


def _decision_for(meta, *, assignee="scarlett", result="done", kind="completed", payload=None, registry=None):
    tid = "t_fixture"
    task = type(
        "Task",
        (),
        {
            "id": tid,
            "title": "Fixture",
            "assignee": assignee,
            "body": _routing_body(meta),
            "result": result,
            "tenant": "telegram:source",
        },
    )()
    event = kb.Event(id=99, task_id=tid, kind=kind, payload=payload, created_at=123, run_id=None)
    return resolve_report_back_decision(task=task, event=event, board="test", registry=registry, dry_run=False)


def test_report_back_required_lane_and_event_fixtures():
    normal = _base_live_meta()
    assert _decision_for(normal).telegram_allowed is True
    assert _decision_for(normal, kind="blocked", payload={"reason": "needs input"}).telegram_allowed is True
    assert _decision_for(normal, kind="status", payload={"status": "needs_approval", "summary": "approve"}).telegram_allowed is True

    sensitive_cases = [
        ("accountant_finance", "maya", "finance", "Bank account/payment detail"),
        ("legal_compliance", "steven", "legal", "Privileged contract detail"),
        ("hiring_people", "harper", "personnel", "Candidate compensation detail"),
        ("creator_only", "geneva", "creator_only", "Creator-only talent lane note"),
        ("personal_family", "sienna", "family", "Family logistics note"),
    ]
    for department_id, assignee, privacy_tier, result in sensitive_cases:
        decision = _decision_for(
            _base_live_meta(department_id, assignee, privacy_tier),
            assignee=assignee,
            result=result,
        )
        assert decision is not None
        assert decision.telegram_allowed is False, department_id
        assert "Desk-only" in decision.telegram_reason
        assert len(decision.redacted_summary.splitlines()) <= 1

    credentials = _decision_for(_base_live_meta(), result="password=[SECRET]")
    assert credentials.telegram_allowed is False
    assert credentials.redacted_summary == "[REDACTED_SECRET]"


def test_sensitive_lane_can_only_live_post_with_explicit_positive_visibility_metadata():
    finance = _base_live_meta("ACCOUNTANT_FINANCE", "maya", "Finance")
    finance["privacy_gate"] = {"auto_post_allowed": True, "allowlist": ["department_topic_allowed"]}

    decision = _decision_for(finance, assignee="maya", result="Approved finance milestone")

    assert decision is not None
    assert decision.department_id == "accountant_finance"
    assert decision.privacy_tier == "finance"
    assert decision.telegram_allowed is True


def test_live_registry_sensitive_lanes_default_desk_only_unless_explicitly_approved(tmp_path, monkeypatch):
    topic_map = tmp_path / "margot-sensitive-topics.json"
    topic_map.write_text(json.dumps({
        "chat_id": "-1004479003480",
        "report_back_policy": {"mode": "milestone_only", "include": ["completed", "blocked", "needs_approval"]},
        "topics": [
            {"department_id": "accountant_finance", "department": "Finance", "topic_id": 21, "topic_name": "Finance — Jennifer", "voice_label": "Jennifer", "privacy": "sensitive"},
            {"department_id": "legal_compliance", "department": "Legal", "topic_id": 23, "topic_name": "Legal — Signer", "voice_label": "Signer", "privacy": "sensitive"},
            {"department_id": "hiring_people", "department": "Hiring + People", "topic_id": 19, "topic_name": "Hiring + People — Lauren", "voice_label": "Lauren", "privacy": "sensitive"},
            {"department_id": "engineering_systems", "department": "Engineering", "topic_id": 11, "topic_name": "Engineering + Systems — Scarlett", "voice_label": "Scarlett"},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("MARGOT_DEPARTMENT_TOPICS_PATH", str(topic_map))
    rb._TOPIC_REGISTRY_CACHE = None
    sensitive_cases = [
        ("maya", "Bank account/payment detail", False),
        ("steven", "Privileged contract detail", False),
        ("harper", "Candidate compensation detail", False),
        ("scarlett", "password=[SECRET]", False),
    ]
    for assignee, result, expected_allowed in sensitive_cases:
        task = type("Task", (), {"id": assignee, "title": assignee, "assignee": assignee, "body": "", "result": result, "tenant": "telegram:source"})()
        event = kb.Event(id=100, task_id=assignee, kind="completed", payload=None, created_at=123, run_id=None)
        decision = resolve_report_back_decision(task=task, event=event, board="test", dry_run=False)
        assert decision is not None
        assert decision.telegram_allowed is expected_allowed, (assignee, decision)
        assert "Desk-only" in decision.telegram_reason

    approved_meta = {
        "department_id": "accountant_finance",
        "privacy_tier": "finance",
        "privacy_gate": {"auto_post_allowed": True, "telegram_visibility": "approved"},
    }
    approved = _decision_for(approved_meta, assignee="maya", result="Approved finance milestone", kind="completed")
    assert approved is not None
    assert approved.telegram_allowed is True

    approved_flag_meta = {
        "department_id": "accountant_finance",
        "privacy_tier": "finance",
        "telegram_report_back_approved": True,
    }
    approved_flag = _decision_for(approved_flag_meta, assignee="maya", result="Approved finance milestone", kind="completed")
    assert approved_flag is not None
    assert approved_flag.telegram_allowed is True


def test_registry_policy_shape_normalizes_mirror_events_and_suppressions():
    registry = {
        "departments": {
            "engineering_systems": {
                "department_owner": "Scarlett",
                "runtime_assignee": "scarlett",
                "privacy_tier": "normal",
                "report_back_policy": {
                    "telegram_mirror_events": ["completed"],
                    "suppress": ["blocked"],
                },
                "department_destination": {
                    "telegram_department_topic": {"enabled": True, "dry_run_only": False, "chat_id": "dept", "topic_id": 1}
                },
            }
        }
    }
    meta = {"department_id": "engineering_systems"}

    assert _decision_for(meta, registry=registry).telegram_allowed is True
    blocked = _decision_for(meta, kind="blocked", payload={"reason": "x"}, registry=registry)
    assert blocked.telegram_allowed is False
    assert "excluded" in blocked.telegram_reason or "not included" in blocked.telegram_reason


def test_report_back_records_desk_before_department_topic_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "report-back-order.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        body = '''Build the thing.

## Routing metadata
```json
{"source_surface":"telegram:source-topic","department_id":"engineering_systems","department_owner":"Scarlett","runtime_assignee":"scarlett","privacy_tier":"normal","return_surface":"telegram:source-topic","desk_artifact":"desk://tasks/eng-1","report_back_policy":{"mode":"milestone_only","include":["completed","blocked","needs_approval"]},"department_destination":{"telegram_department_topic":{"enabled":true,"dry_run_only":true,"chat_id":"dept-chat","topic_id":42,"topic_label":"Engineering"}}}
```
'''
        tid = kb.create_task(conn, title="desk-first report", body=body, assignee="scarlett", tenant="telegram:source")
        assert kb.complete_task(conn, tid, summary="Artifact ready")
        task = kb.get_task(conn, tid)
        event = _last_source_event(conn, tid)

        decision = record_report_back_dry_run(conn, task=task, event=event, board="chris-home", dry_run=True)

        assert decision is not None
        events = _events(conn, tid)
        kinds = [kind for kind, _payload in events]
        assert kinds.index("report_back_desk_updated") < kinds.index("report_back_telegram_skipped")
        desk_payload = dict(events[kinds.index("report_back_desk_updated")][1])
        telegram_payload = dict(events[kinds.index("report_back_telegram_skipped")][1])
        assert desk_payload["canonical_before_telegram"] is True
        assert desk_payload["desk_artifact"]["artifact_id_or_url"] == "desk://tasks/eng-1"
        assert telegram_payload["target"] == "telegram_department_topic"
        assert telegram_payload["live_send_allowed"] is False
        assert telegram_payload["dry_run"] is True
        assert telegram_payload["message_preview"] == "Artifact ready"
        assert telegram_payload["telegram_destination"] == {
            "chat_id": "dept-chat",
            "topic_id": 42,
            "topic_label": "Engineering",
        }
    finally:
        conn.close()


def test_report_back_writes_desk_intake_record_with_artifact_copy(tmp_path, monkeypatch):
    db_path = tmp_path / "desk-intake.db"
    desk_dir = tmp_path / "desk"
    artifact = tmp_path / "draft.md"
    artifact.write_text("# Draft\n\nReady for review.\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("AGENT_OS_DESK_DIR", str(desk_dir))
    kb.init_db()
    conn = kb.connect()
    try:
        body = '''Build the thing.

## Routing metadata
```json
{"source_surface":"agent-os:cockpit:voice","department_id":"engineering_systems","department_owner":"Scarlett","runtime_assignee":"scarlett","privacy_tier":"normal","return_surface":"agent-os:cockpit:voice-callback","desk_artifact":"desk://tasks/eng-voice","report_back_policy":{"mode":"milestone_only","include":["completed"]},"department_destination":{"telegram_department_topic":{"enabled":true,"dry_run_only":true,"chat_id":"dept-chat","topic_id":42,"topic_label":"Engineering"}}}
```
'''
        tid = kb.create_task(conn, title="voice artifact handoff", body=body, assignee="scarlett", tenant="agent-os:cockpit:voice")
        assert kb.complete_task(conn, tid, summary="Draft is ready in Desk", metadata={"artifacts": [str(artifact)]})
        task = kb.get_task(conn, tid)
        event = _last_source_event(conn, tid)

        decision = record_report_back_dry_run(conn, task=task, event=event, board="margot-chat-lanes", dry_run=False)

        assert decision is not None
        records = list(desk_dir.glob("kanban-margot-chat-lanes-*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["package"]["sourceSurface"] == "other"
        assert record["package"]["sourceSurfaceId"] == "agent-os:cockpit:voice"
        assert record["package"]["returnSurface"] == "agent-os:cockpit:voice-callback"
        assert record["package"]["source_surface"] == "agent-os:cockpit:voice"
        assert record["artifacts"][0]["title"] == "draft.md"
        copied = desk_dir / record["versions"][0]["filePath"]
        assert copied.read_text(encoding="utf-8") == "# Draft\n\nReady for review.\n"
        events = _events(conn, tid)
        desk_payload = [payload for kind, payload in events if kind == "report_back_desk_updated"][-1]
        assert desk_payload["desk_artifact"]["record_path"] == str(records[0])
    finally:
        conn.close()


def test_report_back_uses_margot_topic_map_by_assignee(tmp_path, monkeypatch):
    db_path = tmp_path / "report-back-topic-map.db"
    topic_map = tmp_path / "margot-topics.json"
    topic_map.write_text(json.dumps({
        "chat_id": "-1004479003480",
        "report_back_policy": {"mode": "milestone_only", "include": ["completed", "blocked", "needs_approval"]},
        "topics": [{
            "department_id": "research",
            "department": "Research",
            "topic_id": 9,
            "topic_name": "Research — Riley",
            "voice_label": "Riley",
            "runtime_assignee": "riley",
            "icon_emoji": "🔎",
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("MARGOT_DEPARTMENT_TOPICS_PATH", str(topic_map))
    monkeypatch.setenv("AGENT_OS_DESK_DIR", str(tmp_path / "desk"))
    rb._TOPIC_REGISTRY_CACHE = None
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="research report", assignee="riley")
        assert kb.complete_task(conn, tid, summary="Research packet ready at /tmp/report.pdf")
        task = kb.get_task(conn, tid)
        event = _last_source_event(conn, tid)

        decision = record_report_back_dry_run(conn, task=task, event=event, board="chris-home", dry_run=False)

        assert decision is not None
        assert decision.department_id == "research"
        assert decision.department_owner == "Riley"
        assert decision.telegram_allowed is True
        assert decision.telegram_destination == {
            "enabled": True,
            "dry_run_only": False,
            "chat_id": "-1004479003480",
            "topic_id": 9,
            "topic_label": "Research — Riley",
            "icon_emoji": "🔎",
        }
        events = _events(conn, tid)
        previews = [payload for kind, payload in events if kind == "report_back_telegram_preview"]
        assert len(previews) == 1
        assert previews[0]["telegram_destination"] == {
            "chat_id": "-1004479003480",
            "topic_id": 9,
            "topic_label": "Research — Riley",
        }
        assert previews[0]["live_send_allowed"] is True
    finally:
        conn.close()


def test_report_back_idempotency_key_suppresses_duplicate_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "report-back-idempotency.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="dedupe", assignee="scarlett")
        assert kb.complete_task(conn, tid, summary="Done")
        task = kb.get_task(conn, tid)
        event = _last_source_event(conn, tid)

        record_report_back_dry_run(conn, task=task, event=event, board="default", dry_run=True)
        record_report_back_dry_run(conn, task=task, event=event, board="default", dry_run=True)

        report_events = [kind for kind, _payload in _events(conn, tid) if kind.startswith("report_back_")]
        assert report_events == ["report_back_desk_updated", "report_back_telegram_skipped"]
    finally:
        conn.close()


def test_report_back_ignores_worker_churn_events(tmp_path, monkeypatch):
    db_path = tmp_path / "report-back-noise.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="churn", assignee="scarlett")
        task = kb.get_task(conn, tid)
        noise_events = [
            kb.Event(id=10 + idx, task_id=tid, kind=kind, payload=None, created_at=123)
            for idx, kind in enumerate(["claimed", "heartbeat", "retry", "internal_comment", "child_created"])
        ]

        for noise_event in noise_events:
            assert not is_reportable_event(noise_event), noise_event.kind
            assert record_report_back_dry_run(conn, task=task, event=noise_event, board="default") is None
        assert [kind for kind, _payload in _events(conn, tid) if kind.startswith("report_back_")] == []
    finally:
        conn.close()


def test_sensitive_lanes_redact_and_stay_desk_only(tmp_path, monkeypatch):
    db_path = tmp_path / "report-back-sensitive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        body = '''Finance packet.

## Routing metadata
```json
{"department_id":"accountant_finance","department_owner":"Maya","runtime_assignee":"maya","privacy_tier":"finance","report_back_policy":{"mode":"milestone_only","include":["completed"]},"department_destination":{"telegram_department_topic":{"enabled":true,"dry_run_only":false,"chat_id":"finance-chat","topic_id":9}}}
```
'''
        tid = kb.create_task(conn, title="finance", body=body, assignee="maya")
        assert kb.complete_task(conn, tid, summary="Payment token: sk_test_1234567890abcdef1234567890abcdef for person@example.com")
        task = kb.get_task(conn, tid)
        event = _last_source_event(conn, tid)

        decision = record_report_back_dry_run(conn, task=task, event=event, board="default", dry_run=True)

        assert decision is not None
        events = _events(conn, tid)
        skipped = [payload for kind, payload in events if kind == "report_back_telegram_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["live_send_allowed"] is False
        assert "Desk-only" in skipped[0]["reason"] or "dry-run" in skipped[0]["reason"] or "dry_run" in skipped[0]["reason"]
        assert "sk_test" not in skipped[0]["message_preview"]
        assert "person@example.com" not in skipped[0]["message_preview"]

        redacted, labels = redact_report_text(
            "token=sk_test_1234567890abcdef1234567890abcdef person@example.com",
            privacy_tier="finance",
        )
        assert "sk_test" not in redacted
        assert "person@example.com" not in redacted
        assert "secret" in labels
    finally:
        conn.close()

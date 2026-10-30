from __future__ import annotations

import json
import os
import threading

import pytest


def _task(kb, tmp_path, handle):
    return kb.Task(
        id="t_private_intake_v0205",
        title="private intake",
        body=(
            "Canonical Margot OS private intake is held by the gateway until the "
            f"authorized assignee is actually claimed. Opaque handle: {handle}"
        ),
        assignee="steven",
        status="running",
        priority=0,
        created_by="gateway",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(tmp_path),
        claim_lock="claimed-lock",
        claim_expires=9999999999,
        tenant="chris-home",
        current_run_id=7,
    )


def test_private_intake_uses_authenticated_admission_fd_not_argv_env_or_disk(
    tmp_path, monkeypatch
):
    from gateway import department_topic_router as router
    from hermes_cli import kanban_db as kb

    secret = "private-intake-secret-never-persist-41ac"
    handle = router._PRIVATE_INTAKE_BROKER.put(
        request_text=secret,
        allowed_profiles=("steven",),
        idempotency_key="v0205-admission",
    )
    task = _task(kb, tmp_path, handle)
    captured = {}
    readers = []

    class Proc:
        pid = 12345

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured.update(cmd=cmd, kwargs=kwargs)
        inherited_fd = os.dup(kwargs["pass_fds"][0])

        def read_payload():
            with os.fdopen(inherited_fd, "rb") as stream:
                captured["payload"] = stream.read()

        thread = threading.Thread(target=read_payload)
        thread.start()
        readers.append(thread)
        return Proc()

    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["/trusted/hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    # This unit targets the authenticated intake FD; upstream v0.21's
    # independent root-home worker guard is covered by its own tests.
    monkeypatch.setattr(kb, "_worker_profile_uses_root_home", lambda _profile: False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))

    admission = {
        "version": "hermes.worker-admission/v1",
        "task": task.id,
        "workspace_confinement": True,
    }
    assert kb._default_spawn(
        task,
        str(tmp_path),
        board="chris-home",
        admission_contract=admission,
    ) == Proc.pid
    for thread in readers:
        thread.join(timeout=2)

    payload = json.loads(captured["payload"])
    assert payload["query"] == secret
    assert payload["contract"] == admission
    assert secret not in repr(captured["cmd"])
    assert secret not in repr(captured["kwargs"]["env"])
    assert secret not in (task.body or "")
    with pytest.raises(KeyError, match="unavailable"):
        router._PRIVATE_INTAKE_BROKER._reserve_for_gateway_dispatch(
            handle,
            claimed_assignee="steven",
            claim_lock="retry-lock",
        )


def test_private_intake_spawn_failure_releases_reservation_for_retry(
    tmp_path, monkeypatch
):
    from gateway import department_topic_router as router
    from hermes_cli import kanban_db as kb

    handle = router._PRIVATE_INTAKE_BROKER.put(
        request_text="retryable-private-intake",
        allowed_profiles=("steven",),
        idempotency_key="v0205-retry",
    )
    task = _task(kb, tmp_path, handle)

    monkeypatch.setattr(
        kb.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    # This unit targets the authenticated intake FD; upstream v0.21's
    # independent root-home worker guard is covered by its own tests.
    monkeypatch.setattr(kb, "_worker_profile_uses_root_home", lambda _profile: False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))

    with pytest.raises(RuntimeError, match="spawn failed"):
        kb._default_spawn(
            task,
            str(tmp_path),
            board="chris-home",
            admission_contract={"workspace_confinement": True},
        )

    reservation = router._PRIVATE_INTAKE_BROKER._reserve_for_gateway_dispatch(
        handle,
        claimed_assignee="steven",
        claim_lock="retry-lock",
    )
    router._PRIVATE_INTAKE_BROKER._release_gateway_dispatch(reservation)

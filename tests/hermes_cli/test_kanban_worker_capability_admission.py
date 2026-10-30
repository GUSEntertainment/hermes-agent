from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_private_stderr_eof_before_process_exit_still_records_diagnostic(monkeypatch):
    import threading
    records=[]
    recorded=threading.Event()
    def record(**kw):
        records.append(kw)
        recorded.set()
    proc=subprocess.Popen([sys.executable,'-c',
        'import os,time; os.write(2,b"RuntimeError: fixture\\n"); os.close(2); time.sleep(.1); os._exit(7)'],
        stderr=subprocess.PIPE,stdout=subprocess.DEVNULL)
    monkeypatch.setattr(kb,'_record_private_worker_exit_diagnostic',record)
    kb._monitor_private_worker_startup_stderr(board='fixture',task_id='t_fixture',run_id=1,proc=proc)
    assert recorded.wait(5)
    assert len(records)==1 and records[0]['returncode']==7
    assert records[0]['stderr_bytes']==b'RuntimeError: fixture\n'
    assert kb._classify_worker_exit(proc.pid)==('nonzero_exit',7)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "worker"
    profile.mkdir(parents=True)
    review_skill = profile / "skills" / "sdlc-review"
    review_skill.mkdir(parents=True)
    review_skill.joinpath("SKILL.md").write_text(
        "---\nname: sdlc-review\ndescription: Review test candidates.\n---\nReview.\n",
        encoding="utf-8",
    )
    home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-test\n"
        "platform_toolsets:\n  cli: [file]\n"
        "fallback_providers:\n  - provider: anthropic\n    model: claude-fallback\n"
        "privacy:\n  tier: internal\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda *a, **k: "unknown")
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn, home
    finally:
        conn.close()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_invalid_admission_is_generic_sticky_and_has_zero_launch_side_effects(
    board, monkeypatch, lane
):
    conn, _ = board
    task_id = kb.create_task(
        conn, title="invalid capability", assignee="worker", workspace_kind="scratch"
    )
    if lane == "review":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))

    workspace = kb.workspaces_root() / task_id
    spawned = []
    monkeypatch.setattr(
        kb,
        "_build_worker_admission",
        lambda *a, **k: (_ for _ in ()).throw(kb.WorkerAdmissionError()),
    )

    def admission_spawn(*args, admission_contract=None, **kwargs):
        spawned.append((args, kwargs))

    result = kb.dispatch_once(conn, spawn_fn=admission_spawn)
    task = kb.get_task(conn, task_id)
    assert result.capability_blocked == [task_id]
    assert spawned == []
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert task.worker_pid is None
    assert task.consecutive_failures == 0
    assert not workspace.exists()
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked'",
        (task_id,),
    ).fetchall()
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["reason"] == kb.CAPABILITY_BLOCK_REASON

    assert kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append((a, k))).capability_blocked == []
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='capability_preflight_blocked'",
        (task_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_every_spawn_seam_requires_admission_and_claim_binds_digest(board, monkeypatch, lane):
    conn, _ = board
    task_id = kb.create_task(conn, title="admit", assignee="worker", workspace_kind="scratch")
    if lane == "review":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    calls = []
    real = kb._build_worker_admission

    def capture(task, profile, **kwargs):
        admission = real(task, profile, **kwargs)
        calls.append(admission)
        return admission

    monkeypatch.setattr(kb, "_build_worker_admission", capture)
    def admission_spawn(*args, admission_contract=None, **kwargs):
        return None

    result = kb.dispatch_once(conn, spawn_fn=admission_spawn)
    assert len(calls) == 1
    assert result.spawned and result.spawned[0][0] == task_id
    row = conn.execute(
        "SELECT worker_admission_digest, current_run_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["worker_admission_digest"] == calls[0].digest
    run = conn.execute(
        "SELECT worker_admission_digest FROM task_runs WHERE id=?", (row["current_run_id"],)
    ).fetchone()
    assert run["worker_admission_digest"] == calls[0].digest
    contract = calls[0].contract
    assert contract["profile"] == "worker"
    assert contract["provider"] == "anthropic"
    assert contract["model"] == "claude-test"
    assert contract["fallbacks"] == [
        {"model": "claude-fallback", "provider": "anthropic"}
    ]
    assert "file" in contract["toolsets"]
    assert contract["tenant"] is None
    assert contract["privacy"]["tier"] == "internal"
    assert contract["workspace"]["kind"] == "scratch"


def test_task_tamper_after_validation_loses_claim_cas_without_block_or_workspace(board, monkeypatch):
    conn, _ = board
    task_id = kb.create_task(
        conn, title="tamper", assignee="worker", model_override="gpt-original"
    )
    original = kb._build_worker_admission

    def tamper(task, profile, **kwargs):
        admission = original(task, profile, **kwargs)
        other = kb.connect()
        try:
            with kb.write_txn(other):
                other.execute("UPDATE tasks SET model_override='gpt-changed' WHERE id=?", (task.id,))
        finally:
            other.close()
        return admission

    monkeypatch.setattr(kb, "_build_worker_admission", tamper)
    spawned = []

    def admission_spawn(*args, admission_contract=None, **kwargs):
        spawned.append(1)

    result = kb.dispatch_once(conn, spawn_fn=admission_spawn)
    task = kb.get_task(conn, task_id)
    assert spawned == []
    assert result.capability_blocked == []
    assert task.status == "ready"
    assert task.model_override == "gpt-changed"
    assert task.current_run_id is None
    assert not (kb.workspaces_root() / task_id).exists()


def _assert_denied_without_claim(conn, task_id):
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert task.worker_pid is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("skill", ["sdlc-review", "missing-capability"])
def test_legacy_callback_is_denied_for_requested_capabilities_before_claim(
    board, skill
):
    conn, _ = board
    task_id = kb.create_task(
        conn,
        title="governed legacy callback",
        assignee="worker",
        skills=[skill],
    )
    spawned = []

    def legacy_spawn(task, workspace):
        spawned.append(task.id)

    result = kb.dispatch_once(conn, spawn_fn=legacy_spawn)

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


def test_legacy_callback_is_denied_when_profile_requires_confinement_before_claim(
    board
):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    profile_config.write_text(
        profile_config.read_text(encoding="utf-8")
        + "security:\n  workspace_confinement:\n    enabled: true\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(conn, title="private legacy callback", assignee="worker")
    spawned = []

    def legacy_spawn(task, workspace):
        spawned.append(task.id)

    result = kb.dispatch_once(conn, spawn_fn=legacy_spawn)

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


def test_toolset_only_callback_cannot_bypass_private_confinement(board):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    profile_config.write_text(
        profile_config.read_text(encoding="utf-8")
        + "security:\n  workspace_confinement:\n    enabled: true\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(conn, title="private toolset seam", assignee="worker")
    spawned = []

    def toolset_spawn(task, workspace, capability_toolsets=None):
        spawned.append((task.id, capability_toolsets))

    result = kb.dispatch_once(conn, spawn_fn=toolset_spawn)

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


def test_legacy_public_spawn_callback_does_not_claim_private_authority(
    board, monkeypatch
):
    conn, _ = board
    task_id = kb.create_task(conn, title="legacy public", assignee="worker")
    real_policy = getattr(kb, "_legacy_public_policy_admission")
    policy_contracts = []

    def capture_policy(*args, **kwargs):
        admission = real_policy(*args, **kwargs)
        policy_contracts.append(admission.contract)
        return admission

    monkeypatch.setattr(kb, "_legacy_public_policy_admission", capture_policy)
    monkeypatch.setattr(
        kb,
        "_build_worker_admission",
        lambda *a, **k: pytest.fail(
            "public legacy callbacks must not require worker admission"
        ),
    )

    def legacy_spawn(task, workspace):
        return 987654321

    result = kb.dispatch_once(conn, spawn_fn=legacy_spawn)
    task = kb.get_task(conn, task_id)
    row = conn.execute(
        "SELECT worker_admission_digest, worker_pid FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    assert result.spawned and result.spawned[0][0] == task_id
    assert len(policy_contracts) == 1
    assert policy_contracts[0]["version"] == "hermes.legacy-public-policy/v1"
    assert policy_contracts[0]["status"] == "admitted"
    assert policy_contracts[0]["governed_request"] is False
    assert policy_contracts[0]["workspace_confinement"] is False
    assert task is not None and task.status == "running"
    assert row["worker_admission_digest"] is None
    assert row["worker_pid"] is None


def test_legacy_public_policy_change_before_claim_loses_cas(board, monkeypatch):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    task_id = kb.create_task(conn, title="legacy policy race", assignee="worker")
    original = getattr(kb, "_legacy_public_policy_admission")
    spawned = []

    def make_private_after_policy_check(*args, **kwargs):
        admission = original(*args, **kwargs)
        profile_config.write_text(
            "security:\n  workspace_confinement:\n    enabled: true\n",
            encoding="utf-8",
        )
        return admission

    monkeypatch.setattr(
        kb, "_legacy_public_policy_admission", make_private_after_policy_check
    )

    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace: spawned.append(task.id)
    )

    task = kb.get_task(conn, task_id)
    assert result.capability_blocked == []
    assert spawned == []
    assert task is not None and task.status == "ready"
    assert task.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "task_policy",
    [
        {"skills": ["sdlc-review"]},
        {"provider_override": "anthropic", "model_override": "claude-test"},
        {"model_override": "claude-test"},
        {"reasoning_effort": "high"},
        {"tenant": "private-tenant"},
    ],
)
def test_legacy_callback_denies_every_governed_task_policy_before_claim(
    board, monkeypatch, task_policy
):
    conn, _ = board
    task_id = kb.create_task(
        conn, title="governed legacy policy", assignee="worker", **task_policy
    )
    spawned = []
    monkeypatch.setattr(
        kb,
        "_build_worker_admission",
        lambda *a, **k: pytest.fail(
            "governed legacy denial must not need provider/model admission"
        ),
    )

    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace: spawned.append(task.id)
    )

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


@pytest.mark.parametrize(
    "malformed_policy",
    [
        "security: [not, a, mapping]\n",
        "security:\n  workspace_confinement: private\n",
        "security:\n  workspace_confinement:\n    enabled: private\n",
        "security: [unterminated\n",
    ],
)
def test_legacy_callback_denies_malformed_real_profile_policy_before_claim(
    board, monkeypatch, malformed_policy
):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    profile_config.write_text(malformed_policy, encoding="utf-8")
    task_id = kb.create_task(
        conn, title="malformed legacy profile policy", assignee="worker"
    )
    spawned = []
    monkeypatch.setattr(
        kb,
        "_build_worker_admission",
        lambda *a, **k: pytest.fail(
            "malformed legacy denial must not invoke full worker admission"
        ),
    )

    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace: spawned.append(task.id)
    )

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


def test_legacy_callback_denies_unreadable_real_profile_policy_before_claim(
    board, monkeypatch
):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    task_id = kb.create_task(
        conn, title="unreadable legacy profile policy", assignee="worker"
    )
    spawned = []
    real_read_bytes = Path.read_bytes

    def unreadable_policy(path):
        if path == profile_config:
            raise PermissionError("policy is unreadable")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable_policy)
    monkeypatch.setattr(
        kb,
        "_build_worker_admission",
        lambda *a, **k: pytest.fail(
            "unreadable legacy denial must not invoke full worker admission"
        ),
    )

    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace: spawned.append(task.id)
    )

    assert result.capability_blocked == [task_id]
    assert spawned == []
    _assert_denied_without_claim(conn, task_id)


def test_confined_new_worktree_is_persisted_before_admission_and_claim(
    board, monkeypatch, tmp_path
):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    profile_config.write_text(
        profile_config.read_text(encoding="utf-8")
        + "security:\n  workspace_confinement:\n    enabled: true\n",
        encoding="utf-8",
    )
    repo = tmp_path / "project"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test User",
            "-c", "user.email=test@example.com", "commit", "-m", "init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    kb.write_board_metadata("default", default_workdir=str(repo))
    task_id = kb.create_task(
        conn,
        title="confined worktree",
        assignee="worker",
        workspace_kind="worktree",
    )
    observed = {}

    def private_spawn(task, workspace, admission_contract=None):
        observed["task"] = task
        observed["workspace"] = workspace
        observed["contract"] = admission_contract

    result = kb.dispatch_once(conn, spawn_fn=private_spawn)

    expected = (repo / ".worktrees" / task_id).resolve()
    task = kb.get_task(conn, task_id)
    assert result.spawned == [(task_id, "worker", str(expected))]
    assert task is not None and task.status == "running"
    assert task.workspace_path == str(expected)
    assert task.branch_name == f"wt/{task_id}"
    assert observed["workspace"] == str(expected)
    assert observed["contract"]["workspace"] == {
        "kind": "worktree",
        "path": str(expected),
        "branch": f"wt/{task_id}",
        "project": None,
    }
    assert observed["contract"]["workspace_confinement"] is True
    persisted = json.loads(
        conn.execute(
            "SELECT worker_admission_contract FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    )
    assert persisted == observed["contract"]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_worktree_resolution_failure_blocks_before_admission_claim_or_spawn(
    board, monkeypatch, lane
):
    conn, _ = board
    task_id = kb.create_task(
        conn,
        title="broken worktree",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path="/missing/repository/.worktrees/task",
    )
    if lane == "review":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))

    admission_calls = []
    spawn_calls = []
    hook_calls = []
    monkeypatch.setattr(
        kb,
        "_resolve_worktree_workspace",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected materialization failure")),
    )
    monkeypatch.setattr(
        kb, "_build_worker_admission", lambda *a, **k: admission_calls.append(1)
    )
    monkeypatch.setattr(
        kb, "_fire_worker_spawned_hook", lambda *a, **k: hook_calls.append(1)
    )

    def spawn(*args, admission_contract=None, **kwargs):
        spawn_calls.append(1)

    result = kb.dispatch_once(conn, spawn_fn=spawn)
    task = kb.get_task(conn, task_id)

    assert result.workspace_blocked == [task_id]
    assert admission_calls == []
    assert spawn_calls == []
    assert hook_calls == []
    assert task is not None and task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert task.worker_pid is None
    assert task.consecutive_failures == 0
    assert task.last_failure_error == "worktree preclaim: injected materialization failure"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0
    event_kinds = [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (task_id,)
        ).fetchall()
    ]
    assert "worktree_preclaim_blocked" in event_kinds
    assert "claimed" not in event_kinds
    assert "spawned" not in event_kinds
    assert "spawn_failed" not in event_kinds

    again = kb.dispatch_once(conn, spawn_fn=spawn)
    assert again.workspace_blocked == []
    assert admission_calls == []
    assert spawn_calls == []


def test_confined_admission_aware_spawn_requires_live_pid_identity(
    board, monkeypatch
):
    conn, _ = board
    task_id = kb.create_task(conn, title="private pid binding", assignee="worker")
    real_build = kb._build_worker_admission

    def confined_admission(task, profile, **kwargs):
        admission = real_build(task, profile, **kwargs)
        contract = dict(admission.contract)
        contract["workspace_confinement"] = True
        encoded = json.dumps(
            contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        import hashlib
        return kb.WorkerAdmission(
            contract,
            hashlib.sha256(encoded.encode()).hexdigest(),
            admission.task_snapshot,
            admission.profile_config_digest,
        )

    monkeypatch.setattr(kb, "_build_worker_admission", confined_admission)
    monkeypatch.setattr(kb, "_worker_process_identity", lambda _pid: None)

    def private_spawn(task, workspace, admission_contract=None):
        assert admission_contract is not None
        return 987654321

    result = kb.dispatch_once(conn, spawn_fn=private_spawn)
    task = kb.get_task(conn, task_id)
    assert result.spawned == []
    assert task is not None and task.status == "ready"
    assert task.consecutive_failures == 1
    assert task.last_failure_error == "worker PID start identity unavailable"


def test_private_spawn_keeps_query_off_argv_and_inside_admission_pipe(board, monkeypatch, tmp_path):
    _conn, _home = board
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_private_argv", title="private", body=None, assignee="worker",
        status="running", priority=0, created_by="user", created_at=0,
        started_at=0, completed_at=None, workspace_kind="dir",
        workspace_path=str(workspace), claim_lock="claim", claim_expires=9999999999,
        tenant=None,
    )
    contract = {
        "version": "hermes.worker-admission/v1", "task": task.id,
        "admission_nonce": "private-spawn-test-admission",
        "board_database": kb._database_identity_contract(kb.kanban_db_path()),
        "profile": "worker", "profile_home": str(_home / "profiles" / "worker"),
        "profile_config_digest": "a" * 64, "provider": "anthropic", "model": "test",
        "fallbacks": [], "reasoning": None, "toolsets": ["file"], "skills": [],
        "skill_dependencies": {"loaded": [], "unavailable": []}, "privacy": {},
        "tenant": None,
        "workspace": {"kind": "dir", "path": str(workspace), "branch": None, "project": None},
        "workspace_confinement": True, "workspace_read_only_roots": [],
        "workspace_read_only_root_identities": [],
    }
    captured = {}

    class FakeProc:
        pid = os.getpid()

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs["env"]
        return FakeProc()

    def capture_write(fd, payload):
        captured["payload"] = payload
        return len(payload)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "write", capture_write)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    kb._default_spawn(task, str(workspace), admission_contract=contract)

    secret_query = f"work kanban task {task.id}"
    assert secret_query not in captured["argv"]
    assert captured["argv"][-1] == "--private-worker-intake"
    assert json.loads(captured["payload"])["query"] == secret_query


def test_private_spawn_real_process_has_no_query_in_argv_or_worker_log(board, monkeypatch, tmp_path):
    _conn, _home = board
    workspace = tmp_path / "real-private-worker"
    workspace.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, pathlib, psutil\n"
        "fd=int(os.environ['HERMES_WORKER_ADMISSION_FD'])\n"
        "payload=b''\n"
        "while True:\n"
        " chunk=os.read(fd,65536)\n"
        " if not chunk: break\n"
        " payload += chunk\n"
        "pathlib.Path('capture.json').write_text(json.dumps({"
        "'argv': '\\0'.join(psutil.Process().cmdline()),"
        "'intake':json.loads(payload)}))\n"
    )
    task = kb.Task(
        id="t_private_real", title="private", body=None, assignee="worker",
        status="running", priority=0, created_by="user", created_at=0,
        started_at=0, completed_at=None, workspace_kind="dir",
        workspace_path=str(workspace), claim_lock="claim", claim_expires=9999999999,
        tenant=None,
    )
    contract = {
        "version": "hermes.worker-admission/v1", "task": task.id,
        "admission_nonce": "private-spawn-test-admission",
        "board_database": kb._database_identity_contract(kb.kanban_db_path()),
        "profile": "worker", "profile_home": str(_home / "profiles" / "worker"),
        "profile_config_digest": "a" * 64, "provider": "anthropic", "model": "test",
        "fallbacks": [], "reasoning": None, "toolsets": ["file"], "skills": [],
        "skill_dependencies": {"loaded": [], "unavailable": []}, "privacy": {},
        "tenant": None,
        "workspace": {"kind": "dir", "path": str(workspace), "branch": None, "project": None},
        "workspace_confinement": True, "workspace_read_only_roots": [],
        "workspace_read_only_root_identities": [],
    }
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(probe)])
    children = []
    guarded_popen = subprocess.Popen

    def spawn_probe(*args, **kwargs):
        # This test exercises private admission transport, not daemon lifetime.
        # Keep the one-shot macOS probe attached to the canonical runner; its
        # startup guard intentionally refuses detached processes on this host.
        if sys.platform == "darwin":
            assert kwargs["start_new_session"] is True
            kwargs["start_new_session"] = False
        child = guarded_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn_probe)
    try:
        pid = kb._default_spawn(task, str(workspace), admission_contract=contract)
        assert children[0].wait(timeout=10) == 0
        capture = workspace / "capture.json"
        assert capture.exists()
        observed = json.loads(capture.read_text())
        secret_query = f"work kanban task {task.id}"
        assert secret_query not in observed["argv"]
        assert observed["intake"]["query"] == secret_query
        assert not kb.worker_logs_dir().joinpath(f"{task.id}.log").exists()
        assert pid
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_private_spawn_startup_crash_records_bounded_diagnostic(
    board, monkeypatch, tmp_path
):
    conn, _home = board
    workspace = tmp_path / "crashing-private-worker"
    workspace.mkdir()
    probe = tmp_path / "crash_probe.py"
    probe.write_text(
        "import os, sys\n"
        "fd=int(os.environ['HERMES_WORKER_ADMISSION_FD'])\n"
        "while os.read(fd, 65536):\n"
        " pass\n"
        "sys.stderr.write(\"ModuleNotFoundError: No module named 'hermes_cli'\\n\")\n"
        "sys.stderr.write('private task filename /tmp/private-input.txt must not leak\\n')\n"
        "sys.stderr.write('diagnostic-byte-' * 500)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="private startup diagnostic",
        assignee="worker",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert claimed.current_run_id is not None
    contract = {
        "version": "hermes.worker-admission/v1",
        "task": task_id,
        "admission_nonce": "private-startup-diagnostic",
        "board_database": kb._database_identity_contract(kb.kanban_db_path()),
        "profile": "worker",
        "profile_home": str(_home / "profiles" / "worker"),
        "profile_config_digest": "a" * 64,
        "provider": "anthropic",
        "model": "test",
        "fallbacks": [],
        "reasoning": None,
        "toolsets": ["file"],
        "skills": [],
        "skill_dependencies": {"loaded": [], "unavailable": []},
        "privacy": {},
        "tenant": None,
        "workspace": {
            "kind": "dir",
            "path": str(workspace),
            "branch": None,
            "project": None,
        },
        "workspace_confinement": True,
        "workspace_read_only_roots": [],
        "workspace_read_only_root_identities": [],
    }
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(probe)])
    guarded_popen = subprocess.Popen

    def spawn_probe(*args, **kwargs):
        if sys.platform == "darwin":
            kwargs["start_new_session"] = False
        return guarded_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn_probe)

    pid = kb._default_spawn(claimed, str(workspace), admission_contract=contract)
    assert pid

    deadline = time.time() + kb.PRIVATE_WORKER_STARTUP_DIAGNOSTIC_SECONDS + 5
    payload = None
    while time.time() < deadline:
        with kb.contextlib.closing(kb.connect()) as poll_conn:
            row = poll_conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id=? AND kind='worker_startup_diagnostic' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            break
        time.sleep(0.05)

    assert payload is not None
    assert payload["returncode"] == 7
    assert payload["stderr_bytes_captured"] <= kb.PRIVATE_WORKER_STARTUP_DIAGNOSTIC_BYTES
    assert payload["stderr_truncated"] is True
    assert payload["diagnostic_code"] == "missing_python_module"
    assert payload["error_class"] == "ModuleNotFoundError"
    assert payload["error_resource"] == "hermes_cli"
    assert len(payload["diagnostic_fingerprint"]) == 64
    assert payload['runtime_source_fingerprint'] == hashlib.sha256(Path(kb.__file__).read_bytes()).hexdigest()
    payload_text = json.dumps(payload, sort_keys=True)
    assert "stderr_excerpt" not in payload
    assert "private-input" not in payload_text
    assert "diagnostic-byte" not in payload_text
    assert f"work kanban task {task_id}" not in payload_text
    assert not kb.worker_logs_dir().joinpath(f"{task_id}.log").exists()


def test_private_startup_unknown_stderr_uses_digest_without_raw_text():
    raw = b"custom private path /Users/example/secret-project/input.csv\n"
    diagnostic = kb._safe_private_worker_startup_diagnostic(raw)
    assert diagnostic["diagnostic_code"] == "private_worker_startup_crash"
    assert diagnostic["stderr_present"] is True
    assert len(diagnostic["diagnostic_fingerprint"]) == 64
    assert "error_class" not in diagnostic
    assert "secret-project" not in json.dumps(diagnostic, sort_keys=True)


def test_worker_admission_uses_active_source_root_even_from_foreign_cwd(
    board, monkeypatch, tmp_path
):
    conn, _home = board
    task_id = kb.create_task(conn, title="source-bound admission", assignee="worker")
    task = kb.get_task(conn, task_id)
    assert task is not None
    workspace = tmp_path / "target-repository"
    (workspace / "agent").mkdir(parents=True)
    (workspace / "agent" / "__init__.py").write_text(
        'raise RuntimeError("target repository package selected")\n',
        encoding="utf-8",
    )
    (workspace / "hermes_cli").mkdir()
    (workspace / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "hermes_cli" / "config.py").write_text(
        'raise RuntimeError("target repository hermes_cli selected")\n',
        encoding="utf-8",
    )
    captured = {}
    real_run = subprocess.run

    def capture_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env") or {})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(workspace)

    admission = kb._build_worker_admission(task, "worker")

    source_root = str(Path(kb.__file__).resolve().parents[1])
    pythonpath = captured["env"].get("PYTHONPATH", "").split(os.pathsep)
    assert pythonpath[0] == source_root
    if sys.version_info >= (3, 11):
        assert captured["argv"][1] == "-P"
    assert admission.contract["profile"] == "worker"
    assert admission.contract["skill_dependencies"]["tool_schema"]["available_tools"]


def test_default_worker_launcher_uses_safe_path_and_preserves_explicit_hermes_bin(
    monkeypatch,
):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    default_argv = kb._resolve_hermes_argv()
    assert default_argv[0] == sys.executable
    if sys.version_info >= (3, 11):
        assert default_argv[1:3] == ["-P", "-m"]
    else:
        assert default_argv[1:3] == ["-m", "hermes_cli.main"]

    monkeypatch.setenv("HERMES_BIN", "/custom/hermes")
    assert kb._resolve_hermes_argv() == ["/custom/hermes"]


def test_required_skill_tool_must_be_visible_in_actual_worker_schema(board, monkeypatch):
    conn, home = board
    profile_config = home / "profiles" / "worker" / "config.yaml"
    profile_config.write_text(
        "model:\n  provider: anthropic\n  default: claude-test\n"
        "platform_toolsets:\n  cli: [homeassistant]\n",
        encoding="utf-8",
    )
    skill = home / "profiles" / "worker" / "skills" / "requires-ha"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\n"
        "name: requires-ha\n"
        "description: Needs Home Assistant\n"
        "metadata:\n"
        "  hermes:\n"
        "    requires_tools: [ha_list_entities]\n"
        "---\n"
        "Use Home Assistant.\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    monkeypatch.delenv("HASS_URL", raising=False)
    task_id = kb.create_task(
        conn,
        title="requires unavailable connector schema",
        assignee="worker",
        skills=["requires-ha"],
    )
    spawned = []

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace, admission_contract=None: spawned.append(task.id),
    )

    assert result.capability_blocked == [task_id]
    assert spawned == []
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.current_run_id is None


def test_safe_path_subprocess_imports_release_source_before_workspace_package(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace-with-shadow-package"
    package = workspace / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text(
        'raise RuntimeError("workspace package won import precedence")\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    source_root = str(Path(kb.__file__).resolve().parents[1])
    env["PYTHONPATH"] = source_root
    proc = subprocess.run(
        [
            sys.executable,
            *kb._python_safe_path_flag(),
            "-c",
            "import hermes_cli.config; print(hermes_cli.config.__file__)",
        ],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith(source_root)


def test_safe_path_module_launch_uses_release_cli_before_workspace_package(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    workspace = tmp_path / "workspace-with-shadow-main"
    package = workspace / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(
        'raise RuntimeError("workspace main won import precedence")\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    source_root = str(Path(kb.__file__).resolve().parents[1])
    env["PYTHONPATH"] = source_root
    proc = subprocess.run(
        [*kb._resolve_hermes_argv(), "--help"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "workspace main won import precedence" not in proc.stderr
    assert "usage: hermes" in proc.stdout

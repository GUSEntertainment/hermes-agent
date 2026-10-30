import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agent import private_worker_mode as pwm


def _contract(tmp_path, db_path=None):
    workspace = tmp_path / "workspace"
    home = tmp_path / "profiles" / "terra-maker-alpha"
    workspace.mkdir()
    home.mkdir(parents=True)
    contract = {
        "version": "hermes.worker-admission/v1", "task": "t_private",
        "admission_nonce": "test-admission-nonce",
        "profile": "terra-maker-alpha", "profile_home": str(home),
        "profile_config_digest": "a" * 64, "provider": "test", "model": "test",
        "fallbacks": [], "reasoning": None, "toolsets": ["file", "terminal", "skills"],
        "skills": ["alpha"],
        "skill_dependencies": {"loaded": ["alpha"], "unavailable": []},
        "privacy": {}, "tenant": None,
        "workspace": {"kind": "dir", "path": str(workspace), "branch": None, "project": None},
        "workspace_confinement": True, "workspace_read_only_roots": [],
        "workspace_read_only_root_identities": [],
    }
    if db_path is None:
        db_path = tmp_path / "kanban.db"
        db_path.touch(exist_ok=True)
    info = os.stat(db_path)
    contract["board_database"] = {
        "path": os.path.realpath(db_path),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }
    return contract


def _live_claim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    contract = _contract(tmp_path, db_path)
    task_id = kb.create_task(conn, title="live admission", assignee=contract["profile"])
    contract["task"] = task_id
    digest = pwm.contract_digest(contract)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    encoded = pwm._canonical_bytes(contract).decode()
    conn.execute(
        "UPDATE tasks SET worker_admission_digest=?, worker_admission_contract=? WHERE id=?",
        (digest, encoded, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET worker_admission_digest=?, worker_admission_contract=? WHERE id=?",
        (digest, encoded, claimed.current_run_id),
    )
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    return conn, db_path, contract, digest, claimed.current_run_id


@pytest.fixture(autouse=True)
def reset_contract():
    pwm._reset_private_worker_contract_for_tests()
    yield
    pwm._reset_private_worker_contract_for_tests()


def test_contract_requires_live_durable_validator(tmp_path):
    with pytest.raises(pwm.PrivateWorkerContractError, match="not live"):
        pwm.install_authenticated_worker_contract(
            _contract(tmp_path), durable_validator=lambda _contract, _digest: False
        )
    assert pwm.get_authenticated_worker_contract() is None


@pytest.mark.parametrize("mutation", [
    lambda c: c.pop("workspace_confinement"),
    lambda c: c.__setitem__("workspace_confinement", "true"),
    lambda c: c.__setitem__("workspace_read_only_roots", "[]"),
    lambda c: c["workspace"].__setitem__("path", "relative"),
])
def test_contract_rejects_malformed_policy_before_validator(tmp_path, mutation):
    contract = _contract(tmp_path)
    mutation(contract)
    called = False
    def validator(_contract, _digest):
        nonlocal called
        called = True
        return True
    with pytest.raises(pwm.PrivateWorkerContractError):
        pwm.install_authenticated_worker_contract(contract, durable_validator=validator)
    assert called is False


def test_contract_is_one_process_one_identity(tmp_path):
    first = _contract(tmp_path)
    pwm.install_authenticated_worker_contract(first, durable_validator=lambda _c, _d: True)
    second = dict(first)
    second["task"] = "t_other"
    with pytest.raises(pwm.PrivateWorkerContractError, match="already bound"):
        pwm.install_authenticated_worker_contract(second, durable_validator=lambda _c, _d: True)


def test_authenticated_contract_nested_authority_is_immutable(tmp_path):
    source = _contract(tmp_path)
    installed = pwm.install_authenticated_worker_contract(
        source, durable_validator=lambda _c, _d: True
    )
    installed["board_database"]["inode"] = 1
    installed["workspace"]["path"] = "/"
    source["board_database"]["inode"] = 2

    observed = pwm.get_authenticated_worker_contract()
    assert observed is not None
    assert observed["board_database"]["inode"] not in {1, 2}
    assert observed["workspace"]["path"] != "/"


def test_admission_fd_malformed_fails_closed_instead_of_becoming_ordinary_worker(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"not-json")
    os.close(write_fd)
    monkeypatch.setenv("HERMES_WORKER_ADMISSION_FD", str(read_fd))

    with pytest.raises(pwm.PrivateWorkerContractError, match="malformed"):
        pwm.get_authenticated_worker_contract()


def test_admission_fd_durable_rejection_remains_failed_closed(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"contract": _contract(tmp_path), "query": "secret"}).encode())
    os.close(write_fd)
    monkeypatch.setenv("HERMES_WORKER_ADMISSION_FD", str(read_fd))
    monkeypatch.setattr(pwm, "_default_durable_validator", lambda *_args: False)

    with pytest.raises(pwm.PrivateWorkerContractError, match="not live"):
        pwm.consume_private_worker_intake()
    with pytest.raises(pwm.PrivateWorkerContractError, match="previously failed"):
        pwm.get_authenticated_worker_contract()


def test_private_intake_activates_suppression_before_json_parsing(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"{")
    os.close(write_fd)
    monkeypatch.setenv("HERMES_WORKER_ADMISSION_FD", str(read_fd))
    observed = []
    monkeypatch.setattr(pwm, "activate_private_worker_mode", lambda: observed.append("suppressed"))

    with pytest.raises(pwm.PrivateWorkerContractError):
        pwm.consume_private_worker_intake()
    assert observed == ["suppressed"]


def test_model_tool_assembly_refuses_failed_admission(monkeypatch):
    import model_tools

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"bad")
    os.close(write_fd)
    monkeypatch.setenv("HERMES_WORKER_ADMISSION_FD", str(read_fd))

    with pytest.raises(pwm.PrivateWorkerContractError):
        model_tools._confined_worker_skill_boundary_active()


def test_private_worker_transcript_is_not_persisted(monkeypatch):
    from run_agent import AIAgent

    calls = []
    fake = type("FakeAgent", (), {
        "_session_persist_lock": None,
        "_session_db": None,
        "_drop_trailing_empty_response_scaffolding": lambda self, messages: calls.append("drop"),
        "_save_session_log": lambda self, messages: calls.append("json"),
        "_flush_messages_to_session_db": lambda self, messages, history: calls.append("db"),
    })()
    monkeypatch.setenv("HERMES_PRIVATE_WORKER", "1")

    AIAgent._persist_session(fake, [{"role": "user", "content": "private-canary"}])
    assert calls == []


def test_durable_validator_waits_for_dispatcher_pid_registration(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    contract = _contract(tmp_path, db_path)
    task_id = kb.create_task(conn, title="registration race", assignee=contract["profile"])
    contract["task"] = task_id
    digest = pwm.contract_digest(contract)
    claimed = kb.claim_task(conn, task_id)
    encoded = pwm._canonical_bytes(contract).decode()
    conn.execute(
        "UPDATE tasks SET worker_admission_digest=?, worker_admission_contract=? WHERE id=?",
        (digest, encoded, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET worker_admission_digest=?, worker_admission_contract=? WHERE id=?",
        (digest, encoded, claimed.current_run_id),
    )
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    def register_later():
        time.sleep(0.05)
        other = kb.connect(db_path)
        try:
            kb._set_worker_pid(other, task_id, os.getpid())
        finally:
            other.close()

    thread = threading.Thread(target=register_later)
    thread.start()
    try:
        assert pwm._default_durable_validator(contract, digest) is True
    finally:
        thread.join(timeout=5)
        conn.close()


def test_durable_validator_fails_closed_when_database_is_unavailable(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("db unavailable")))
    contract = _contract(tmp_path)
    assert pwm._default_durable_validator(contract, pwm.contract_digest(contract)) is False


def test_durable_validator_rejects_expired_live_claim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn, _db_path, contract, digest, run_id = _live_claim(tmp_path, monkeypatch)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
        conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (contract["task"],))
        conn.execute("UPDATE task_runs SET claim_expires=1 WHERE id=?", (run_id,))
        conn.commit()
        assert pwm._default_durable_validator(contract, digest) is False
    finally:
        conn.close()


def test_durable_validator_rejects_pid_reuse_start_mismatch(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn, _db_path, contract, digest, run_id = _live_claim(tmp_path, monkeypatch)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
        conn.execute(
            "UPDATE tasks SET worker_process_start=worker_process_start-1 WHERE id=?",
            (contract["task"],),
        )
        conn.execute(
            "UPDATE task_runs SET worker_process_start=worker_process_start-1 WHERE id=?",
            (run_id,),
        )
        conn.commit()
        assert pwm._default_durable_validator(contract, digest) is False
    finally:
        conn.close()


def test_durable_validator_rejects_when_os_identity_probe_is_unavailable(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb

    conn, _db_path, contract, digest, _run_id = _live_claim(tmp_path, monkeypatch)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
        monkeypatch.setattr(kb, "_worker_process_identity", lambda _pid: None)
        assert pwm._default_durable_validator(contract, digest) is False
    finally:
        conn.close()


def test_durable_validator_rejects_copied_board_replay(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn, _db_path, contract, digest, _run_id = _live_claim(tmp_path, monkeypatch)
    copied_path = tmp_path / "copied.db"
    copied = kb.connect(copied_path)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
        conn.backup(copied)
    finally:
        copied.close()
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(copied_path))
    assert pwm._default_durable_validator(contract, digest) is False


def test_durable_validator_rejects_board_path_replacement(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn, db_path, contract, digest, _run_id = _live_claim(tmp_path, monkeypatch)
    replacement_path = tmp_path / "replacement.db"
    replacement = kb.connect(replacement_path)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
        conn.backup(replacement)
    finally:
        replacement.close()
        conn.close()
    os.replace(replacement_path, db_path)
    assert pwm._default_durable_validator(contract, digest) is False


def test_durable_validator_rejects_handoff_replayed_by_reopened_process(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb

    conn, db_path, contract, digest, _run_id = _live_claim(tmp_path, monkeypatch)
    try:
        assert kb._set_worker_pid(conn, contract["task"], os.getpid()) is not False
    finally:
        conn.close()
    code = (
        "import json, os, sys; from agent import private_worker_mode as p; "
        "c=json.loads(os.environ['CONTRACT']); "
        "sys.exit(0 if not p._default_durable_validator(c, os.environ['DIGEST']) else 9)"
    )
    env = dict(os.environ)
    env.update(
        HERMES_KANBAN_DB=str(db_path), CONTRACT=json.dumps(contract), DIGEST=digest
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, check=False)
    assert result.returncode == 0


def test_worker_pid_and_start_registration_has_no_separate_transaction_gap(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    observer = kb.connect(db_path)
    task_id = kb.create_task(conn, title="registration atomic", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    probing = threading.Event()
    release = threading.Event()
    real_identity = kb._worker_process_identity(os.getpid())
    assert real_identity is not None

    def delayed_identity(pid):
        probing.set()
        assert release.wait(5)
        return real_identity

    monkeypatch.setattr(kb, "_worker_process_identity", delayed_identity)

    def register():
        registration = kb.connect(db_path)
        try:
            kb._set_worker_pid(registration, task_id, os.getpid())
        finally:
            registration.close()

    worker = threading.Thread(target=register)
    worker.start()
    try:
        assert probing.wait(5)
        row = observer.execute(
            "SELECT worker_pid,worker_process_start FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["worker_pid"] is None
        assert row["worker_process_start"] is None
    finally:
        release.set()
        worker.join(timeout=5)
        observer.close()
        conn.close()

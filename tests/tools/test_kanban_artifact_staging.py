import json
import os
from pathlib import Path
import zipfile

import pytest


def _pdf(path: Path, body=b"safe") -> None:
    path.write_bytes(b"%PDF-1.7\n" + body)


def _docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")


class _KB:
    def __init__(self, destination: Path):
        self.destination = destination

    def task_attachments_dir(self, task_id, board=None):
        return self.destination / task_id


def test_stage_uses_bound_source_descriptor_when_path_is_swapped(tmp_path, monkeypatch):
    import tools.kanban_artifact_staging as staging

    source = tmp_path / "report.pdf"
    _pdf(source, b"ORIGINAL")
    original_validate = staging._validate_source_fd

    def swap_after_validation(fd, path):
        result = original_validate(fd, path)
        source.rename(tmp_path / "original-unlinked.pdf")
        _pdf(source, b"ATTACKER")
        return result

    monkeypatch.setattr(staging, "_validate_source_fd", swap_after_validation)
    staged = staging.stage_review_worthy_artifacts(
        _KB(tmp_path / "attachments"), task_id="t1", board=None, artifacts=[str(source)]
    )

    assert Path(staged[0]).read_bytes().endswith(b"ORIGINAL")
    assert b"ATTACKER" not in Path(staged[0]).read_bytes()
    assert (os.stat(staged[0]).st_mode & 0o777) == 0o600


def test_stage_rejects_symlinked_destination_parent(tmp_path):
    from tools.kanban_artifact_staging import stage_review_worthy_artifacts

    source = tmp_path / "report.pdf"
    _pdf(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    attachments = tmp_path / "attachments"
    attachments.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="destination"):
        stage_review_worthy_artifacts(
            _KB(attachments), task_id="t1", board=None, artifacts=[str(source)]
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["credentials.pdf", "client-secret.docx", "api_key.pdf", ".env.pdf"])
def test_stage_rejects_secret_like_names(tmp_path, name):
    from tools.kanban_artifact_staging import stage_review_worthy_artifacts

    source = tmp_path / name
    if source.suffix == ".docx":
        _docx(source)
    else:
        _pdf(source)
    with pytest.raises(ValueError, match="secret-like"):
        stage_review_worthy_artifacts(
            _KB(tmp_path / "attachments"), task_id="t1", board=None, artifacts=[str(source)]
        )


def test_stage_rejects_false_document_magic_and_non_regular_source(tmp_path):
    from tools.kanban_artifact_staging import stage_review_worthy_artifacts

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"not pdf")
    fake_docx = tmp_path / "fake.docx"
    fake_docx.write_bytes(b"PK definitely not a docx")
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    for source in (fake_pdf, fake_docx, fifo):
        with pytest.raises(ValueError):
            stage_review_worthy_artifacts(
                _KB(tmp_path / "attachments"), task_id="t1", board=None, artifacts=[str(source)]
            )


def test_collision_and_partial_failure_leave_no_new_staged_files(tmp_path):
    from tools.kanban_artifact_staging import stage_review_worthy_artifacts

    source1 = tmp_path / "one.pdf"
    source2 = tmp_path / "two.pdf"
    _pdf(source1, b"one")
    _pdf(source2, b"two")
    destination = tmp_path / "attachments" / "t1"
    destination.mkdir(parents=True)
    collision = destination / "two.pdf"
    collision.write_bytes(b"existing")

    with pytest.raises(ValueError, match="already exists"):
        stage_review_worthy_artifacts(
            _KB(tmp_path / "attachments"),
            task_id="t1",
            board=None,
            artifacts=[str(source1), str(source2)],
        )

    assert not (destination / "one.pdf").exists()
    assert collision.read_bytes() == b"existing"
    assert not [path for path in destination.iterdir() if path.name.startswith(".")]


def test_review_worthy_completion_stages_before_upstream_lifecycle(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(conn, title="stage", assignee="test-worker")
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    # v0.21.5 upstream fails closed without the dispatcher-pinned run id.
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    artifact = tmp_path / "deliverable.pdf"
    _pdf(artifact)

    output = json.loads(kt._handle_complete({
        "summary": "ready",
        "artifacts": [str(artifact)],
        "metadata": {"desk_artifacts": [{"review_worthy": True}]},
    }))

    assert output["ok"] is True
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, task_id)
        staged = run.metadata["artifacts"]
        assert len(staged) == 1
        assert Path(staged[0]).parent == kb.task_attachments_dir(task_id)
        assert Path(staged[0]).read_bytes() == artifact.read_bytes()
        assert run.metadata["desk_artifacts"] == [{"review_worthy": True}]
    finally:
        conn.close()

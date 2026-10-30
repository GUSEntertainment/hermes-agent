"""Descriptor-bound staging for explicit human-facing Kanban documents."""
from __future__ import annotations

import errno
import os
import re
import stat
import time
import zipfile
from pathlib import Path
from typing import Any

_DOCUMENT_SUFFIXES = {".pdf", ".docx"}
_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
_SECRET_NAME = re.compile(
    r"(?:^|[._-])(?:\.env|credentials?|secrets?|tokens?|api[_-]?key|"
    r"private[_-]?key|id_rsa|auth(?:entication)?|password)(?:[._-]|$)",
    re.IGNORECASE,
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def review_worthy_requested(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    descriptors = metadata.get("desk_artifacts")
    return isinstance(descriptors, (list, tuple)) and any(
        isinstance(item, dict) and item.get("review_worthy") is True
        for item in descriptors
    )


def _validate_source_fd(fd: int, path: Path) -> os.stat_result:
    """Validate type, size and document bytes through the held descriptor."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("human document source must be a non-symlink regular file")
    if info.st_size > _MAX_DOCUMENT_BYTES:
        raise ValueError("human document exceeds the staging size limit")
    suffix = path.suffix.lower()
    if suffix not in _DOCUMENT_SUFFIXES:
        raise ValueError("review-worthy artifact must be a PDF or DOCX document")
    if _SECRET_NAME.search(path.name):
        raise ValueError("refusing to stage a secret-like credential file")
    os.lseek(fd, 0, os.SEEK_SET)
    magic = os.read(fd, 8)
    if suffix == ".pdf":
        if not magic.startswith(b"%PDF-"):
            raise ValueError("human document is not a readable PDF")
    else:
        if not magic.startswith(b"PK\x03\x04"):
            raise ValueError("human document is not a readable DOCX")
        duplicate = os.dup(fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb") as stream:
                duplicate = -1
                try:
                    with zipfile.ZipFile(stream) as archive:
                        names = set(archive.namelist())
                except (OSError, zipfile.BadZipFile) as exc:
                    raise ValueError("human document is not a readable DOCX") from exc
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                raise ValueError("human document is not a readable DOCX")
        finally:
            if duplicate >= 0:
                os.close(duplicate)
    os.lseek(fd, 0, os.SEEK_SET)
    return info


def _open_source(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW | _CLOEXEC)
    except OSError as exc:
        raise ValueError(f"human document source is missing or unreadable: {path}") from exc


def _open_destination_directory(path: Path) -> int:
    """Create/open every destination component without following symlinks."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise ValueError("staging destination must resolve to an absolute path")
    current = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("staging destination contains an unsafe component")
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=current,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=current,
                    )
                except OSError as exc:
                    raise ValueError("staging destination component is not a real directory") from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("staging destination component is not a real directory") from exc
                raise ValueError("staging destination is unavailable") from exc
            os.close(current)
            current = child
        os.fchmod(current, 0o700)
        return current
    except Exception:
        os.close(current)
        raise


def _copy_bound_source(source_fd: int, destination_fd: int, name: str) -> None:
    temporary = f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
    output_fd: int | None = None
    try:
        output_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=destination_fd,
        )
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise OSError("failed to copy staged attachment")
                view = view[written:]
        os.fchmod(output_fd, 0o600)
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=destination_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ValueError(f"staged attachment already exists: {name}") from exc
    finally:
        if output_fd is not None:
            os.close(output_fd)
        try:
            os.unlink(temporary, dir_fd=destination_fd)
        except FileNotFoundError:
            pass


def stage_review_worthy_artifacts(
    kb: Any, *, task_id: str, board: str | None, artifacts: list[str]
) -> list[str]:
    """Validate and atomically copy documents into the board attachment root."""
    if not artifacts:
        raise ValueError("review-worthy completion requires at least one document artifact")
    destination = Path(kb.task_attachments_dir(task_id, board=board))
    destination_fd = _open_destination_directory(destination)
    staged_names: list[str] = []
    try:
        for raw in artifacts:
            source = Path(raw).expanduser()
            source_fd = _open_source(source)
            try:
                _validate_source_fd(source_fd, source)
                _copy_bound_source(source_fd, destination_fd, source.name)
            finally:
                os.close(source_fd)
            staged_names.append(source.name)
    except Exception:
        for name in staged_names:
            try:
                os.unlink(name, dir_fd=destination_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(destination_fd)
    return [str(destination / name) for name in staged_names]

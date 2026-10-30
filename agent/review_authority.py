"""Single-use reviewer decision authority inherited over an anonymous pipe."""
from __future__ import annotations

import json
import os
import stat
import threading
from typing import Any

_MAX_BYTES = 16 * 1024
_LOCK = threading.Lock()
_HANDOFF: dict[str, Any] | None = None
_CONSUMED = False
_FAILED = False


class ReviewAuthorityError(PermissionError):
    """The reviewer authority handoff is missing, malformed, or mismatched."""


def _load() -> dict[str, Any] | None:
    global _HANDOFF, _CONSUMED, _FAILED
    raw = os.environ.get("HERMES_REVIEW_DECISION_FD", "").strip()
    if not raw:
        return None
    with _LOCK:
        if _FAILED:
            raise ReviewAuthorityError("review authority handoff previously failed")
        if _HANDOFF is not None:
            return dict(_HANDOFF)
        if _CONSUMED:
            raise ReviewAuthorityError("review authority handoff was already consumed")
        _CONSUMED = True
    fd: int | None = None
    try:
        fd = int(raw)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReviewAuthorityError("review authority requires an anonymous pipe")
        data = b""
        while len(data) <= _MAX_BYTES:
            chunk = os.read(fd, min(4096, _MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > _MAX_BYTES:
            raise ReviewAuthorityError("review authority handoff is oversized")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewAuthorityError("review authority handoff is malformed") from exc
        required = {"version", "task", "run_id", "profile", "token"}
        if not isinstance(value, dict) or set(value) != required:
            raise ReviewAuthorityError("review authority schema is invalid")
        if value["version"] != "hermes.review-decision-handoff/v1":
            raise ReviewAuthorityError("review authority version is invalid")
        if not all(isinstance(value[key], str) and value[key] for key in ("task", "profile", "token")):
            raise ReviewAuthorityError("review authority identity is invalid")
        if not isinstance(value["run_id"], int) or value["run_id"] <= 0:
            raise ReviewAuthorityError("review authority run is invalid")
        if value["task"] != os.environ.get("HERMES_KANBAN_TASK", "").strip():
            raise ReviewAuthorityError("review authority task mismatch")
        if str(value["run_id"]) != os.environ.get("HERMES_KANBAN_RUN_ID", "").strip():
            raise ReviewAuthorityError("review authority run mismatch")
        if value["profile"] != os.environ.get("HERMES_PROFILE", "").strip():
            raise ReviewAuthorityError("review authority profile mismatch")
        with _LOCK:
            _HANDOFF = dict(value)
        return dict(value)
    except (ValueError, OSError, ReviewAuthorityError):
        with _LOCK:
            _FAILED = True
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def decision_token(task_id: str, run_id: int | None) -> str | None:
    """Return authority only when the process handoff matches task and run."""
    handoff = _load()
    if handoff is None:
        return None
    if handoff["task"] != str(task_id) or int(handoff["run_id"]) != int(run_id or 0):
        raise ReviewAuthorityError("review authority does not match this decision")
    return str(handoff["token"])


def _reset_for_tests() -> None:
    global _HANDOFF, _CONSUMED, _FAILED
    with _LOCK:
        _HANDOFF = None
        _CONSUMED = False
        _FAILED = False

#!/usr/bin/env python3
"""Fail-closed helpers used by the canonical test-suite boundary."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

OWNER_MARKER = ".hermes-test-suite-owner"


def _owner_digest(token: str) -> str:
    """Return non-authoritative marker evidence for private *token*."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def validate_suite_home(candidate: Path | str, forbidden: Iterable[Path | str]) -> Path:
    """Return a normalized candidate, rejecting live roots and descendants."""
    resolved = _resolved(candidate)
    for root_value in forbidden:
        if not str(root_value).strip():
            continue
        root = _resolved(root_value)
        if resolved == root or root in resolved.parents:
            raise ValueError(f"suite home {resolved} is forbidden by live root {root}")
    return resolved


def create_suite_home(
    base: Path | str | None = None,
    forbidden: Iterable[Path | str] = (),
) -> tuple[Path, str]:
    """Create a random suite root and bind it to an unpredictable marker."""
    parent = _resolved(base) if base is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="hermes-test-suite-", dir=parent))
    try:
        candidate = validate_suite_home(candidate, forbidden)
        token = secrets.token_urlsafe(32)
        marker = candidate / OWNER_MARKER
        marker.write_text(_owner_digest(token) + "\n", encoding="utf-8")
        marker.chmod(0o600)
        return candidate, token
    except BaseException:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def cleanup_suite_home(
    candidate: Path | str,
    token: str,
    forbidden: Iterable[Path | str] = (),
) -> None:
    """Remove only the validated root carrying this invocation's marker."""
    root = validate_suite_home(candidate, forbidden)
    marker = root / OWNER_MARKER
    try:
        actual = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"suite-home ownership marker missing at {marker}") from exc
    expected = _owner_digest(token) if token else ""
    if not expected or not secrets.compare_digest(actual, expected):
        raise RuntimeError(f"suite-home ownership marker mismatch at {marker}")
    shutil.rmtree(root)


def is_real_service_control(command: Sequence[object] | str) -> bool:
    """Classify commands that can control a host service or gateway PID."""
    if isinstance(command, str):
        text = command
        executable = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    else:
        parts = [os.fspath(part) if isinstance(part, os.PathLike) else str(part) for part in command]
        text = " ".join(parts)
        executable = parts[0] if parts else ""
    base = Path(executable).name.lower()
    lowered = text.lower()
    managers = {
        "s6-svc",
        "s6-svscanctl",
        "s6-svwait",
        "systemctl",
        "launchctl",
        "sc",
        "sc.exe",
        "schtasks",
        "schtasks.exe",
    }
    if base in managers:
        return True
    if base in {"taskkill", "taskkill.exe", "pkill", "killall"}:
        return True
    return any(
        needle in lowered
        for needle in (
            "/command/s6-svc",
            "/run/service/",
            "systemctl ",
            "launchctl ",
            "sc.exe ",
            "schtasks ",
        )
    )


def _cli() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("--base")
    create.add_argument("--forbid", action="append", default=[])
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("path")
    cleanup.add_argument("--token-fd", type=int, required=True)
    cleanup.add_argument("--forbid", action="append", default=[])
    args = parser.parse_args()
    if args.action == "create":
        path, token = create_suite_home(args.base, args.forbid)
        print(f"{path}\t{token}")
    else:
        with os.fdopen(args.token_fd, encoding="utf-8") as authority:
            token = authority.read(4097)
        if len(token) > 4096 or "\n" in token.rstrip("\n"):
            raise RuntimeError("invalid suite-home cleanup authority transport")
        cleanup_suite_home(args.path, token.rstrip("\n"), args.forbid)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

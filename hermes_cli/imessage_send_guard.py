#!/usr/bin/env python3
"""Fail-closed wrapper for the local ``imsg`` CLI.

Read-only commands pass through unchanged. Before any ``imsg send`` call, the
wrapper atomically records an intent keyed by logical thread and normalized
payload. An identical send is blocked for 24 hours even when the first
transport attempt times out or Apple Messages readback is ambiguous.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

TTL_SECONDS = 24 * 60 * 60
DEFAULT_REAL_BINARY = "/opt/homebrew/opt/imsg/libexec/imsg"
BLOCKED_EXIT = 73
KILL_SWITCH_EXIT = 75
UNRESOLVED_TARGET_EXIT = 76


class UnresolvedTargetError(RuntimeError):
    pass


def _user_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or _user_home() / ".hermes")


def _guard_root() -> Path:
    return _home() / "state" / "imessage-send-guard"


def _kill_switch_active() -> bool:
    switch = _home() / "state" / "imessage-outbound.disabled"
    try:
        switch.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PermissionError("unable to verify automated iMessage kill switch") from exc
    return True


def _prune_expired_intents(intents: Path) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - (TTL_SECONDS * 2)
    try:
        candidates = list(intents.glob("*.json"))[:1000]
    except OSError as exc:
        raise PermissionError("unable to inspect iMessage intent store") from exc
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PermissionError("unable to prune iMessage intent store") from exc


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def canonical_recipient(value: str) -> str:
    """Match Photon/Spectrum's identity for one Apple direct-message target."""
    target = value.strip()
    parts = target.split(";", 2)
    if (
        len(parts) == 3
        and parts[0].lower() in {"any", "imessage", "sms"}
        and parts[1] == "-"
    ):
        return f"dm:{parts[2].strip().lower()}"
    if target.lower().startswith("dm:"):
        return f"dm:{target[3:].strip().lower()}"
    if target.startswith("+") or "@" in target:
        return f"dm:{target.lower()}"
    return target


def canonical_kind(kind: str) -> str:
    value = kind.strip().lower()
    return "text" if value == "richlink" else value


def _option(args: list[str], name: str) -> str | None:
    try:
        idx = args.index(name)
    except ValueError:
        return None
    return args[idx + 1] if idx + 1 < len(args) else None


def _db_path(args: list[str]) -> Path:
    return Path(_option(args, "--db") or _user_home() / "Library" / "Messages" / "chat.db").expanduser()


def _lookup_chat_guid(args: list[str], column: str, value: str) -> str | None:
    try:
        with sqlite3.connect(f"file:{_db_path(args)}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute(f"SELECT guid FROM chat WHERE {column} = ? ORDER BY ROWID DESC LIMIT 1", (value,)).fetchone()
            return str(row[0]) if row and row[0] else None
    except (OSError, sqlite3.Error):
        return None


def canonical_target(args: list[str]) -> str | None:
    chat_guid = _option(args, "--chat-guid")
    if chat_guid:
        return canonical_recipient(chat_guid)
    chat_id = _option(args, "--chat-id")
    if chat_id:
        guid = _lookup_chat_guid(args, "ROWID", chat_id)
        if not guid:
            raise UnresolvedTargetError("cannot resolve --chat-id to an Apple chat GUID; use read-only thread lookup or an explicit verified --chat-guid")
        return canonical_recipient(guid)
    identifier = _option(args, "--chat-identifier")
    if identifier:
        guid = _lookup_chat_guid(args, "chat_identifier", identifier)
        if not guid:
            raise UnresolvedTargetError("cannot resolve --chat-identifier to an Apple chat GUID; use read-only thread lookup or an explicit verified --chat-guid")
        return canonical_recipient(guid)
    recipient = _option(args, "--to")
    if recipient:
        return canonical_recipient(recipient)
    return None


def _attachment_payload(path_value: str, caption: str) -> str:
    path = Path(path_value).expanduser().resolve()
    try:
        stat = path.stat()
        suffix = f"\0size:{stat.st_size}\0mtime_ns:{stat.st_mtime_ns}"
    except OSError:
        suffix = "\0size:unknown\0mtime_ns:unknown"
    return f"{_normalize_text(caption)}\0file:{path}{suffix}"


def payload_for(args: list[str]) -> tuple[str, str] | None:
    text = _option(args, "--text") or ""
    file_value = _option(args, "--file")
    if file_value:
        return "attachment", _attachment_payload(file_value, text)
    if text:
        return "text", _normalize_text(text)
    return None


def fingerprint_for(kind: str, target: str, payload: str) -> str:
    identity = (
        f"v1\0{canonical_kind(kind)}\0{canonical_recipient(target)}\0{payload}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def claim_intent(kind: str, target: str, payload: str, *, source: str = "imsg-cli") -> str:
    if _kill_switch_active():
        raise PermissionError("automated iMessage outbound kill switch is active")
    canonical_target = canonical_recipient(target)
    canonical_message_kind = canonical_kind(kind)
    intents = _guard_root() / "intents"
    intents.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune_expired_intents(intents)
    fingerprint = fingerprint_for(canonical_message_kind, canonical_target, payload)
    now = datetime.now(timezone.utc).timestamp()
    bucket = int(now // TTL_SECONDS)
    # Legacy claims used the unsuffixed name.  New claims use deterministic
    # TTL buckets: contenders replacing an expired claim race on the same
    # O_EXCL path instead of unlinking a pathname another contender may have
    # just recreated.  Keeping the previous bucket in the read set preserves
    # a sliding 24-hour TTL across a bucket boundary.
    candidates = (
        intents / f"{fingerprint}.json",
        intents / f"{fingerprint}.{bucket}.json",
        intents / f"{fingerprint}.{bucket - 1}.json",
    )
    for candidate in candidates:
        try:
            age = now - candidate.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PermissionError("unable to inspect iMessage send intent") from exc
        if age < TTL_SECONDS:
            raise FileExistsError("blocked duplicate iMessage intent for the same thread and payload")

    intent_path = intents / f"{fingerprint}.{bucket}.json"
    try:
        fd = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FileExistsError("blocked duplicate iMessage intent for the same thread and payload") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "fingerprint": fingerprint, "target_hash": hashlib.sha256(canonical_target.encode()).hexdigest(), "payload_hash": hashlib.sha256(payload.encode()).hexdigest(), "kind": canonical_message_kind, "source": source, "attempted_at": datetime.now(timezone.utc).isoformat()}, handle, sort_keys=True)
        handle.write("\n")
    return fingerprint


def _real_binary() -> str:
    if os.environ.get("IMSG_GUARD_TEST_MODE") == "1":
        return os.environ.get("IMSG_GUARD_REAL_BINARY", DEFAULT_REAL_BINARY)
    for candidate in (
        DEFAULT_REAL_BINARY,
        "/usr/local/opt/imsg/libexec/imsg",
        shutil.which("imsg"),
    ):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return DEFAULT_REAL_BINARY


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    real = _real_binary()
    if not args or args[0] != "send":
        os.execv(real, [real, *args])
    try:
        target = canonical_target(args)
    except UnresolvedTargetError as exc:
        print(f"imsg-send-guard: BLOCKED: {exc}", file=sys.stderr)
        return UNRESOLVED_TARGET_EXIT
    payload = payload_for(args)
    if not target:
        print(
            "imsg-send-guard: BLOCKED: send target is missing or cannot be canonicalized",
            file=sys.stderr,
        )
        return UNRESOLVED_TARGET_EXIT
    if not payload:
        print(
            "imsg-send-guard: BLOCKED: send payload is missing or unrecognized",
            file=sys.stderr,
        )
        return UNRESOLVED_TARGET_EXIT
    kind, body = payload
    try:
        claim_intent(kind, target, body)
    except PermissionError as exc:
        print(f"imsg-send-guard: BLOCKED: {exc}", file=sys.stderr)
        return KILL_SWITCH_EXIT
    except FileExistsError as exc:
        print(f"imsg-send-guard: BLOCKED: {exc}", file=sys.stderr)
        return BLOCKED_EXIT
    os.execv(real, [real, *args])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())

"""Trusted capability-matrix and host approval-key resolution.

Only safe profile configuration and skill/plugin manifests are fingerprinted.
Credential values, .env files, auth stores, browser data, and account identifiers
are never read by this module.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
from typing import Any, Mapping

from .policy import ApprovalError, CapabilityProfile, HmacApprovalVerifier


MATRIX_RELATIVE_PATH = Path("policy") / "capability-profiles.json"
APPROVAL_KEYS_RELATIVE_PATH = Path("policy") / "capability-approval-keys.json"
_SAFE_PROFILE_FILES = ("config.yaml", "profile.yaml", "SOUL.md")
_SAFE_PROFILE_TREES = ("skills", "plugins")
_IGNORED_NAMES = {".env", "auth.json", "state.db", "state.db-wal", "state.db-shm"}


@dataclass(frozen=True)
class CapabilityInventory:
    profiles: tuple[CapabilityProfile, ...]
    matrix_digest: str
    matrix_path: str
    errors: Mapping[str, str]


def execution_host_id() -> str:
    """Return the stable host binding used for legacy signature validation."""
    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    return host or "unknown-host"


def profile_config_fingerprint(profile_name: str) -> str:
    """Hash non-secret capability-relevant profile state deterministically."""
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

    name = normalize_profile_name(profile_name)
    if not profile_exists(name):
        raise ApprovalError("profile does not exist")
    root = get_profile_dir(name).resolve()
    digest = hashlib.sha256()
    digest.update(b"hermes-capability-profile-fingerprint-v1\0")
    digest.update(name.encode("utf-8") + b"\0")
    for filename in _SAFE_PROFILE_FILES:
        path = root / filename
        _hash_safe_file(digest, root, path)
    for dirname in _SAFE_PROFILE_TREES:
        tree = root / dirname
        if not tree.is_dir() or tree.is_symlink():
            digest.update(f"{dirname}/<absent>".encode("utf-8") + b"\0")
            continue
        for path in sorted(tree.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            relative_parts = path.relative_to(tree).parts
            if any(part.startswith(".") for part in relative_parts):
                continue
            if path.name in _IGNORED_NAMES or path.suffix in {".pyc", ".pyo"}:
                continue
            if "__pycache__" in path.parts:
                continue
            if dirname == "skills" and not (
                path.name == "SKILL.md"
                or any(part in {"references", "templates", "scripts", "assets"} for part in relative_parts)
            ):
                continue
            if dirname == "plugins" and path.suffix.lower() not in {
                ".py", ".js", ".mjs", ".cjs"
            }:
                continue
            _hash_safe_file(digest, root, path)
    return digest.hexdigest()


def load_capability_inventory(matrix_path: str | os.PathLike[str] | None = None) -> CapabilityInventory:
    """Load declared authority and attach the installed profile identity."""
    path = _resolve_matrix_path(matrix_path)
    _validate_trusted_policy_file(path, secret=False)
    raw_bytes = path.read_bytes()
    matrix_digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalError("capability matrix is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ApprovalError("capability matrix schema_version must be 1")
    rows = payload.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ApprovalError("capability matrix profiles must be a non-empty list")

    profiles: list[CapabilityProfile] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ApprovalError("capability matrix profile entries must be objects")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise ApprovalError("capability matrix profile name is required")
        if name in seen:
            raise ApprovalError(f"capability matrix contains duplicate profile {name!r}")
        seen.add(name)
        expected_fingerprint = row.get("profile_fingerprint")
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
            errors[name] = "profile_manifest_missing_fingerprint"
            continue
        try:
            actual_fingerprint = profile_config_fingerprint(name)
        except Exception:
            errors[name] = "unknown_assignee"
            continue
        # The matrix fingerprint is historical evidence, not an execution lock.
        # Routine config/SOUL edits must not disable an otherwise authorized
        # profile. Keep the live fingerprint on the result so admission state
        # still records the exact installed profile; declared authority below
        # and all task/worker admission checks remain unchanged.
        try:
            profile = CapabilityProfile(
                name=name,
                allowed_capabilities=frozenset(_string_list(row, "allowed_capabilities")),
                role=str(row.get("role") or "unclassified"),
                privacy_tier=str(row.get("privacy_tier") or "standard"),
                data_realms=tuple(_string_list(row, "data_realms", default=["standard"])),
                workspace_roots=tuple(_absolute_path_list(row, "workspace_roots")),
                toolsets=frozenset(_string_list(row, "toolsets")),
                skills=frozenset(_string_list(row, "skills")),
                mcp_families_read=frozenset(_string_list(row, "mcp_families_read")),
                mcp_families_mutation=frozenset(_string_list(row, "mcp_families_mutation")),
                profile_fingerprint=actual_fingerprint,
                source=f"{path}#{matrix_digest}",
            )
        except ApprovalError:
            raise
        except Exception as exc:
            raise ApprovalError(f"invalid capability matrix entry for {name!r}") from exc
        profiles.append(profile)
    return CapabilityInventory(tuple(profiles), matrix_digest, str(path), errors)


def load_approval_verifier() -> HmacApprovalVerifier | None:
    """Load host-owned HMAC verification keys without exposing their values."""
    from hermes_constants import get_default_hermes_root

    path = Path(get_default_hermes_root()) / APPROVAL_KEYS_RELATIVE_PATH
    if not path.exists():
        return None
    _validate_trusted_policy_file(path, secret=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalError("capability approval key store is invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ApprovalError("capability approval key store schema_version must be 1")
    raw_keys = payload.get("hmac_sha256_keys")
    if not isinstance(raw_keys, Mapping) or not raw_keys:
        raise ApprovalError("capability approval key store has no keys")
    keys: dict[str, bytes] = {}
    for issuer, encoded in raw_keys.items():
        if not isinstance(issuer, str) or not isinstance(encoded, str):
            raise ApprovalError("capability approval key store entries are invalid")
        try:
            key = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ApprovalError("capability approval key encoding is invalid") from exc
        if len(key) < 32:
            raise ApprovalError("capability approval keys must be at least 256 bits")
        keys[issuer] = key
    return HmacApprovalVerifier(keys)


def _resolve_matrix_path(matrix_path: str | os.PathLike[str] | None) -> Path:
    from hermes_constants import get_default_hermes_root

    if matrix_path is None:
        env_path = os.environ.get("HERMES_CAPABILITY_MATRIX_PATH", "").strip()
        matrix_path = env_path or (Path(get_default_hermes_root()) / MATRIX_RELATIVE_PATH)
    path = Path(matrix_path).expanduser()
    if not path.is_absolute():
        raise ApprovalError("capability matrix path must be absolute")
    return path


def _validate_trusted_policy_file(path: Path, *, secret: bool) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise ApprovalError(f"trusted policy file is unavailable: {path}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ApprovalError("trusted policy file must be a regular non-symlink file")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ApprovalError("trusted policy file must be owned by the current user")
    if st.st_mode & 0o022:
        raise ApprovalError("trusted policy file must not be group/world writable")
    if secret and st.st_mode & 0o077:
        raise ApprovalError("capability approval key store must be mode 0600 or stricter")


def _hash_safe_file(digest: "hashlib._Hash", root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix()
    if path.name in _IGNORED_NAMES:
        return
    if not path.exists():
        digest.update(relative.encode("utf-8") + b"\0<absent>\0")
        return
    if not path.is_file() or path.is_symlink():
        digest.update(relative.encode("utf-8") + b"\0<unsafe>\0")
        return
    data = path.read_bytes()
    digest.update(relative.encode("utf-8") + b"\0")
    digest.update(hashlib.sha256(data).digest())
    digest.update(b"\0")


def _string_list(row: Mapping[str, Any], key: str, *, default: list[str] | None = None) -> list[str]:
    value = row.get(key, default if default is not None else [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ApprovalError(f"capability matrix {key} must be a string list")
    if len(value) != len(set(value)):
        raise ApprovalError(f"capability matrix {key} must not contain duplicates")
    return value


def _absolute_path_list(row: Mapping[str, Any], key: str) -> list[str]:
    value = _string_list(row, key)
    if any(not item.startswith("/") for item in value):
        raise ApprovalError(f"capability matrix {key} must contain absolute paths")
    return value

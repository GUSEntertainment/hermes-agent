"""Browser targets + headless account creation for the vault tools.

- ``chrome_current``: Chris's real Chrome (agent360 profile) exposed on the local CDP port. The vault
  tools attach their own supervisor to it so fills go over the CDP WebSocket only (never argv), with the
  same exact-origin checks as the Browser Use session.
- ``generate_password`` / ``store_in_accounts_vault``: server-side strong password generation and an
  encrypted mirror into the Margot /accounts store (AES-256-GCM, same record format as account-edit.ts).
  Nothing here returns, logs or prints a secret value.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import string
import uuid
from pathlib import Path
from typing import Optional

CHROME_CURRENT = "chrome_current"
BROWSER_USE = "browser_use"
CHROME_CURRENT_CDP = os.environ.get("HERMES_CHROME_CURRENT_CDP", "http://127.0.0.1:9222")
ACCOUNT_INDEX = Path(os.environ.get("AGENT_OS_ACCOUNT_INDEX",
                                    str(Path.home() / ".hermes/private/account-dashboard/index.json")))


def normalize_target(value: Optional[str]) -> str:
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in {"chrome", "chrome_current", "current", "real_chrome"}:
        return CHROME_CURRENT
    return BROWSER_USE


def supervisor_key(task_id: str, target: str) -> str:
    return f"{CHROME_CURRENT}:{task_id}" if target == CHROME_CURRENT else task_id


def chrome_current_supervisor(task_id: str):
    """Attach (idempotently) a CDP supervisor to Chrome Current. Loopback only."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override

    if not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/?$", CHROME_CURRENT_CDP):
        return None  # never attach to a non-loopback endpoint
    ws = _resolve_cdp_override(CHROME_CURRENT_CDP)
    if not ws or "/devtools/browser/" not in ws:
        return None
    policy, timeout_s = _get_dialog_policy_config()
    return SUPERVISOR_REGISTRY.get_or_start(task_id=supervisor_key(task_id, CHROME_CURRENT), cdp_url=ws,
                                            dialog_policy=policy, dialog_timeout_s=timeout_s)


_SYMBOLS = "!@#$%^&*-_=+?"


def generate_password(length: int = 24) -> str:
    """CSPRNG password with upper/lower/digit/symbol guaranteed (sites with composition rules)."""
    length = max(16, min(int(length or 24), 64))
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _SYMBOLS for c in pw)):
            return pw


def _private_json(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if st.st_mode & 0o077:
            raise PermissionError("account index must be private")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        os.close(fd)


def _atomic_private_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def store_in_accounts_vault(label: str, origin: str, identifier: str, password: str,
                            owner: str = "chris") -> Optional[str]:
    """Mirror a login into the Margot /accounts store. Returns the account id, or None when the store
    is unavailable (the Hermes vault copy is still authoritative for fills)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    index = _private_json(ACCOUNT_INDEX)
    if index.get("version") != 1 or not os.path.isabs(index.get("sourceDirectory", "")):
        return None
    root = Path(os.path.realpath(index["sourceDirectory"]))
    key_hex = (root / ".encryption-key").read_text().split("\n", 1)[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        return None
    key = bytes.fromhex(key_hex)
    record_id = str(uuid.uuid4())
    enc = {}
    aes = AESGCM(key)
    for field, value in (("Username", identifier), ("Password", password)):
        iv = secrets.token_bytes(12)
        ct = aes.encrypt(iv, value.encode("utf-8"), None)
        enc[field] = {"iv": iv.hex(), "tag": ct[-16:].hex(), "data": ct[:-16].hex()}
    del key, aes
    now = _dt.datetime.now(_dt.timezone.utc)
    host = origin.split("://", 1)[-1]
    record = {"id": record_id, "title": f"{owner.title()} — {label} account login",
              "description": f"Created by Margot for {origin}. Password generated server-side; never shown in chat.",
              "fields": ["Username", "Password"], "createdAt": now.isoformat(),
              "expiresAt": (now + _dt.timedelta(days=3650)).isoformat(), "submitted": True,
              "encryptedData": enc}
    _atomic_private_write(root / f"{record_id}.json", json.dumps(record, indent=2))
    base = re.sub(r"[^a-z0-9-]+", "-", f"{label}-{host}".lower()).strip("-")[:60] or "account"
    ids = {row.get("id") for row in index.get("items", [])}
    acct_id, n = base, 2
    while acct_id in ids:
        acct_id, n = f"{base}-{n}"[:80], n + 1
    index.setdefault("items", []).append({"id": acct_id, "owner": owner, "label": f"{label} ({host})"[:100],
                                          "record": record_id})
    _atomic_private_write(ACCOUNT_INDEX, json.dumps(index, indent=2))
    return acct_id

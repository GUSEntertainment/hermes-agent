"""Margot Accounts vault (~/.openclaw/workspace/.secrets) as a READ-ONLY login backend.

Source files are never written, moved or deleted. Only website logins are listed:
an item needs a password field AND a resolvable web origin (explicit ``service_url``-style
field, or the curated keyword map below). API keys (.txt), tokens, OAuth blobs, cards,
banking and other people's private folders are skipped. ``*.enc`` files are decrypted
in-process with the vault's standard openssl method (key file path, never argv secret).

Secrets stay server-side: ``list_items`` returns metadata only; ``resolve_password`` is
called by browser_vault_fill at fill time after the exact-origin check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.vault_backends.base import LoginBackend
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "/Users/Chris/.openclaw/workspace/.secrets"
DEFAULT_KEY = "/Users/Chris/projects/mission-control/secure-data/.encryption-key"

# Top-level folders that never hold Chris's website logins (or are someone else's / archives).
_SKIP_DIRS = {
    ".revoked", "agent-archives", "backups", "model-backups", "task-tests", "generated",
    "payment-vault", "hiring-applicant-intake", "contractor-invoice-ledger", "recipients",
    "tesla-proxy", "tesla-telemetry", "system", "intake-refs", "team-docs", "metricool-browser-profile",
    "travel",
}
_ALLOWED_PEOPLE = {"chris"}  # people/<name>/… : only Chris's own folder
_PASSWORD_KEYS = ("password", "custom:password", "custom:password or login secret", "pass")
_IDENT_KEYS = (("email", "email"), ("account_email", "email"), ("username", "username"), ("login", "username"))
_URL_KEYS = ("service_url", "login_url", "url", "admin_url", "website", "site")
_SKIP_NAME_WORDS = ("card", "bank", "payment", "i-9", "insurance", "direct-deposit", "oauth", "token", "api")

# filename keyword -> origins (exact; every listed origin is a fill target, nothing inferred).
ORIGIN_MAP: Dict[str, Tuple[str, ...]] = {
    "apple": ("https://appstoreconnect.apple.com", "https://idmsa.apple.com",
              "https://developer.apple.com", "https://appleid.apple.com"),
    "huggingface": ("https://huggingface.co",),
    "zenbusiness": ("https://www.zenbusiness.com", "https://app.zenbusiness.com"),
    "instagram": ("https://www.instagram.com",),
    "godaddy": ("https://sso.godaddy.com",),
    "linkedin": ("https://www.linkedin.com",),
    "dropbox": ("https://www.dropbox.com",),
    "facebook": ("https://www.facebook.com",),
    "google-admin": ("https://accounts.google.com",),
    "app-gusentertainment": ("https://app.gusentertainment.com",),
    "mission-control": ("https://app.gusentertainment.com",),
    "onelegal": ("https://www.onelegal.com",),
    "homes-alive": ("https://www.homesalivepets.com",),
    "amilia": ("https://app.amilia.com",),
}


def _decrypt(path: Path, key: str) -> Optional[bytes]:
    if not Path(key).is_file():
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — argv list; key is a file path, not a secret value
            ["openssl", "enc", "-aes-256-cbc", "-d", "-salt", "-pbkdf2", "-pass", f"file:{key}", "-in", str(path)],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _flatten(doc) -> Optional[Dict]:
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("data"), dict):
        merged = {k: v for k, v in doc.items() if k != "data"}
        merged.update(doc["data"])
        return merged
    return doc


def _lower(d: Dict) -> Dict[str, object]:
    return {str(k).strip().lower(): v for k, v in d.items()}


def _password(d: Dict) -> str:
    low = _lower(d)
    for k in _PASSWORD_KEYS:
        v = low.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _origins(rel: str, d: Dict) -> Tuple[str, ...]:
    low = _lower(d)
    out: List[str] = []
    for k in _URL_KEYS:
        v = low.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            try:
                o = normalize_origin(v)
            except Exception:
                continue
            if o not in out:
                out.append(o)
    name = rel.lower()
    for word, origins in ORIGIN_MAP.items():
        if word in name:
            for o in origins:
                if o not in out:
                    out.append(o)
            break
    # Google sign-in entries (Accounts page "Sign-in method: Google", or a google-named login file)
    # bind to accounts.google.com so they fill behind any "Sign in with Google" button (t_12a85d17).
    if _is_google_signin(rel, low) and GOOGLE_SIGNIN_ORIGIN not in out:
        out.append(GOOGLE_SIGNIN_ORIGIN)
    return tuple(out)


GOOGLE_SIGNIN_ORIGIN = "https://accounts.google.com"


def _is_google_signin(rel: str, low: Dict[str, object]) -> bool:
    for k, v in low.items():
        if ("sign-in method" in k or "signin method" in k or "login method" in k or k == "method") \
                and isinstance(v, str) and "google" in v.lower():
            return True
    stem = Path(rel).stem.lower()
    return stem.startswith("google-") and "/" not in rel.replace("\\", "/")  # top-level google-*.json only


class AccountsLoginBackend(LoginBackend):
    name = "accounts"
    display_name = "Margot Accounts vault"
    prefix = "acct:"

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.root = Path(str(cfg.get("root") or DEFAULT_ROOT))
        self.key = str(cfg.get("key_file") or DEFAULT_KEY)
        self._cache: Dict[str, Tuple[float, Optional[VaultItemMeta]]] = {}

    def _candidates(self):
        if not self.root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root)
            parts = rel_dir.parts
            if parts and (parts[0] in _SKIP_DIRS or parts[0].startswith(".")):
                dirnames[:] = []
                continue
            if parts and parts[0] == "people" and len(parts) >= 2 and parts[1] not in _ALLOWED_PEOPLE:
                dirnames[:] = []
                continue
            if "backups" in parts:
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.endswith(".json") or fn.endswith(".enc"):
                    if fn.endswith(".enc.json"):
                        continue  # wrapped per-service envelopes with their own key scheme; not openssl
                    yield Path(dirpath) / fn

    def _load(self, path: Path) -> Optional[Dict]:
        try:
            raw = _decrypt(path, self.key) if path.suffix == ".enc" else path.read_bytes()
            return _flatten(json.loads(raw)) if raw else None
        except Exception:
            return None

    def _meta_for(self, path: Path) -> Optional[VaultItemMeta]:
        rel = str(path.relative_to(self.root))
        if any(w in path.stem.lower() for w in _SKIP_NAME_WORDS):
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._cache.get(rel)
        if hit and hit[0] == mtime:
            return hit[1]
        meta = None
        d = self._load(path)
        if d and not d.get("deleted") and _password(d):
            origins = _origins(rel, d)
            if origins:
                low = _lower(d)
                ident, itype = None, None
                for k, t in _IDENT_KEYS:
                    v = low.get(k)
                    if isinstance(v, str) and v.strip():
                        ident, itype = v.strip(), t
                        break
                label = str(low.get("label") or low.get("title") or path.stem)
                handle = self.prefix + hashlib.sha256(rel.encode()).hexdigest()[:16]
                meta = VaultItemMeta(id=handle, kind="login", label=label, origin=origins[0], created_at="",
                                     identifier_type=itype, identifier=ident, allowed_origins=origins)
        self._cache[rel] = (mtime, meta)
        return meta

    def _index(self) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        for p in self._candidates():
            m = self._meta_for(p)
            if m is not None:
                out[m.id] = p
        return out

    def list_items(self) -> List[VaultItemMeta]:
        metas = []
        for p in self._candidates():
            m = self._meta_for(p)
            if m is not None:
                metas.append(m)
        return metas

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        p = self._index().get(handle)
        return self._meta_for(p) if p else None

    def resolve_password(self, handle: str) -> str:
        p = self._index().get(handle)
        d = self._load(p) if p else None
        return _password(d) if d else ""


# ── Payment cards (t_12a85d17) ──────────────────────────────────────────────
# Only these explicit Chris/GUS card files are exposed, as kind="payment" with NO bound origin:
# browser_vault_fill asks Chris to confirm on every fill, naming the exact checkout origin.
PAYMENT_FILES: Tuple[str, ...] = ("people/chris/payment.enc", "gus/gus-debit-card.enc")
_PAY_PREFIX = "acctpay:"


def _card_secret(d: Dict) -> Dict[str, str]:
    low = _lower(d)
    pick = lambda *ks: next((str(low[k]).strip() for k in ks if isinstance(low.get(k), (str, int)) and str(low[k]).strip()), "")
    out = {"card_number": "".join(ch for ch in pick("card_number", "number") if ch.isdigit()),
           "cardholder_name": pick("name_on_card", "cardholder_name", "name"),
           "cvc": pick("card_cvv", "cvv", "cvc", "card_cvc"),
           "billing_postal_code": pick("billing_zip", "billing_postal_code", "zip")}
    exp = pick("card_expiry", "expiry", "exp")
    digits = [p for p in exp.replace("-", "/").split("/") if p.strip()]
    if len(digits) == 2:
        mm, yy = digits[0].strip(), digits[1].strip()
        if len(mm) == 4:  # YYYY/MM
            mm, yy = yy, mm
        out["exp_month"] = mm.zfill(2)
        out["exp_year"] = yy if len(yy) == 4 else "20" + yy[-2:]
    if not out["billing_postal_code"]:
        import re as _re
        m = _re.search(r"\b(\d{5})(?:-\d{4})?\s*$", pick("billing_address"))
        if m:
            out["billing_postal_code"] = m.group(1)
    return {k: v for k, v in out.items() if v}


def _pay_handle(rel: str) -> str:
    return _PAY_PREFIX + hashlib.sha256(rel.encode()).hexdigest()[:16]


class _PaymentMixin:
    def _payment_metas(self) -> List[VaultItemMeta]:
        metas = []
        for rel in PAYMENT_FILES:
            p = self.root / rel
            d = self._load(p) if p.is_file() else None
            num = _card_secret(d).get("card_number", "") if d else ""
            if len(num) < 12:
                continue
            owner = "GUS" if rel.startswith("gus/") or "gus" in str(_lower(d).get("business", "")).lower() else "Chris"
            metas.append(VaultItemMeta(id=_pay_handle(rel), kind="payment", label=f"{owner} card ending {num[-4:]}",
                                       origin=None, created_at=""))
        return metas

    def _payment_path(self, handle: str) -> Optional[Path]:
        return next((self.root / r for r in PAYMENT_FILES if _pay_handle(r) == handle), None)


AccountsLoginBackend._payment_metas = _PaymentMixin._payment_metas
AccountsLoginBackend._payment_path = _PaymentMixin._payment_path
_orig_list, _orig_meta = AccountsLoginBackend.list_items, AccountsLoginBackend.get_meta


def _list_items(self) -> List[VaultItemMeta]:
    return _orig_list(self) + self._payment_metas()


def _get_meta(self, handle: str) -> Optional[VaultItemMeta]:
    if handle.startswith(_PAY_PREFIX):
        return next((m for m in self._payment_metas() if m.id == handle), None)
    return _orig_meta(self, handle)


def _resolve_secret(self, handle: str) -> Dict[str, str]:
    if handle.startswith(_PAY_PREFIX):
        p = self._payment_path(handle)
        d = self._load(p) if p and p.is_file() else None
        return _card_secret(d) if d else {}
    return {"password": self.resolve_password(handle)}


AccountsLoginBackend.list_items = _list_items
AccountsLoginBackend.get_meta = _get_meta
AccountsLoginBackend.resolve_secret = _resolve_secret
AccountsLoginBackend.owns = lambda self, h: h.startswith(self.prefix) or h.startswith(_PAY_PREFIX)

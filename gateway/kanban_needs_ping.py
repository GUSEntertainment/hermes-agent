"""One Margot ask per "Needs from Chris" card, on iMessage + Telegram.

Chris's Oct 7 card-clarity spec, item 5 (t_2c12c11b), hardened by the
t_b338f418 acceptance pass:

* When ``task_needs`` says a blocked/review/triage card is waiting on Chris,
  Margot sends exactly one message on iMessage (Photon home channel) and one
  on Telegram (Chris's DM) with the plain ask and a deep link to the card.
* Sensitive asks get an exact-field secure form BEFORE the card is presented
  (every tick, independent of sending, including baselined asks). The form is
  bound to the ask version by digest, so a changed ask gets a fresh form. A
  sensitive ask never takes a plain-text answer.
* A reply to either message is bound to the exact channel, chat, sender and
  message id, and to the ask version that message carried. The ask is consumed
  atomically (one write, no matter how many channels reply at once); a reply
  to a stale ask version is refused before anything is written.
* The answer goes through the Margot app's own answer path
  (``scripts/card-answer-cli.ts`` -> ``runCardAction``), so holds, refusals,
  sensitive guards and triage release behave exactly as in the app, and the
  acknowledgement reports truthfully whether the card actually moved.
* Delivery is per channel with retry: a failed channel is retried with
  backoff; an ask with no delivered channel stays retryable, never "sent".
* Every tick writes a liveness receipt (``needs_pings_meta``).

State lives in a ``needs_pings`` side table on the board DB. Only the owner's
private board is watched. Asks that existed before this watcher first ran are
baselined (no ping burst) but still receive their secure form.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

BOARD = "chris-home"
APP_ORIGIN = "https://margot.gusentertainment.com"
# Deep link to the exact card (t_3ca94d12): the board opens ?card=<id>.
APP_LINK = APP_ORIGIN + "/work/board?hermes=1&board={board}&card={task_id}"
# One-tap approve page; the token is opaque and only its sha256 is stored.
APPROVE_LINK = APP_ORIGIN + "/a/{token}"
APPROVE_TTL_SECONDS = 7 * 86_400
SECURE_PREFIX = "Secure form for this ask"
SECURE_ORIGIN = os.environ.get("AGENT_OS_SECURE_FORMS_ORIGIN", "https://secure.gusentertainment.com").rstrip("/")
SECURE_KEY_FILE = os.environ.get(
    "AGENT_OS_SECURE_PORTAL_KEY_FILE",
    "/Users/Chris/.openclaw/workspace/.secrets/secure-portal-api-key.txt",
)
APP_DIR = os.environ.get("AGENT_OS_APP_DIR", "/Users/Chris/projects/gus-agent-os-stable")
OPEN_STATUSES = ("blocked", "review", "triage")
MAX_SEND_ATTEMPTS = 6
RETRY_BACKOFF = (60, 300, 900, 1800, 3600, 3600)
FORM_RETRY_SECONDS = 600
FORM_TTL_MS = 7 * 86_400_000

# Mirrors gus-agent-os src/lib/margot/card-clarity.ts isSensitiveAsk.
SENSITIVE = re.compile(
    r"\b(password|passcode|passphrase|log ?in|sign[- ]?in|credential|api key|secret|bank|routing|"
    r"account number|card number|credit card|debit card|cvv|cvc|ssn|social security|last 4|"
    r"one[- ]time codes?|verification codes?|identity check|2fa|otp|pin|passport|"
    r"driver'?s licen[cs]e|date of birth|tax id|ein|itin|\bid number)\b",
    re.IGNORECASE,
)

# Exact secure-form fields per requested item. Mirrors
# gus-agent-os src/lib/margot/secure-fields.ts (same order, same labels).
FIELD_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(user ?name|user id|login email|log ?in|sign[- ]?in|credentials?)\b", re.I), ["custom:Username or email"]),
    (re.compile(r"\b(password|passphrase|log ?in|sign[- ]?in|credentials?)\b", re.I), ["custom:Password"]),
    (re.compile(r"\bpasscode\b", re.I), ["custom:Passcode"]),
    (re.compile(r"\bapi key\b", re.I), ["custom:API key"]),
    (re.compile(r"\bsecret\b", re.I), ["custom:Secret"]),
    (re.compile(r"\b(one[- ]time codes?|verification codes?|2fa|otp)\b", re.I), ["custom:Verification code"]),
    (re.compile(r"\bpin\b", re.I), ["custom:PIN"]),
    (re.compile(r"\brouting\b", re.I), ["custom:Routing number"]),
    (re.compile(r"\baccount number\b", re.I), ["custom:Account number"]),
    (re.compile(r"\b(card number|credit card|debit card)\b", re.I), ["card_number", "card_expiry", "card_cvv"]),
    (re.compile(r"\b(cvv|cvc)\b", re.I), ["card_cvv"]),
    (re.compile(r"\b(ssn|social security)\b", re.I), ["custom:Social Security number"]),
    (re.compile(r"\blast 4\b", re.I), ["custom:Last 4 digits"]),
    (re.compile(r"\bpassport\b", re.I), ["passport_number", "passport_expiry"]),
    (re.compile(r"\bdriver'?s licen[cs]e\b", re.I), ["custom:Driver's license number"]),
    (re.compile(r"\bdate of birth\b", re.I), ["custom:Date of birth"]),
    (re.compile(r"\b(tax id|ein|itin)\b", re.I), ["custom:Tax ID"]),
    (re.compile(r"\bid number\b", re.I), ["custom:ID number"]),
    (re.compile(r"\bbank\b", re.I), ["custom:Bank name", "custom:Account number", "custom:Routing number"]),
]

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS needs_pings (
    task_id        TEXT NOT NULL,
    ask_digest     TEXT NOT NULL,
    ask            TEXT NOT NULL,
    sensitive      INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL,           -- baseline | pending | sending | sent | answering | answered
    telegram_chat  TEXT,
    telegram_msg   TEXT,
    photon_chat    TEXT,
    photon_msg     TEXT,
    sent_at        INTEGER,
    answered_at    INTEGER,
    answered_via   TEXT,
    PRIMARY KEY (task_id, ask_digest)
)
"""
# Columns added by the t_b338f418 hardening (idempotent ALTERs).
EXTRA_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_retry_at": "INTEGER",
    "form_url": "TEXT",
    "form_attempt_at": "INTEGER",
    "closed_telegram": "INTEGER",
    "closed_photon": "INTEGER",
    "answer_result": "TEXT",
}


def is_sensitive(ask: Optional[str]) -> bool:
    return bool(ask and SENSITIVE.search(ask))


def digest(ask: str) -> str:
    return hashlib.sha256((ask or "").strip().encode()).hexdigest()[:24]


def secure_fields(ask: str) -> list[str]:
    """Exact fields for what the ask requests; never a generic 'Anything else'."""
    out: list[str] = []
    for rx, fields in FIELD_RULES:
        if rx.search(ask or ""):
            for f in fields:
                if f not in out:
                    out.append(f)
    if not out:
        label = re.sub(r"\s+", " ", ask or "").strip()
        out.append("custom:" + (label if len(label) <= 70 else label[:69] + "…"))
    return out[:20]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_SQL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(needs_pings)").fetchall()}
    for col, ddl in EXTRA_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE needs_pings ADD COLUMN {col} {ddl}")
    conn.execute("CREATE TABLE IF NOT EXISTS needs_pings_meta (k TEXT PRIMARY KEY, v TEXT)")
    if not conn.execute("SELECT 1 FROM needs_pings_meta WHERE k='baseline_at'").fetchone():
        conn.execute("INSERT INTO needs_pings_meta VALUES ('baseline_at', ?)", (str(int(time.time())),))
    conn.commit()


def _baseline_at(conn) -> int:
    row = conn.execute("SELECT v FROM needs_pings_meta WHERE k='baseline_at'").fetchone()
    return int(row[0]) if row else int(time.time())


def _meta_set(conn, k: str, v: Any) -> None:
    conn.execute("INSERT INTO needs_pings_meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, v if isinstance(v, str) else json.dumps(v, sort_keys=True)))
    conn.commit()


def liveness(conn) -> dict:
    """Watcher liveness receipt: last tick time, age and stats (no secrets)."""
    rows = dict(conn.execute("SELECT k, v FROM needs_pings_meta").fetchall())
    last = int(rows.get("last_tick_at") or 0)
    return {"last_tick_at": last, "age_seconds": int(time.time()) - last if last else None,
            "last_tick": json.loads(rows["last_tick"]) if rows.get("last_tick") else None}


# ---------------------------------------------------------------- secure form

def _secure_rx():
    return re.compile(re.escape(SECURE_ORIGIN) + r"/secure/[0-9a-f-]{36}", re.I)


def secure_url_for_ask(conn, task_id: str, ask: str) -> Optional[str]:
    """Newest issued form bound to THIS ask version (``[ask:<digest>]``)."""
    tag = f"[ask:{digest(ask)}]"
    rx = _secure_rx()
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC", (task_id,)
    ).fetchall()
    for (body,) in rows:
        body = str(body)
        if body.startswith(SECURE_PREFIX) and tag in body:
            m = rx.search(body)
            if m:
                return m.group(0)
    return None


def _secure_key() -> Optional[str]:
    try:
        key = open(SECURE_KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        return None
    return key or None


def form_status(url: str, *, timeout: float = 8.0) -> Optional[dict]:
    """{'submitted': bool, 'expired': bool} from the form service, or None."""
    import urllib.request

    m = re.search(r"/secure/([0-9a-f-]{36})$", url or "", re.I)
    if not m:
        return None
    try:
        with urllib.request.urlopen(f"{SECURE_ORIGIN}/api/secure/{m.group(1)}", timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode() or "{}")
    except Exception:
        return None
    return {"submitted": data.get("submitted") is True, "expired": data.get("expired") is True}


def create_secure_form(ask: str, *, timeout: float = 8.0) -> Optional[str]:
    """Same request shape as the Margot app's createSecureForm (exact fields)."""
    key = _secure_key()
    if not key:
        return None
    import urllib.request

    body = json.dumps({
        "title": "Margot — secure answer",
        "description": f"{ask} This goes to your Vault, never onto the card.",
        "fields": secure_fields(ask),
        "expiresIn": FORM_TTL_MS,
    }).encode()
    req = urllib.request.Request(
        f"{SECURE_ORIGIN}/api/secure", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https origin
            data = json.loads(resp.read().decode() or "{}")
    except Exception:
        return None
    url = data.get("url") if isinstance(data, dict) else None
    if isinstance(url, str) and re.fullmatch(r"https://\S+/secure/[0-9a-f-]{36}", url, re.I):
        return url
    return None


def ensure_secure_form(conn, task_id: str, ask: str) -> Optional[str]:
    """Bound, unexpired form for this exact ask version; created if missing.

    The card comment carries only the link and the ask digest, never values.
    """
    existing = secure_url_for_ask(conn, task_id, ask)
    if existing:
        st = form_status(existing)
        if st is None or not st["expired"]:
            return existing
    url = create_secure_form(ask)
    if not url:
        return None
    from hermes_cli import kanban_db as kb

    kb.add_comment(conn, task_id, "margot",
                   f"{SECURE_PREFIX} [ask:{digest(ask)}] (values go to the Vault, never the card): {url}")
    return url


# ---------------------------------------------------------------- text

def mint_approve_link(conn, task_id: str, ask: str, now: Optional[int] = None) -> str:
    """Opaque single-use approve token for THIS ask version (t_3ca94d12).

    Only sha256(token) is stored; the app enforces owner session, expiry,
    same ask version and single use. Returns the URL to text.
    """
    import secrets

    now = int(now or time.time())
    conn.execute("CREATE TABLE IF NOT EXISTS needs_approve_links (token_hash TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                 "ask_digest TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL)")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO needs_approve_links VALUES (?,?,?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), task_id, digest(ask), now + APPROVE_TTL_SECONDS, now))
    conn.execute("DELETE FROM needs_approve_links WHERE expires_at < ?", (now - 86_400,))
    conn.commit()
    return APPROVE_LINK.format(token=token)


def ping_text(title: str, ask: str, *, board: str, secure_url: Optional[str],
              task_id: str = "", approve_url: Optional[str] = None) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()[:120]
    lines = [f"Waiting on you: {ask}", f"({title})" if title else ""]
    if secure_url:
        lines.append(f"Please use the secure form, not a reply: {secure_url}")
    else:
        if approve_url:
            lines.append(f"Approve or decline in one tap: {approve_url}")
        lines.append("Reply to this message to answer, or open the card:")
    lines.append(APP_LINK.format(board=board, task_id=task_id) if task_id
                 else APP_ORIGIN + f"/work/board?hermes=1&board={board}")
    return "\n".join(l for l in lines if l)


def photon_caption(ask: str) -> str:
    """One short plain line for iMessage; the link follows as its own card."""
    ask = re.sub(r"\s+", " ", ask or "").strip()
    if len(ask) > 160:
        ask = ask[:157].rstrip() + "..."
    return f"Need your OK: {ask} Tap the card below to approve or decline, or reply here."


def ask_kind(ask: str, title: str = "") -> Optional[str]:
    """t_fd14aee8: approve | do | report, from the SAME classifier the one-tap
    page uses (scripts/ask-kind-cli.ts). Only an approve ask gets approve
    wording and a one-tap link; a report is not texted at all. None = the
    classifier could not run (caller sends a plain note, no approve link)."""
    script = os.path.join(APP_DIR, "scripts", "ask-kind-cli.ts")
    tsx = os.path.join(APP_DIR, "node_modules", ".bin", "tsx")
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"}
    try:
        proc = subprocess.run([tsx, script], input=json.dumps({"ask": ask or "", "title": title or ""}),
                              capture_output=True, text=True, timeout=30, cwd=APP_DIR, env=env)
        kind = json.loads((proc.stdout or "").strip().splitlines()[-1]).get("kind")
    except Exception:
        return None
    return kind if kind in ("approve", "do", "report") else None


def note_text(title: str, ask: str, *, board: str, task_id: str) -> str:
    """Plain 'needs you' note: no approve wording, no one-tap link."""
    title = re.sub(r"\s+", " ", title or "").strip()[:120]
    lines = [f"Needs you: {ask}", f"({title})" if title else "",
             "Reply here when it's done, or open the card:",
             APP_LINK.format(board=board, task_id=task_id) if task_id
             else APP_ORIGIN + f"/work/board?hermes=1&board={board}"]
    return "\n".join(l for l in lines if l)


# ---------------------------------------------------------------- answer path

def _status(conn, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else None


def record_answer(board: str, task_id: str, text: str, via: str) -> dict:
    """Answer through the Margot app's own ``runCardAction('answer')``.

    Returns {'ok': bool, 'moved': bool, 'message': str}. Same guards as the
    app: sensitive refusal, keep-paused refusal, full-record holds, evidence
    bound triage release, Chris attribution and attachments.
    """
    script = os.path.join(APP_DIR, "scripts", "card-answer-cli.ts")
    tsx = os.path.join(APP_DIR, "node_modules", ".bin", "tsx")
    payload = json.dumps({"board": board, "taskId": task_id, "text": text.strip()[:4000], "via": via})
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"}
    try:
        proc = subprocess.run([tsx, script], input=payload, capture_output=True, text=True,
                              timeout=60, cwd=APP_DIR, env=env)
        line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else "{}"
        res = json.loads(line)
    except Exception:
        return {"ok": False, "moved": False, "message": ""}
    return {"ok": res.get("ok") is True, "moved": res.get("moved") is True,
            "message": str(res.get("message") or "")[:300]}


# ---------------------------------------------------------------- planning

def _open_asks(conn):
    try:
        return conn.execute(
            "SELECT n.task_id, n.needs_from_chris, n.updated_at, t.title, t.status "
            "FROM task_needs n JOIN tasks t ON t.id=n.task_id "
            "WHERE n.waiting_on='chris' AND n.needs_from_chris!=''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def current_ask(conn, task_id: str) -> Optional[tuple[str, str]]:
    """(ask, digest) when the card is open and waiting on Chris, else None."""
    if _status(conn, task_id) not in OPEN_STATUSES:
        return None
    try:
        row = conn.execute("SELECT needs_from_chris, waiting_on FROM task_needs WHERE task_id=?",
                           (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row[1] != "chris" or not (row[0] or "").strip():
        return None
    return row[0], digest(row[0])


CARD_PING_COOLDOWN = 4 * 3600

def due_and_closed(conn, now: Optional[int] = None) -> tuple[list[dict], list[dict]]:
    """Return (asks to deliver now, delivered asks that are now answered)."""
    ensure_table(conn)
    now = int(now or time.time())
    base = _baseline_at(conn)
    due = []
    for task_id, ask, updated_at, title, status in _open_asks(conn):
        if status not in OPEN_STATUSES:
            continue
        d = digest(ask)
        row = conn.execute(
            "SELECT state, attempts, next_retry_at, telegram_msg, photon_msg FROM needs_pings "
            "WHERE task_id=? AND ask_digest=?", (task_id, d)).fetchone()
        item = {"task_id": task_id, "ask": ask, "digest": d, "title": title or "",
                "sensitive": is_sensitive(ask)}
        if row is None:
            # One card = one ping per 4h, however the ask is reworded (a looping worker
            # rephrased the same login ask ~20x in an hour and each version texted Chris).
            recent = conn.execute(
                "SELECT 1 FROM needs_pings WHERE task_id=? AND sent_at>=?",
                (task_id, now - CARD_PING_COOLDOWN)).fetchone()
            if recent:
                continue
            if int(updated_at or 0) < base:
                conn.execute(
                    "INSERT OR IGNORE INTO needs_pings(task_id, ask_digest, ask, sensitive, state) "
                    "VALUES (?,?,?,?, 'baseline')", (task_id, d, ask, int(item["sensitive"])))
                continue
            due.append(item)
            continue
        state, attempts, next_retry_at, tg_msg, ph_msg = row
        # Retry a channel that has not delivered yet (per-channel receipt).
        if state in ("pending", "sent") and (not tg_msg or not ph_msg) \
                and int(attempts or 0) < MAX_SEND_ATTEMPTS and int(next_retry_at or 0) <= now:
            item["retry"] = True
            due.append(item)
    conn.commit()

    closed = []
    for r in conn.execute("SELECT task_id, ask_digest, sent_at, telegram_chat, telegram_msg, "
                          "photon_chat, photon_msg, ask FROM needs_pings "
                          "WHERE state IN ('sent','pending')").fetchall():
        task_id, d, sent_at, tg_chat, tg_msg, ph_chat, ph_msg, ask = r
        cur = current_ask(conn, task_id)
        status = _status(conn, task_id)
        chris_comment = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND lower(author)='chris' AND created_at>=?",
            (task_id, int(sent_at or now))).fetchone() if sent_at else None
        ask_changed = cur is None or cur[1] != d
        if chris_comment or status not in OPEN_STATUSES or ask_changed:
            closed.append({"task_id": task_id, "digest": d, "telegram_chat": tg_chat, "telegram_msg": tg_msg,
                           "photon_chat": ph_chat, "photon_msg": ph_msg, "ask": ask,
                           "via": "card" if chris_comment else ("moved" if status not in OPEN_STATUSES else "ask changed")})
    return due, closed


def sensitive_needing_form(conn, now: Optional[int] = None) -> list[dict]:
    """Open sensitive asks (incl. baseline/triage) without a bound form yet."""
    ensure_table(conn)
    now = int(now or time.time())
    out = []
    for task_id, ask, _u, _t, status in _open_asks(conn):
        if status not in OPEN_STATUSES or not is_sensitive(ask):
            continue
        if secure_url_for_ask(conn, task_id, ask):
            continue
        row = conn.execute("SELECT form_attempt_at FROM needs_pings WHERE task_id=? AND ask_digest=?",
                           (task_id, digest(ask))).fetchone()
        if row and row[0] and now - int(row[0]) < FORM_RETRY_SECONDS:
            continue
        out.append({"task_id": task_id, "ask": ask, "digest": digest(ask)})
    return out


def claim(conn, item: dict) -> bool:
    """First delivery claim for a new ask (exactly once)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO needs_pings(task_id, ask_digest, ask, sensitive, state) VALUES (?,?,?,?, 'sending')",
        (item["task_id"], item["digest"], item["ask"], int(item["sensitive"])))
    conn.commit()
    return cur.rowcount == 1


def claim_retry(conn, item: dict, now: int) -> bool:
    cur = conn.execute(
        "UPDATE needs_pings SET next_retry_at=? WHERE task_id=? AND ask_digest=? "
        "AND state IN ('pending','sent') AND COALESCE(next_retry_at,0)<=?",
        (now + 30, item["task_id"], item["digest"], now))
    conn.commit()
    return cur.rowcount == 1


def record_delivery(conn, item: dict, *, telegram: tuple, photon: tuple, now: Optional[int] = None) -> str:
    """Store per-channel receipts. 'sent' only when a channel delivered."""
    now = int(now or time.time())
    row = conn.execute("SELECT telegram_chat, telegram_msg, photon_chat, photon_msg, attempts, sent_at "
                       "FROM needs_pings WHERE task_id=? AND ask_digest=?",
                       (item["task_id"], item["digest"])).fetchone()
    tg_chat, tg_msg, ph_chat, ph_msg, attempts, sent_at = row or (None, None, None, None, 0, None)
    tg_chat, tg_msg = (telegram[0], telegram[1]) if telegram[1] else (tg_chat or telegram[0], tg_msg)
    ph_chat, ph_msg = (photon[0], photon[1]) if photon[1] else (ph_chat or photon[0], ph_msg)
    attempts = int(attempts or 0) + 1
    delivered = bool(tg_msg or ph_msg)
    state = "sent" if delivered else "pending"
    backoff = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
    conn.execute(
        "UPDATE needs_pings SET state=?, sent_at=?, telegram_chat=?, telegram_msg=?, photon_chat=?, photon_msg=?, "
        "attempts=?, next_retry_at=? WHERE task_id=? AND ask_digest=? AND state IN ('sending','pending','sent')",
        (state, sent_at or (now if delivered else None), tg_chat, tg_msg, ph_chat, ph_msg, attempts,
         now + backoff, item["task_id"], item["digest"]))
    conn.commit()
    return state


# Back-compat for callers/tests of the original module.
def mark_sent(conn, item: dict, *, telegram: tuple, photon: tuple) -> None:
    record_delivery(conn, item, telegram=telegram, photon=photon)


def mark_answered(conn, task_id: str, d: str, via: str) -> bool:
    cur = conn.execute(
        "UPDATE needs_pings SET state='answered', answered_at=?, answered_via=? "
        "WHERE task_id=? AND ask_digest=? AND state IN ('sent','pending')", (int(time.time()), via, task_id, d))
    conn.commit()
    return cur.rowcount == 1


def consume_for_reply(conn, task_id: str, d: str, via: str) -> str:
    """Atomically consume one ask version for a reply.

    Returns 'ok' (this reply owns the answer), 'answered' (already consumed),
    or 'stale' (the card's ask changed or it left the open states).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT state FROM needs_pings WHERE task_id=? AND ask_digest=?",
                           (task_id, d)).fetchone()
        if not row or row[0] in ("answering", "answered"):
            conn.execute("COMMIT")
            return "answered"
        cur = current_ask(conn, task_id)
        if cur is None or cur[1] != d:
            conn.execute("UPDATE needs_pings SET state='answered', answered_at=?, answered_via='ask changed' "
                         "WHERE task_id=? AND ask_digest=? AND state IN ('sent','pending')",
                         (int(time.time()), task_id, d))
            conn.execute("COMMIT")
            return "stale"
        conn.execute("UPDATE needs_pings SET state='answering', answered_via=? WHERE task_id=? AND ask_digest=?",
                     (via, task_id, d))
        conn.execute("COMMIT")
        return "ok"
    except Exception:
        conn.execute("ROLLBACK")
        raise


def finish_reply(conn, task_id: str, d: str, result: dict) -> None:
    receipt = json.dumps({"ok": result.get("ok"), "moved": result.get("moved")})
    if result.get("ok"):
        conn.execute("UPDATE needs_pings SET state='answered', answered_at=?, answer_result=? "
                     "WHERE task_id=? AND ask_digest=? AND state='answering'",
                     (int(time.time()), receipt, task_id, d))
    else:  # nothing was written: give the ask back so another reply can answer
        conn.execute("UPDATE needs_pings SET state='sent', answered_via=NULL, answer_result=? "
                     "WHERE task_id=? AND ask_digest=? AND state='answering'", (receipt, task_id, d))
    conn.commit()


def match_reply(conn, platform: str, reply_to: str, chat_id: Optional[str] = None) -> Optional[dict]:
    if platform == "telegram":
        msg_col, chat_col = "telegram_msg", "telegram_chat"
    else:
        msg_col, chat_col = "photon_msg", "photon_chat"
    row = conn.execute(
        f"SELECT task_id, ask_digest, sensitive, state, ask, {chat_col} FROM needs_pings WHERE {msg_col}=?",  # noqa: S608
        (str(reply_to),)).fetchone()
    if not row:
        return None
    if chat_id is not None and runner_norm(str(row[5] or "")) != runner_norm(str(chat_id)):
        return None
    return {"task_id": row[0], "digest": row[1], "sensitive": bool(row[2]), "state": row[3], "ask": row[4]}


# ---------------------------------------------------------------- gateway glue

def _owner_telegram_chat() -> Optional[str]:
    first = (os.environ.get("TELEGRAM_ALLOWED_USERS") or "").split(",")[0].strip()
    return first or None


def _destinations(runner) -> dict:
    from gateway.config import Platform

    out = {}
    tg = _owner_telegram_chat()
    adapter = getattr(runner, "adapters", {}).get(Platform.TELEGRAM)
    if tg and adapter is not None:
        out["telegram"] = (adapter, tg)
    try:
        home = runner.config.get_home_channel(Platform("photon"))
        ph_adapter = getattr(runner, "adapters", {}).get(Platform("photon"))
    except Exception:
        home, ph_adapter = None, None
    if home and ph_adapter is not None and getattr(home, "chat_id", None):
        out["photon"] = (ph_adapter, str(home.chat_id))
    return out


async def _send(adapter, chat_id: str, text: str, logical_id: str) -> Optional[str]:
    from gateway.kanban_delivery_receipts import send_once

    meta = {"logical_message_id": logical_id}
    try:
        res = await send_once(adapter, chat_id, meta, text=text)
    except Exception as exc:
        logger.warning("needs ping: send failed on %s: %s", getattr(adapter, "platform", "?"), type(exc).__name__)
        return None
    if getattr(res, "success", False) and getattr(res, "message_id", None):
        return str(res.message_id)
    return None


async def _mark_channel_answered(dest: dict, item: dict, via: str) -> dict:
    note = "✅ Answered" + (f" ({via})" if via else "") + " — nothing more needed here."
    out: dict[str, Optional[bool]] = {"telegram": None, "photon": None}
    tg = dest.get("telegram")
    if tg and item.get("telegram_msg"):
        try:
            await tg[0].edit_message(item["telegram_chat"] or tg[1], item["telegram_msg"],
                                     f"{note}\nWas: {item['ask']}", finalize=True)
            out["telegram"] = True
        except Exception:
            out["telegram"] = False
            logger.debug("needs ping: telegram mark-answered failed", exc_info=True)
    ph = dest.get("photon")
    if ph and item.get("photon_msg"):
        try:
            await ph[0]._add_reaction(item["photon_chat"] or ph[1], item["photon_msg"], "👍")
            out["photon"] = True
        except Exception:
            out["photon"] = False
            logger.debug("needs ping: photon tapback failed", exc_info=True)
    return out


def _store_closure(conn, item: dict, receipt: dict) -> None:
    conn.execute("UPDATE needs_pings SET closed_telegram=?, closed_photon=? WHERE task_id=? AND ask_digest=?",
                 (None if receipt["telegram"] is None else int(receipt["telegram"]),
                  None if receipt["photon"] is None else int(receipt["photon"]),
                  item["task_id"], item["digest"]))
    conn.commit()


async def tick(runner, board: str = BOARD) -> dict:
    import asyncio

    from hermes_cli import kanban_db as kb

    stats = {"sent": 0, "pending": 0, "closed": 0, "forms": 0}
    dest = _destinations(runner)
    now = int(time.time())

    def _with_conn(fn):
        conn = kb.connect(board=board)
        try:
            return fn(conn)
        finally:
            conn.close()

    due, closed = await asyncio.to_thread(_with_conn, lambda c: due_and_closed(c, now))

    # Secure forms first, independent of sending (baseline + triage included).
    def _forms(conn):
        made = 0
        # Asks about to be pinged get their form in the send path; a baseline
        # row here would make the send claim lose and suppress the ping.
        due_keys = {(d["task_id"], d["digest"]) for d in due} if dest else set()
        for it in sensitive_needing_form(conn, now):
            if (it["task_id"], it["digest"]) in due_keys:
                continue
            conn.execute("INSERT OR IGNORE INTO needs_pings(task_id, ask_digest, ask, sensitive, state) "
                         "VALUES (?,?,?,1,'baseline')", (it["task_id"], it["digest"], it["ask"]))
            conn.execute("UPDATE needs_pings SET form_attempt_at=? WHERE task_id=? AND ask_digest=?",
                         (now, it["task_id"], it["digest"]))
            conn.commit()
            url = ensure_secure_form(conn, it["task_id"], it["ask"])
            if url:
                conn.execute("UPDATE needs_pings SET form_url=? WHERE task_id=? AND ask_digest=?",
                             (url, it["task_id"], it["digest"]))
                conn.commit()
                made += 1
        return made
    stats["forms"] = await asyncio.to_thread(_with_conn, _forms)

    for item in closed:
        won = await asyncio.to_thread(_with_conn, lambda c, i=item: mark_answered(c, i["task_id"], i["digest"], i["via"]))
        if won:
            receipt = await _mark_channel_answered(dest, item, "")
            await asyncio.to_thread(_with_conn, lambda c, i=item, r=receipt: _store_closure(c, i, r))
            stats["closed"] += 1

    if dest:
        for item in due:
            def _prep(conn, item=item):
                ok = claim_retry(conn, item, now) if item.get("retry") else claim(conn, item)
                if not ok:
                    return None, False, None, None
                if not item["sensitive"]:
                    item["kind"] = ask_kind(item["ask"], item["title"])
                    if item["kind"] == "report":
                        # A status line that asks nothing: never texted as a need.
                        conn.execute("UPDATE needs_pings SET state='answered', answered_via='not-an-ask', "
                                     "answered_at=? WHERE task_id=? AND ask_digest=?",
                                     (now, item["task_id"], item["digest"]))
                        conn.commit()
                        return None, False, None, None
                url = ensure_secure_form(conn, item["task_id"], item["ask"]) if item["sensitive"] else None
                row = conn.execute("SELECT telegram_msg, photon_msg FROM needs_pings WHERE task_id=? AND ask_digest=?",
                                   (item["task_id"], item["digest"])).fetchone()
                approve = None
                if not item["sensitive"] and item.get("kind") == "approve":
                    try:
                        approve = mint_approve_link(conn, item["task_id"], item["ask"], now)
                    except Exception:
                        logger.warning("approve link mint failed", exc_info=True)
                return url, True, row, approve

            url, ok, have, approve = await asyncio.to_thread(_with_conn, _prep)
            if not ok:
                continue
            if item["sensitive"] and not url:
                # Never invite a plain-text answer for a sensitive ask.
                text = ping_text(item["title"], item["ask"], board=board, secure_url=None, task_id=item["task_id"]).replace(
                    "Reply to this message to answer, or open the card:",
                    "Open the card and tap “Enter securely” (don't reply with it here):")
            elif not url and item.get("kind") != "approve":
                text = note_text(item["title"], item["ask"], board=board, task_id=item["task_id"])
            else:
                text = ping_text(item["title"], item["ask"], board=board, secure_url=url,
                                 task_id=item["task_id"], approve_url=approve)
            lid = f"needs-ping:{item['task_id']}:{item['digest']}"
            have_tg, have_ph = (have or (None, None))
            tg_msg = ph_msg = None
            if "telegram" in dest and not have_tg:
                tg_msg = await _send(dest["telegram"][0], dest["telegram"][1], text, lid)
            if "photon" in dest and not have_ph:
                if approve and not item["sensitive"]:
                    # t_9fc9391a: iMessage gets a short explanation, then the
                    # approve link ALONE so it renders as the branded rich
                    # card (a URL inside prose is a bare, often mangled link).
                    ph_msg = await _send(dest["photon"][0], dest["photon"][1],
                                         photon_caption(item["ask"]), lid)
                    if ph_msg:
                        await _send(dest["photon"][0], dest["photon"][1], approve, lid + ":link")
                else:
                    ph_msg = await _send(dest["photon"][0], dest["photon"][1], text, lid)
            state = await asyncio.to_thread(_with_conn, lambda c, i=item, t=tg_msg, p=ph_msg: record_delivery(
                c, i, telegram=(dest.get("telegram", (None, None))[1], t),
                photon=(dest.get("photon", (None, None))[1], p)))
            stats["sent" if state == "sent" else "pending"] += 1

    await asyncio.to_thread(_with_conn, lambda c: (_meta_set(c, "last_tick_at", str(int(time.time()))),
                                                   _meta_set(c, "last_tick", stats)))
    return stats


async def watcher(runner, interval: float = 15.0) -> None:
    import asyncio

    while getattr(runner, "_running", True):
        try:
            await tick(runner)
        except Exception:
            logger.warning("needs ping watcher tick failed", exc_info=True)
        await asyncio.sleep(interval)


def _ack(result: dict, via: str) -> str:
    if not result.get("ok"):
        msg = result.get("message") or ""
        if "secure form" in msg.lower():
            return "That one needs the secure form, so I didn't put your reply on the card. Please use the secure link."
        return "I couldn't put that on the card just now — please answer in the Margot app."
    if result.get("moved"):
        return "Got it — on the card, and the agent is picking it back up."
    return "Got it — it's on the card. It stays paused until its held step is approved in the Margot app."


async def maybe_handle_reply(runner, event) -> Optional[str]:
    """If ``event`` replies to a needs ping from Chris, record it on the card.

    Returns the short acknowledgement to send back, or None when the message
    is not a needs-ping reply (normal handling continues).
    """
    import asyncio

    reply_to = getattr(event, "reply_to_message_id", None)
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if not reply_to or platform not in ("telegram", "photon"):
        return None
    if getattr(source, "chat_type", None) not in ("dm", None):
        return None
    chat_id = str(getattr(source, "chat_id", "") or "")
    if platform == "telegram":
        owner = _owner_telegram_chat() or "-"
        if str(getattr(source, "user_id", "")) != owner:
            return None
    else:
        dest = _destinations(runner).get("photon")
        if not dest or runner_norm(dest[1]) != runner_norm(chat_id):
            return None
    from hermes_cli import kanban_db as kb

    def _find():
        conn = kb.connect(board=BOARD)
        try:
            ensure_table(conn)
            return match_reply(conn, platform, str(reply_to), chat_id)
        finally:
            conn.close()

    hit = await asyncio.to_thread(_find)
    if not hit:
        return None
    text = (getattr(event, "text", "") or "").strip()
    if hit["state"] in ("answered", "answering"):
        return "That one's already answered — nothing more needed."
    if hit["sensitive"]:
        return "That one needs the secure form, so I didn't put your reply on the card. Please use the secure link."
    if not text:
        return None
    via = "iMessage" if platform == "photon" else "Telegram"

    def _consume():
        conn = kb.connect(board=BOARD)
        try:
            return consume_for_reply(conn, hit["task_id"], hit["digest"], via)
        finally:
            conn.close()

    won = await asyncio.to_thread(_consume)
    if won == "answered":
        return "That one's already answered — nothing more needed."
    if won == "stale":
        await tick(runner)
        return "That question changed since I asked — please check the card in the Margot app for the current ask."
    result = await asyncio.to_thread(record_answer, BOARD, hit["task_id"], text, via)
    if not isinstance(result, dict):
        result = {"ok": bool(result), "moved": bool(result), "message": ""}

    def _finish():
        conn = kb.connect(board=BOARD)
        try:
            finish_reply(conn, hit["task_id"], hit["digest"], result)
        finally:
            conn.close()

    await asyncio.to_thread(_finish)
    if result.get("ok"):
        # Close the other channel too (state is already answered for this ask).
        await _close_answered(runner, hit["task_id"], hit["digest"], via)
    return _ack(result, via)


async def _close_answered(runner, task_id: str, d: str, via: str) -> None:
    import asyncio

    from hermes_cli import kanban_db as kb

    def _row():
        conn = kb.connect(board=BOARD)
        try:
            return conn.execute("SELECT telegram_chat, telegram_msg, photon_chat, photon_msg, ask FROM needs_pings "
                                "WHERE task_id=? AND ask_digest=?", (task_id, d)).fetchone()
        finally:
            conn.close()

    r = await asyncio.to_thread(_row)
    if not r:
        return
    item = {"task_id": task_id, "digest": d, "telegram_chat": r[0], "telegram_msg": r[1],
            "photon_chat": r[2], "photon_msg": r[3], "ask": r[4]}
    receipt = await _mark_channel_answered(_destinations(runner), item, via)

    def _store():
        conn = kb.connect(board=BOARD)
        try:
            _store_closure(conn, item, receipt)
        finally:
            conn.close()

    await asyncio.to_thread(_store)


def runner_norm(chat_id: str) -> str:
    m = re.match(r"^any;-;(\+\d{6,})$", chat_id or "")
    return m.group(1) if m else (chat_id or "")

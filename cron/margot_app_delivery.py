"""Margot app thread delivery for cron jobs (t_9f210be5).

Target syntax: ``margot-app:<owner>:<threadId>``. A run posts one real assistant
message (a response-only turn) into that app conversation and records a
``work`` conversation notice, which the app already shows in Updates and speaks
when the owner has voice updates on. That is the same ledger the board's work
follow-ups use, so no new app code path is involved.

Owner binding: the job must carry a ``margot-app`` origin whose owner equals the
target owner, and the thread must be a live ``gus`` conversation owned by that
owner. A job can therefore only post into threads owned by whoever created it.

Dedup: the turn fingerprint and notice id derive from (job id, execution id or
content digest), inserted with INSERT OR IGNORE in one transaction, so retries,
parallel ticks and replays never double post.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

MARGOT_APP_PLATFORM = "margot-app"
DEFAULT_STORE = os.path.expanduser(
    "~/Library/Application Support/GUS/Margot/conversations.sqlite")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_THREAD_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SESSION_RE = re.compile(r"^app_gus_[0-9a-f]{32}$")


def store_path() -> str:
    return os.environ.get("HERMES_MARGOT_APP_STORE") or DEFAULT_STORE


def parse_token(part: str) -> Optional[tuple[str, str]]:
    """``margot-app:<owner>:<thread>`` -> (owner, thread); None if not this platform.
    Raises ValueError for a malformed margot-app token."""
    raw = (part or "").strip()
    if not raw.lower().startswith(MARGOT_APP_PLATFORM + ":"):
        return None
    bits = raw.split(":")
    if len(bits) != 3:
        raise ValueError("margot-app target must be margot-app:<owner>:<threadId>")
    owner, thread = bits[1].strip().lower(), bits[2].strip().lower()
    if not _OWNER_RE.match(owner) or not _THREAD_RE.match(thread):
        raise ValueError("margot-app target has an invalid owner or thread id")
    return owner, thread


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or store_path(), timeout=15, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def thread_owner(thread_id: str, path: Optional[str] = None) -> Optional[str]:
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT owner FROM conversations WHERE id=? AND domain='gus' AND archivedAt IS NULL",
                (thread_id,)).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else None
    except Exception:
        logger.debug("margot-app: thread owner lookup failed", exc_info=True)
        return None


def origin_for_session(session_id: str, path: Optional[str] = None) -> Optional[dict]:
    """Map an authenticated app API session (``app_gus_<hex>``) to its conversation.
    The session id is server-issued by the app and bound to exactly one conversation."""
    sid = (session_id or "").strip()
    if not _SESSION_RE.match(sid):
        return None
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT id, owner, title FROM conversations WHERE sessionId=? AND domain='gus' AND archivedAt IS NULL",
                (sid,)).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.debug("margot-app: session lookup failed", exc_info=True)
        return None
    if not row:
        return None
    conv_id, owner, title = str(row[0]), str(row[1]), str(row[2] or "")
    return {
        "platform": MARGOT_APP_PLATFORM,
        "chat_id": owner,
        "chat_name": title[:80] or None,
        "thread_id": conv_id,
        "user_id": owner,
    }


def job_creator(job: dict) -> Optional[str]:
    origin = job.get("origin")
    if isinstance(origin, dict) and str(origin.get("platform") or "").lower() == MARGOT_APP_PLATFORM:
        owner = str(origin.get("user_id") or origin.get("chat_id") or "").strip().lower()
        return owner or None
    return None


def authorize(job: dict, owner: str, thread_id: str, path: Optional[str] = None) -> Optional[str]:
    """None when allowed, else the refusal reason."""
    creator = job_creator(job)
    if not creator:
        return "margot-app delivery requires a job created from a Margot app conversation"
    if creator != owner:
        return "margot-app delivery refused: target owner is not the job's creator"
    actual = thread_owner(thread_id, path)
    if actual is None:
        return "margot-app delivery refused: thread not found or archived"
    if actual != owner:
        return "margot-app delivery refused: thread is not owned by the job's creator"
    return None


def deliver(job: dict, content: str, owner: str, thread_id: str, path: Optional[str] = None) -> Optional[str]:
    """Post one assistant message + work notice. Returns None on success (including a
    deduplicated replay), else an error string."""
    text = (content or "").strip()
    if not text:
        return None
    if len(text) > 32000:
        text = text[:32000]
    refusal = authorize(job, owner, thread_id, path)
    if refusal:
        logger.warning("Job '%s': %s", job.get("id", "?"), refusal)
        return refusal
    run_key = str(job.get("execution_id") or "") or hashlib.sha256(text.encode()).hexdigest()[:24]
    digest = hashlib.sha256(
        json.dumps(["cron", job.get("id"), owner, thread_id, run_key]).encode()).hexdigest()
    fingerprint = f"cron-{job.get('id')}-{digest[:32]}"
    notice_id = "cron-" + digest
    now = int(time.time() * 1000)
    summary = re.sub(r"\s+", " ", text).strip()[:600]
    try:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                owner_row = conn.execute(
                    "SELECT owner FROM conversations WHERE id=? AND domain='gus' AND archivedAt IS NULL",
                    (thread_id,)).fetchone()
                if not owner_row or str(owner_row[0]) != owner:
                    conn.execute("ROLLBACK")
                    return "margot-app delivery refused: thread ownership changed"
                if conn.execute("SELECT 1 FROM turns WHERE conversationId=? AND fingerprint=?",
                                (thread_id, fingerprint)).fetchone():
                    conn.execute("COMMIT")
                    job["_margot_app_deduped"] = True
                    return None
                turn_id = str(uuid.uuid4())
                payload = json.dumps({"localAction": "cron", "jobId": job.get("id")})
                conn.execute(
                    "INSERT INTO turns(id,conversationId,message,source,state,response,error,model,runId,payload,fingerprint,createdAt,updatedAt)"
                    " VALUES (?,?,'','text','completed',?,NULL,NULL,NULL,?,?,?,?)",
                    (turn_id, thread_id, text, payload, fingerprint, now, now))
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_notices(id,owner,conversationId,turnId,kind,summary,createdAt)"
                    " VALUES (?,?,?,?,'work',?,?)",
                    (notice_id, owner, thread_id, turn_id, summary, now))
                conn.execute("UPDATE conversations SET updatedAt=MAX(updatedAt,?) WHERE id=? AND owner=?",
                             (now, thread_id, owner))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Job '%s': margot-app delivery failed: %s", job.get("id", "?"), exc)
        return f"margot-app delivery failed: {type(exc).__name__}"
    return None

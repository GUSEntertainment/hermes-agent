"""Plain-English "Needs from Chris" line for blocked / review cards.

Every kanban_block and kanban_request_review records one row here:

* ``needs_from_chris`` -- one plain sentence naming the single action Chris
  must take ("Enter your bank country in Etsy"), or empty.
* ``waiting_on`` -- ``"chris"`` when that sentence is non-empty, otherwise
  ``"agents"`` (Margot / the owner handles it; the card must not surface to
  Chris as needing him).

The line is sanitized: task/run ids, paths, pids, comment numbers and model
jargon are stripped. Anything that reads like an agent-fixable obstacle (tool
error, routing, reassignment, executor bug, budget, worker overlap) is forced
to ``agents`` even if a worker wrote it as a request to Chris.

Stored in a side table so no core schema migration is required; Mission
Control and the Telegram notifier read it with a LEFT JOIN / lookup.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Optional

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_needs (
    task_id          TEXT PRIMARY KEY,
    needs_from_chris TEXT NOT NULL DEFAULT '',
    link             TEXT,
    waiting_on       TEXT NOT NULL DEFAULT 'agents',
    source           TEXT,
    updated_at       INTEGER NOT NULL
)
"""

MAX_LEN = 160

# Obstacles an agent / Margot / the operator can resolve without Chris.
_AGENT_FIXABLE = re.compile(
    r"\b(margot|operator|dispatcher|executor|worker|subagent|reassign\w*|"
    r"rout(?:e|ing|ed)|re-?queue\w*|retry|retries|tool (?:error|failure)|"
    r"traceback|exception|crash\w*|timed? ?out|timeout|budget|token limit|"
    r"rate.?limit|overlap\w*|collision|duplicate (?:worker|run)|lock|"
    r"admission|preflight|toolset|schema|artifact.?binding|digest|rebind|"
    r"same-card|continuation|escalat\w*|reviewer|peer review|qa lane|"
    r"model (?:error|override|fallback)|provider error|sqlite|migration bug|"
    r"worktree|merge conflict|ci fail\w*|test fail\w*|build fail\w*)\b",
    re.IGNORECASE,
)

_PLUMBING = [
    re.compile(r"\bt_[0-9a-f]{6,}\b", re.I),                 # task ids
    re.compile(r"\b(?:run|pid|comment|event)\s*#?\s*\d+\b", re.I),
    re.compile(r"#\d+\b"),
    re.compile(r"(?:~|/Users|/tmp|/private|/var)/\S+"),       # paths
    re.compile(r"\b[0-9a-f]{12,}\b", re.I),                  # hashes
    re.compile(r"`[^`]*`"),
    re.compile(r"\([^)]*\)"),
]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_SQL)


def sanitize(text: Optional[str]) -> str:
    if not text:
        return ""
    s = str(text).strip().splitlines()[0] if str(text).strip() else ""
    for pat in _PLUMBING:
        s = pat.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -:;,.")
    # first sentence only
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    if m:
        s = m.group(1)
    s = s.strip()
    if len(s) > MAX_LEN:
        s = s[: MAX_LEN - 1].rsplit(" ", 1)[0] + "…"
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if s and s[-1] not in ".?!…":
        s += "."
    return s


def is_agent_fixable(text: Optional[str]) -> bool:
    return bool(text) and bool(_AGENT_FIXABLE.search(str(text)))


def classify(
    *,
    explicit: Optional[str],
    reason: Optional[str],
    kind: Optional[str],
    source: str,
) -> tuple[str, str]:
    """Return ``(needs_from_chris, waiting_on)``."""
    if explicit is not None and str(explicit).strip():
        line = sanitize(explicit)
        if line and not is_agent_fixable(line) and not is_agent_fixable(reason if kind in {"capability", "transient"} else None):
            return line, "chris"
        return "", "agents"
    # No explicit field: only a clean needs_input block may surface to Chris.
    if source == "block" and kind == "needs_input" and reason and not is_agent_fixable(reason):
        line = sanitize(reason)
        if 8 <= len(line):
            return line, "chris"
    return "", "agents"


def record(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    explicit: Optional[str],
    reason: Optional[str],
    kind: Optional[str],
    source: str,
    link: Optional[str] = None,
) -> dict:
    ensure_table(conn)
    line, waiting_on = classify(explicit=explicit, reason=reason, kind=kind, source=source)
    clean_link = None
    if link and waiting_on == "chris" and re.match(r"^https?://\S+$", str(link).strip()):
        clean_link = str(link).strip()[:500]
    conn.execute(
        "INSERT INTO task_needs(task_id, needs_from_chris, link, waiting_on, source, updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
        "needs_from_chris=excluded.needs_from_chris, link=excluded.link, "
        "waiting_on=excluded.waiting_on, source=excluded.source, updated_at=excluded.updated_at",
        (task_id, line, clean_link, waiting_on, source, int(time.time())),
    )
    conn.commit()
    return {"needs_from_chris": line, "waiting_on": waiting_on, "link": clean_link}


def lookup(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT needs_from_chris, link, waiting_on FROM task_needs WHERE task_id=?",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {"needs_from_chris": row[0] or "", "link": row[1], "waiting_on": row[2] or "agents"}


def notification_line(needs: Optional[dict]) -> Optional[str]:
    """Top line for a Telegram blocked/review ping, or None to fall back."""
    if not needs:
        return None
    if needs.get("waiting_on") == "chris" and needs.get("needs_from_chris"):
        tail = f"\n{needs['link']}" if needs.get("link") else ""
        return f"Waiting on you: {needs['needs_from_chris']}{tail}"
    return "Agents handling it — nothing needed from you."

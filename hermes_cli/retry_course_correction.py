"""Retry course-correction for re-dispatched workers (Chris, t_2c12c11b c8633).

* A retried worker's context opens with the previous failure and an explicit
  instruction to change approach / repair the root cause.
* When the latest two worker failures share a fingerprint, the worker must
  record a one-line "What I changed this time:" note (via kanban_comment)
  before it may finish the run (complete / block / request review).
* Nothing here mutates failure history, clears stops, changes assignees or
  reintroduces any block-loop-to-triage cap. Holds and approval gates are
  untouched: the note is evidence, never authority.
"""
from __future__ import annotations

import json
import re
from typing import Optional

CHANGE_NOTE_PREFIX = "What I changed this time:"
CHANGE_NOTE_EVENT = "retry_change_note"
_NOTE_RE = re.compile(r"^\s*what i changed this time\s*:\s*(\S.*)$", re.I | re.S)


def parse_change_note(body: str) -> Optional[str]:
    m = _NOTE_RE.match(body or "")
    if not m:
        return None
    line = " ".join(m.group(1).split())
    return line[:400] if len(line) >= 8 else None


def _recovery(conn, task_id):
    from hermes_cli.owner_work_start import instruction_recovery
    try:
        return instruction_recovery(conn, task_id)
    except Exception:
        return None


def _current_run(conn, task_id):
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else None


def has_prior_runs(conn, task_id) -> bool:
    cur = _current_run(conn, task_id)
    row = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND id IS NOT ?",
                       (task_id, cur)).fetchone()
    return bool(row and row[0])


def state(conn, task_id) -> dict:
    rec = _recovery(conn, task_id) or {}
    latest = rec.get("latest_worker_failure")
    repeated = bool(rec.get("repeated_same_failure"))
    run_id = _current_run(conn, task_id)
    note = None
    if repeated and run_id is not None:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND run_id=? "
            "ORDER BY id DESC LIMIT 1", (task_id, CHANGE_NOTE_EVENT, run_id)).fetchone()
        if row:
            try:
                note = json.loads(row[0]).get("note")
            except (ValueError, TypeError, AttributeError):
                note = None
    return {"latest": latest, "repeated": repeated, "run_id": run_id,
            "retry": bool(latest) and has_prior_runs(conn, task_id),
            "change_note": note,
            "change_note_required": repeated and not note}


def context_lines(conn, task_id) -> list[str]:
    """Leading context block for a retried worker; empty on a first run."""
    s = state(conn, task_id)
    if not s["retry"]:
        return []
    latest = s["latest"]
    reason = (latest.get("reason") or "(no reason recorded)").strip()
    if len(reason) > 1200:
        reason = reason[:1200] + "…"
    lines = ["## RETRY: previous attempt failed — change approach",
             f"Previous failure ({latest.get('kind') or 'unknown kind'}): {reason}",
             "Do NOT repeat the approach that produced this failure. Identify the root cause "
             "and try a different method or repair the cause first. Reuse verified progress."]
    if s["repeated"]:
        lines.append(
            "The SAME failure has now repeated. Before doing anything else, post "
            f"kanban_comment(body=\"{CHANGE_NOTE_PREFIX} <one line: diagnosed cause and what you "
            "are doing differently>\"). kanban_complete, kanban_block and kanban_request_review "
            "are refused this run until that note exists, and so is every tool that changes "
            "anything (only reading/diagnosis tools and a genuine needs_input/dependency "
            "kanban_block work before the note).")
        if s["change_note"]:
            lines.append(f"Recorded change note: {s['change_note']}")
    lines.append("Original holds, explicit stops, approval gates and privacy boundaries still apply; "
                 "this retry grants no new authority. Agent-fixable tool/config/routing snags stay "
                 "with an existing agent owner, never with Chris.")
    lines.append("")
    return lines


def record_change_note(conn, task_id, body, author) -> Optional[str]:
    note = parse_change_note(body)
    if not note:
        return None
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, CHANGE_NOTE_EVENT,
                     {"note": note, "author": author}, run_id=_current_run(conn, task_id))
    conn.commit()
    return note


def gate_error(conn, task_id, tool) -> Optional[str]:
    s = state(conn, task_id)
    if s["change_note_required"]:
        return (f"{tool} refused: this retry repeats the previous failure. First post "
                f"kanban_comment(task_id='{task_id}', body='{CHANGE_NOTE_PREFIX} <diagnosed cause "
                "and what you changed>'), then try the changed approach.")
    return None


# ---------------------------------------------------------------- pre-work gate
# t_b338f418 E: the change note must precede the WORK, not just the finish.
# Until it exists, a repeated-failure retry may only read/diagnose, post the
# note, or report a genuine owner-input/dependency blocker.
PREWORK_ALLOWED = frozenset({
    "kanban_show", "kanban_comment", "kanban_heartbeat", "kanban_attachments", "kanban_list",
    "kanban_block",  # its own gate lets only needs_input/dependency through
    "read_file", "search_files", "web_search", "web_extract", "skill_view", "skills_list",
    "session_search", "shared_history", "chat_history_lookup", "tool_search", "tool_describe",
    "todo", "clarify", "vision_analyze",
})
_note_seen: dict = {}


def prework_gate(tool_name: str) -> Optional[str]:
    """Error text when ``tool_name`` must wait for the change note, else None."""
    import os

    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid or tool_name in PREWORK_ALLOWED:
        return None
    run = os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    if _note_seen.get((tid, run)):
        return None
    try:
        from hermes_cli import kanban_db as kb
        board = os.environ.get("HERMES_KANBAN_BOARD")
        conn = kb.connect(board=board) if board else kb.connect()
        try:
            s = state(conn, tid)
        finally:
            conn.close()
    except Exception:
        return None  # fail open: the finish gate still applies
    if not s["change_note_required"]:
        _note_seen[(tid, run)] = True
        return None
    return (f"{tool_name} refused: this retry repeats the previous failure. Before changing "
            f"anything, post kanban_comment(task_id='{tid}', body='{CHANGE_NOTE_PREFIX} <diagnosed "
            "cause and what you are doing differently>'). Reading and diagnosis tools still work.")

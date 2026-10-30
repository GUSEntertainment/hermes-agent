"""Read-only committed-event hints for the existing dispatcher, never admission.

Bounded polling avoids a new IPC endpoint, writer permissions or scheduler.
Lost hints, event GC, new boards and read failures retain periodic reconciliation.
"""
import asyncio
from contextlib import closing
import sqlite3
import time
from pathlib import Path

KINDS = frozenset({'created', 'promoted', 'unblocked', 'review_requested', 'changes_requested', 'triage_resumed', 'review_triage_resumed', 'review_work_resumed', 'owner_work_started', 'owner_work_requested', 'owner_handoff', 'owner_recovery_requested'})
POLL_SECONDS = 2.0
MIN_TICK_SECONDS = 5.0
BATCH = 256

class CommittedTransitions:
    def __init__(self):
        self.cursors = {}

    def changed(self, paths):
        changed = False
        for value in paths:
            path = Path(value)
            try:
                st = path.stat()
                identity = (str(path), st.st_dev, st.st_ino)
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.05)) as conn:
                    conn.execute('BEGIN')
                    latest = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events').fetchone()[0]
                    cursor = self.cursors.get(identity)
                    if cursor is None or latest < cursor:
                        self.cursors[identity] = latest
                        continue
                    rows = conn.execute('SELECT e.id,e.kind,t.status FROM task_events e LEFT JOIN tasks t ON t.id=e.task_id WHERE e.id>? ORDER BY e.id LIMIT ?', (cursor,BATCH)).fetchall()
                    if rows:
                        self.cursors[identity] = rows[-1][0]
                        changed |= any((kind in KINDS and status in ('ready','review')) or (kind == 'completed' and status == 'done') for _,kind,status in rows)
            except (OSError,sqlite3.Error):
                # Hints cannot disable periodic dispatch or create/migrate a DB.
                continue
        return changed

async def wait_for_transition(observer, paths, running, *, periodic_deadline,
                              last_tick, clock=None, sleep=None):
    """Coalesce bursts, preserve fixed periodic deadline, yield False on shutdown."""
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    pending=False
    while running():
        now=clock()
        if now>=periodic_deadline:return False
        await sleep(min(POLL_SECONDS,periodic_deadline-now))
        if not running():return False
        try:
            pending = await asyncio.to_thread(observer.changed, paths()) or pending
        except Exception:
            pass  # catalog/read failure: periodic fallback
        if pending and clock()-last_tick>=MIN_TICK_SECONDS:return True
    return False

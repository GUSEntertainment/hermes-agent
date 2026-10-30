"""Staged runtime seams. Not installed into the gateway or active learner.

resolve() must be bound by the authenticated transport adapter; never register
this module as a model-facing tool. Backfill writes need the shared lock below
at BOTH insertion and expiration, not only in semantic decision preparation.
"""
import contextlib
import fcntl
import json
from pathlib import Path
from .corrections import Envelope, LIKELY, QUESTION


def require_caught_up(status_path, *, error_counts=None):
    """Owner-approved gate: drained backlog, only source_changed errors.

    error_counts must be an aggregate from the read-only ledger at cutover;
    terminal status and count agreement prevent accepting a partial pass.
    """
    state = json.loads(Path(status_path).read_text())
    progress = state.get('progress', {})
    if state.get('status') not in {'caught_up', 'source_errors'} or any(
            progress.get(k, -1) != 0 for k in
            ('pending_units', 'processing_units', 'staged_facts')):
        raise RuntimeError('backfill_not_caught_up')
    errors = progress.get('error_units', -1)
    if errors != 0:
        if (not isinstance(errors, int) or errors < 0 or
                error_counts != {'source_changed': errors}):
            raise RuntimeError('backfill_not_caught_up')
    elif error_counts not in (None, {}):
        raise RuntimeError('backfill_not_caught_up')
    return state['run_id']


class CaptureHook:
    def __init__(self, engine, resolve):
        self.engine, self.resolve = engine, resolve

    def before_turn(self, text, *, pending_id=None):
        # Ordinary messages incur no identity lookup, database operation or AI.
        if not isinstance(text, str) or (pending_id is None and not LIKELY.search(text)):
            return {'status':'ignored', 'turn_note':None}
        authority = self.resolve()  # Current transport-bound identity only.
        if not authority or authority.get('principal') not in {'chris', *self.engine.delegates}:
            return {'status':'denied', 'turn_note':None}
        envelope = Envelope(text=text, **authority)
        result = self.engine.capture(envelope, pending_id=pending_id)
        # Append a fixed, non-secret note to this turn's existing sidecar notes.
        # Normal Margot turn delivers the question; the hook never sends a message.
        note = None
        if result['status'] == 'pending':
            note = 'No memory was changed. Ask exactly one question: '+QUESTION
        elif result['status'] == 'applied':
            note = 'The explicit correction was verified in memory; the old fact remains in history.'
        elif result['status'] == 'uncertain_write':
            note = 'The correction write could not be verified. Do not claim memory was updated.'
        return {**result, 'turn_note':note}


@contextlib.contextmanager
def lower_priority_write(journal, fact):
    """Coordinator must hold this through remote write, readback and mirror update.

    False means keep the candidate in audit as conflict_with_chris; do not
    remember it and do not expire any fact. This includes already-prepared
    supersession decisions, so an old decision cannot race a new correction.
    """
    with open(journal.path+'.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield not journal.protects(fact['entity'], fact['attribute'], fact['tier'])

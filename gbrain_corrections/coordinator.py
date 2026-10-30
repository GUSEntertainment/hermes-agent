"""Joint-cutover coordinator guards. Protect BOTH remember and forget.
Decorator spans remote write, readback and mirror sync, sharing the correction
engine's exact lock. Recursive ingest -> apply uses one lock, not a deadlock.
"""
import functools
import threading
import fcntl
from .corrections import Journal
from .mirror import sync_verified
from .native_runtime import STATE

_mutex = threading.RLock()
_local = threading.local()


def protected(fact):
    journal = getattr(_local,'journal',None)
    return bool(journal and journal.protects(fact['entity'],fact.get('attribute',''),fact['tier']))


def coordinated(fn):
    @functools.wraps(fn)
    def call(ledger,*args,**kwargs):
        if getattr(_local,'journal',None) is not None:
            return fn(ledger,*args,**kwargs)
        if not (STATE/'READY').is_file():
            return fn(ledger,*args,**kwargs)
        from agent.redact import redact_sensitive_text
        with _mutex, open(str(STATE/'corrections.sqlite')+'.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            journal=Journal(STATE/'corrections.sqlite')
            _local.journal=journal
            try:
                sync_verified(journal,ledger,redact_sensitive_text)
                result=fn(ledger,*args,**kwargs)
                sync_verified(journal,ledger,redact_sensitive_text)
                return result
            finally:
                _local.journal=None
                journal.db.close()
    return call

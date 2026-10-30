"""Payload-free monotonic phases scoped by the native server's allocated run ID.

No environment identity, user input, profile, paths, model, or exception text is
logged. Outside an explicit native run scope, timing wrappers are silent.
"""
from contextvars import ContextVar
from functools import wraps
import json
import logging
import math
import re
import time

_run = ContextVar("native_direct_timing_run", default=None)
_PHASES = frozenset({"constructor", "turn_context", "pre_llm_hook", "memory_turn_start", "memory_prefetch", "tool_schemas", "memory_initialize", "provider_resolution"})
logger = logging.getLogger("hermes.direct_turn_timing")


def timed_call(phase, fn, *args, **kwargs):
    identity = _run.get()
    if identity is None or phase not in _PHASES:
        return fn(*args, **kwargs)
    try:
        start = time.monotonic()
    except Exception:
        return fn(*args, **kwargs)
    outcome = "error"
    try:
        result = fn(*args, **kwargs)
        outcome = "ok"
        return result
    finally:
        # Telemetry must never replace the wrapped result/exception.
        try:
            elapsed = time.monotonic() - start
            if math.isfinite(elapsed) and elapsed >= 0:
                logger.info("HERMES_DIRECT_TURN_TIMING %s", json.dumps({
                    "run_id": identity, "phase": phase,
                    "seconds": round(elapsed, 6), "outcome": outcome,
                }, sort_keys=True))
        except Exception:
            pass


def timed(phase):
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            return timed_call(phase, fn, *args, **kwargs)
        return invoke
    return decorate


def run_scope(run_id, phase=None):
    # Only called with the server's persisted run_id, never a request field.
    identity = run_id if isinstance(run_id, str) and re.fullmatch(r"run_[a-f0-9]{32}", run_id) else None
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            token = _run.set(identity)
            try:
                return timed_call(phase, fn, *args, **kwargs) if phase else fn(*args, **kwargs)
            finally:
                _run.reset(token)
        return invoke
    return decorate

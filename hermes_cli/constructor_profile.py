"""Bounded constructor-only diagnostic; never emits raw profiler data or paths."""
import cProfile
from functools import wraps
import json
import logging
import math
import os
import threading
from pathlib import Path
import re
import sys

logger = logging.getLogger('hermes.constructor_profile')
_ROOT = Path(__file__).resolve().parents[1]
# Only repository code in these fixed namespaces is reportable.
_NAMESPACES = frozenset({'agent','gateway','hermes_cli','tools','providers','model_tools','run_agent','hermes_state','toolsets'})
# Read once at process module startup; no prompt/request switch exists.
_ENABLED = os.environ.get('HERMES_DIAGNOSTIC_CONSTRUCTOR_ONCE') == '1'
_sample_lock = threading.Lock()
_sample_consumed = False


def _claim_sample():
    global _sample_consumed
    with _sample_lock:
        if _sample_consumed:
            return False
        _sample_consumed = True
        return True


_ID = re.compile(r'run_[a-f0-9]{32}')
_SYMBOL = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def projection(stats):
    rows = {}
    for (filename, _line, function), (primitive, total, own, cumulative, _callers) in stats.items():
        if not isinstance(filename,str) or not isinstance(function,str) or not _SYMBOL.fullmatch(function):continue
        try:
            relative = Path(filename).resolve().relative_to(_ROOT)
        except (OSError,ValueError):continue
        if relative.suffix!='.py':continue
        parts=relative.with_suffix('').parts
        if not parts or parts[0] not in _NAMESPACES or any(not _SYMBOL.fullmatch(p) for p in parts):continue
        if any(type(n) is not int or n<0 for n in (primitive,total)):continue
        if any(not isinstance(n,(int,float)) or not math.isfinite(n) or n<0 for n in (own,cumulative)):continue
        key=('.'.join(parts),function)
        row=rows.setdefault(key,{'module':key[0],'function':function,'primitive_calls':0,'calls':0,'self_seconds':0.0,'cumulative_seconds':0.0})
        row['primitive_calls']+=primitive;row['calls']+=total;row['self_seconds']+=own;row['cumulative_seconds']+=cumulative
    result=sorted(rows.values(),key=lambda r:(-r['cumulative_seconds'],r['module'],r['function']))[:20]
    for r in result:
        r['self_seconds']=round(r['self_seconds'],6);r['cumulative_seconds']=round(r['cumulative_seconds'],6)
    return result


def profile_constructor(run_id):
    def decorate(fn):
        @wraps(fn)
        def invoke(*args,**kwargs):
            # Do not replace a debugger/profiler already installed in this thread.
            try:
                enabled = (_ENABLED and isinstance(run_id,str) and bool(_ID.fullmatch(run_id))
                           and sys.getprofile() is None and _claim_sample())
            except Exception:
                enabled = False
            if not enabled:
                return fn(*args,**kwargs)
            profiler=None
            try:
                profiler=cProfile.Profile()
                profiler.enable()
            except Exception:
                if profiler is not None:
                    try:profiler.disable()
                    except Exception:pass
                return fn(*args,**kwargs)
            outcome='error'
            try:
                result=fn(*args,**kwargs)
                outcome='ok'
                return result
            finally:
                # Capture only this invocation/thread. All reporting is best effort.
                try:
                    profiler.disable()
                    profiler.create_stats()
                    logger.info('HERMES_CONSTRUCTOR_PROFILE %s',json.dumps({'run_id':run_id,'outcome':outcome,'functions':projection(profiler.stats)},sort_keys=True))
                except Exception:
                    pass
        return invoke
    return decorate

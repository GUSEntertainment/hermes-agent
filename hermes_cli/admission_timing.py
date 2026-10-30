"""Non-authoritative monotonic timings; never reflect task content or errors."""
from contextlib import contextmanager
from functools import wraps
import json
import logging
import math
import os
import re
import time

PREFIX = 'HERMES_ADMISSION_TIMING '
PHASES = frozenset({'admission_total','profile_hash_before','profile_hash_after',
 'profile_skill_sync','discovery_subprocess','admission_validation','claim','spawn','imports',
 'plugin_discovery','effective_config','tool_schemas','skills','readiness'})
TASK = re.compile(r't_[a-f0-9]{8,64}\Z')

def emit(phase, seconds, outcome, task=None, *, stderr=False):
    try:
        if phase not in PHASES or outcome not in ('ok','failed') or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds < 0:return
        row={'phase':phase,'seconds':round(seconds,6),'outcome':outcome}
        if isinstance(task,str) and TASK.fullmatch(task):row['task']=task
        data=json.dumps(row,sort_keys=True,separators=(',',':'))
        if stderr:os.write(2,(PREFIX+data+'\n').encode())
        else:logging.getLogger('hermes.admission_timing').info('%s%s',PREFIX,data)
    except BaseException:
        pass  # Diagnostics may never change admission or mask its exception.

@contextmanager
def phase(name, task=None, *, stderr=False, clock=None):
    clock=clock or time.monotonic
    try:start=clock()
    except BaseException:start=None
    outcome='failed'
    try:
        yield
        outcome='ok'
    finally:
        if start is not None:
            try:emit(name,clock()-start,outcome,task,stderr=stderr)
            except BaseException:pass

def timed_task(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(task,*args,**kwargs):
            with phase(name,getattr(task,'id',None)):return fn(task,*args,**kwargs)
        return wrapped
    return decorate

def relay(stderr, task):
    """Only allowlisted numeric rows, never arbitrary captured stderr."""
    if not isinstance(stderr,str):return
    for line in stderr.splitlines()[:1000]:
        if not line.startswith(PREFIX) or len(line)>512:continue
        try:
            row=json.loads(line[len(PREFIX):])
            if set(row) != {'phase','seconds','outcome'}:continue
            emit(row['phase'],row['seconds'],row['outcome'],task)
        except (ValueError,TypeError):continue

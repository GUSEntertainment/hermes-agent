import os
import pathlib
import stat
from .errors import Denied

def private_directory(value):
    p=pathlib.Path(value)
    if not p.is_absolute() or p.resolve()!=p:raise Denied('Canonical directory required')
    s=p.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077:raise Denied('Private directory required')
    return s

def private_file(value):
    p=pathlib.Path(value);private_directory(p.parent)
    if p.resolve()!=p:raise Denied('Canonical file required')
    s=p.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_size>16*1024*1024:raise Denied('Private file required')
    return s

def code_file(value,interpreter=False):
    p=pathlib.Path(value)
    if not p.is_absolute() or p.parent.resolve()!=p.parent or (not interpreter and p.resolve()!=p):raise Denied('Configured code path required')
    for q in (p.parent,p.resolve()):
        s=q.stat()
        if s.st_uid not in (0,os.getuid()) or s.st_mode&0o022:raise Denied('Protected code required')
    if not p.is_file():raise Denied('Code file required')
    return str(p)  # Keep venv/bin/python, not its resolved executable.

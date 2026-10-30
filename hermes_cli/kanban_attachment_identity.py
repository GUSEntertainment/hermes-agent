"""Reuse only verified identical durable bytes already attached to this task."""
import hashlib
import os
import stat
from pathlib import Path


def _digest(path, limit, *, durable=False):
    fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|getattr(os,'O_NOFOLLOW',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>limit:
            raise ValueError('Attachment is not a bounded regular file')
        if durable and (before.st_nlink!=1 or before.st_uid!=os.getuid() or before.st_mode&0o022):
            raise ValueError('Unsafe durable attachment')
        digest=hashlib.sha256();size=0
        while chunk:=os.read(fd,1024*1024):
            size+=len(chunk)
            if size>limit:raise ValueError('Attachment grew beyond limit')
            digest.update(chunk)
        after=os.fstat(fd)
        fields=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink,s.st_uid,s.st_mode)
        if fields(before)!=fields(after) or size!=before.st_size:
            raise ValueError('Attachment changed while reading')
        current=os.stat(path,follow_symlinks=False)
        if fields(current)!=fields(after):raise ValueError('Attachment path changed')
        return size,digest.digest()
    finally:os.close(fd)


def identical_attachment(conn,task_id,source,directory,limit):
    """No row/metadata mutation; an unsafe/stale candidate is never reused."""
    source=Path(source);directory=Path(directory)
    if directory.resolve()!=directory:return None
    try:identity=_digest(source,limit)
    except (OSError,ValueError):return None
    rows=conn.execute('SELECT stored_path,size FROM task_attachments WHERE task_id=? AND size=? ORDER BY id',(task_id,identity[0]))
    for row in rows:
        candidate=Path(row['stored_path'])
        # Ignore pointers to siblings, workspace files, symlinks or ancestors.
        if candidate.parent!=directory or candidate.resolve()!=candidate:continue
        try:
            if _digest(candidate,limit,durable=True)==identity:return candidate
        except (OSError,ValueError):continue
    return None

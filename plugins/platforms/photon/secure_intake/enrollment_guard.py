"""Current enrollment gate. Protected metadata only; never copies credentials.

Reader runs inside the form service's existing registry write transaction.
Sweep is optional startup/maintenance administration and preserves all bindings.
"""
import hashlib
import json
import os
import pathlib
import sqlite3
import time
from urllib.parse import quote
from .errors import Denied
from .request_registry import encoded,identity_valid

class EnrollmentRevoked(Denied):pass

def protected_file(path):
    p=pathlib.Path(path)
    if not p.is_absolute() or p.resolve()!=p:raise Denied('Canonical protected file required')
    st=p.lstat();parent=p.parent.stat()
    if not p.is_file() or st.st_nlink!=1 or st.st_uid!=os.getuid() or st.st_mode&0o077 or parent.st_uid!=os.getuid() or parent.st_mode&0o077 or st.st_size>16*1024*1024:raise Denied('Protected file required')
    return st

def fingerprint(projection):
    # Caller supplies an explicit nonsecret projection, never a whole profile.
    return hashlib.sha256(encoded(projection).encode()).hexdigest()

class EnrollmentGuard:
    def __init__(self,registry,*,enrollment,clock=time.time):
        self.path=pathlib.Path(registry);self.enrollment,self.clock=enrollment,clock
    def _connection(self,write=False):
        before=protected_file(self.path)
        db=sqlite3.connect('file:'+quote(str(self.path))+'?mode='+('rw' if write else 'ro'),uri=True,timeout=5,isolation_level=None)
        db.row_factory=sqlite3.Row
        after=protected_file(self.path)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):db.close();raise Denied('Registry changed')
        return db
    def _state(self,db,i):
        identity_valid(i)
        row=db.execute('SELECT identity,binding,expires,revoked FROM ingress_requests WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
        grant=db.execute('SELECT identity,generation FROM intake_enrollment_grants WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
        if not row or not grant or row['identity']!=encoded(i) or grant['identity']!=encoded(i):return {'allowed':False,'revoked':False}
        if row['revoked'] or row['expires']<=self.clock():return {'allowed':False,'revoked':True}
        b=json.loads(row['binding']);p=self.enrollment(i.copy())
        allowed=(type(p) is dict and p.get('enabled') is True and p.get('revoked') is False and isinstance(p.get('generation'),str) and len(p['generation'])==64 and p['generation']==grant['generation'] and all(p.get(k)==b.get(k) for k in ('owner','tenant','sender','conversation')))
        return {'allowed':allowed,'revoked':not allowed}
    def state(self,i):
        db=None
        try:
            before=protected_file(self.path);db=self._connection();result=self._state(db,i);after=protected_file(self.path)
            return result if (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino) else {'allowed':False,'revoked':False}
        except EnrollmentRevoked:return {'allowed':False,'revoked':True}
        except Exception:return {'allowed':False,'revoked':False}
        finally:
            if db is not None:db.close()
    def allowed(self,i):return self.state(i)['allowed']
    def sweep(self):
        # No owner argument; all rows derive their exact original trusted source.
        db=self._connection(write=True)
        try:
            db.execute('BEGIN IMMEDIATE');count=0;unavailable=0
            for row in db.execute('SELECT platform,message,identity FROM ingress_requests WHERE revoked=0').fetchall():
                try:
                    state=self._state(db,json.loads(row['identity']));revoked=state['revoked']
                    if not state['allowed'] and not revoked:unavailable+=1
                except EnrollmentRevoked:revoked=True
                except Exception:revoked=False;unavailable+=1
                if revoked:
                    db.execute('UPDATE ingress_requests SET revoked=1 WHERE platform=? AND message=?',(row['platform'],row['message']))
                    db.execute('UPDATE intake_sources SET revoked=1 WHERE platform=? AND message=?',(row['platform'],row['message']))
                    count+=1
            db.execute('COMMIT')
            if unavailable:raise Denied('Enrollment reconciliation unavailable')
            return {'revoked':count}
        except BaseException:
            if db.in_transaction:db.execute('ROLLBACK')
            raise
        finally:db.close()

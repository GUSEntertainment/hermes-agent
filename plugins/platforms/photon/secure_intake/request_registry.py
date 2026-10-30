"""Protected operator-configured durable ingress registry. Not a model tool.

Only a future reviewed trusted gateway ingress may call register_trusted after
resolving the real sender, enrollment and saved credential-custody request.
These arguments are NOT themselves authentication.
"""
import contextlib
import json
import math
import re
import os
import pathlib
import sqlite3
import tempfile
import time
from .errors import Denied

IDENTITY={'platform','chat_type','conversation','sender','message','session'}
PIN_FIELDS={'subject_key','generation','phone_digest'}
BINDING={'sender','owner','tenant','conversation','request','service','purpose','expires','channel','authorized'}
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def identity_valid(i):
    if type(i) is not dict or set(i)!=IDENTITY or any(type(v) is not str or not v or len(v)>200 for v in i.values()) or i['platform']!='photon' or i['chat_type']!='dm':raise Denied('Verified ingress required')

class RequestRegistry:
    def __init__(self,path,*,clock=time.time):
        path=pathlib.Path(path)
        from .paths import private_file
        _initial=private_file(path)
        self._identity=(_initial.st_dev,_initial.st_ino)
        self.path,self.clock=path,clock
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ingress_requests(platform TEXT NOT NULL,message TEXT NOT NULL,identity TEXT NOT NULL,binding TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(platform,message))')
        os.chmod(path,0o600)
    @contextlib.contextmanager
    def _db(self):
        from .paths import private_file
        from urllib.parse import quote
        before=private_file(self.path)
        if (before.st_dev,before.st_ino)!=self._identity:raise Denied('Registry replaced')
        db=sqlite3.connect('file:'+quote(str(self.path))+'?mode=rw',uri=True,timeout=5,isolation_level=None);db.row_factory=sqlite3.Row
        after=private_file(self.path)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):db.close();raise Denied('Registry changed')
        try:yield db
        finally:db.close()
    def register_trusted(self,identity,binding):
        identity_valid(identity)
        if type(binding) is not dict:raise Denied('Exact binding required')
        pin=binding.get('purpose')=='enroll-phone-pin'
        if set(binding)!=(BINDING|PIN_FIELDS if pin else BINDING):raise Denied('Exact purpose binding required')
        if pin and (type(binding['generation'])!=int or binding['generation']<1 or type(binding['subject_key'])!=str or not binding['subject_key'] or type(binding['phone_digest'])!=str or not re.fullmatch(r'[0-9a-f]{64}',binding['phone_digest'])):raise Denied('Exact PIN enrollment required')
        if any(type(binding[k]) is not str or not binding[k] or len(binding[k])>200 for k in BINDING-{'expires','authorized'}):raise Denied('Invalid custody binding')
        expiry=binding['expires']
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=self.clock() or binding['authorized'] is not True or binding['purpose'] not in ('credential-custody','enroll-phone-pin') or binding['channel']!='photon' or any(binding[k]!=identity[k] for k in ('sender','conversation')):raise Denied('Current custody binding required')
        i,b=encoded(identity),encoded(binding)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT * FROM ingress_requests WHERE platform=? AND message=?',(identity['platform'],identity['message'])).fetchone()
                if row:
                    if row['identity']!=i or row['binding']!=b or row['revoked'] or row['expires']<=self.clock():raise Denied('Message already bound')
                else:db.execute('INSERT INTO ingress_requests(platform,message,identity,binding,expires) VALUES (?,?,?,?,?)',(identity['platform'],identity['message'],i,b,expiry))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
    def __call__(self,identity):
        identity_valid(identity)
        with self._db() as db:row=db.execute('SELECT * FROM ingress_requests WHERE platform=? AND message=?',(identity['platform'],identity['message'])).fetchone()
        if not row or row['revoked'] or row['expires']<=self.clock() or row['identity']!=encoded(identity):raise Denied('Current registered request required')
        return {'identity':json.loads(row['identity']),'binding':json.loads(row['binding'])}
    def revoke_trusted(self,identity):
        identity_valid(identity)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT identity FROM ingress_requests WHERE platform=? AND message=?',(identity['platform'],identity['message'])).fetchone()
                if not row or row['identity']!=encoded(identity):raise Denied('Registered message mismatch')
                db.execute('UPDATE ingress_requests SET revoked=1 WHERE platform=? AND message=?',(identity['platform'],identity['message']))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise

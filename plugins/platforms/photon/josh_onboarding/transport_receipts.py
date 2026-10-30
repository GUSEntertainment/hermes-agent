"""Inactive server-only transport receipt extension. No HTTP or model tool.

The verifier and enrollment callbacks are trusted server composition, never
request JSON. The verifier reads already-authenticated request/executor state.
"""
import hashlib
import json
import re
import secrets
import time
from plugins.platforms.photon.secure_intake.trusted_intake import Capture, _CAPTURE
from plugins.platforms.photon.secure_intake.request_registry import RequestRegistry, encoded
from plugins.platforms.photon.secure_intake.errors import Denied


def word(v):
    return type(v) is str and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',v))


def digest(v):
    return type(v) is str and bool(re.fullmatch(r'[a-f0-9]{64}',v))


class TransportReceipts:
    def __init__(self, registry, *, enrollment, verifier, clock=time.time):
        if type(registry) is not RequestRegistry: raise Denied('Protected registry required')
        self.registry,self.enrollment,self.verifier,self.clock=registry,enrollment,verifier,clock
        with registry._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS transport_receipts(
                handle TEXT PRIMARY KEY, platform TEXT NOT NULL, message TEXT NOT NULL,
                transport TEXT NOT NULL, digest TEXT NOT NULL, enrollment TEXT NOT NULL,
                expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                session TEXT, run TEXT, UNIQUE(platform,message))''')

    def _current_storage(self):
        # Revalidate the published pathname, not merely the already-open SQLite
        # handle: trusted callbacks may run while a deployment replaces it.
        from plugins.platforms.photon.secure_intake.paths import private_file
        state=private_file(self.registry.path)
        if (state.st_dev,state.st_ino)!=self.registry._identity:
            raise Denied('Registry replaced')

    def _enrollment(self, transport):
        # Callback must resolve transport using protected explicit enrollment.
        p=self.enrollment(tuple(transport))
        keys={'owner','profile','generation','endpoint','enabled','revoked'}
        if type(p) is not dict or set(p)!=keys or p['enabled'] is not True or p['revoked'] is not False:
            raise Denied('Current enrollment required')
        if not word(p['owner']) or p['profile']!=p['owner'] or not digest(p['generation']):
            raise Denied('Exact private profile required')
        from urllib.parse import urlsplit
        if type(p['endpoint']) is not str or len(p['endpoint'])>300: raise Denied('Exact named endpoint required')
        u=urlsplit(p['endpoint'])
        try: valid=u.port and u.hostname=='127.0.0.1' and u.scheme=='http' and u.path in (f"/p/{p['profile']}/v1/chat/completions",f"/p/{p['profile']}/v1/runs") and p['endpoint']==f'http://127.0.0.1:{u.port}{u.path}'
        except ValueError: valid=False
        if not valid: raise Denied('Exact named endpoint required')
        return encoded(p)

    def publish(self, capture):
        if type(capture) is not Capture or capture.seal is not _CAPTURE:
            raise Denied('Attested capture required')
        t=capture.transport
        if type(t) is not tuple or len(t)!=5 or t[:2]!=('photon','dm') or any(type(v) is not str or not v or len(v)>200 for v in t) or not digest(capture.digest):
            raise Denied('Bounded source required')
        if type(capture.captured) is not int or not self.clock()-300 < capture.captured <= self.clock():
            raise Denied('Current capture required')
        expiry=capture.captured+300
        p=self._enrollment(t)
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM transport_receipts WHERE platform=? AND message=?', (t[0],t[4])).fetchone()
            if row:
                if row['transport']!=encoded(t) or row['digest']!=capture.digest or row['enrollment']!=p or row['revoked'] or row['expires']<=self.clock():
                    raise Denied('Conflicting transport replay')
                handle=row['handle']
            else:
                handle=secrets.token_urlsafe(32)
                db.execute('INSERT INTO transport_receipts(handle,platform,message,transport,digest,enrollment,expires) VALUES(?,?,?,?,?,?,?)',(handle,t[0],t[4],encoded(t),capture.digest,p,expiry))
            if self._enrollment(t)!=p or expiry<=self.clock(): raise Denied('Enrollment changed')
            self._current_storage()
            db.execute('COMMIT')
        self._current_storage()
        return handle

    def claim(self, handle):
        """Resolve and atomically bind to an authenticated native run/session.

        Repeat same run/session is idempotent; this does NOT authorize repeating
        model execution. Native run lifecycle must reconcile its own outcome.
        """
        if type(handle) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{43}',handle):
            raise Denied('Receipt required')
        a=self.verifier()
        keys={'principal','profile','endpoint','digest','session','run'}
        if type(a) is not dict or set(a)!=keys or not word(a['principal']) or a['profile']!=a['principal'] or not digest(a['digest']) or any(type(a[k]) is not str or not a[k] or len(a[k])>200 for k in ('session','run')):
            raise Denied('Authenticated native run required')
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM transport_receipts WHERE handle=?',(handle,)).fetchone()
            if not r or r['revoked'] or r['expires']<=self.clock(): raise Denied('Current receipt required')
            t=json.loads(r['transport']);p=json.loads(r['enrollment'])
            if a['principal']!=p['owner'] or a['profile']!=p['profile'] or a['endpoint']!=p['endpoint'] or a['digest']!=r['digest'] or self._enrollment(t)!=r['enrollment']:
                raise Denied('Receipt authority mismatch')
            if r['session'] is not None and (r['session'],r['run'])!=(a['session'],a['run']):
                raise Denied('Receipt already claimed')
            db.execute('UPDATE transport_receipts SET session=?,run=? WHERE handle=?',(a['session'],a['run'],handle))
            if self.verifier()!=a or self._enrollment(t)!=r['enrollment'] or r['expires']<=self.clock(): raise Denied('Authority changed')
            self._current_storage()
            db.execute('COMMIT')
        self._current_storage()
        return {'identity':dict(zip(('platform','chat_type','conversation','sender','message'),t),session=a['session']), 'digest':r['digest'],'expires':r['expires'], 'run':a['run']}

    def revoke(self, handle):
        # Trusted ingress/admin only. No public registration or API endpoint.
        with self.registry._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('UPDATE transport_receipts SET revoked=1 WHERE handle=?',(handle,)).rowcount!=1: raise Denied('Unknown receipt')
            self._current_storage()
            db.execute('COMMIT')
        self._current_storage()

"""Inactive authenticated API composition. Locator is never identity authority."""
import contextvars
import hashlib
from contextlib import contextmanager
from .transport_receipts import TransportReceipts, Denied
from plugins.platforms.photon.secure_intake.request_registry import encoded
from plugins.platforms.photon.secure_intake import trusted_intake
from gateway.session_context import set_session_vars

_ACTIVE=contextvars.ContextVar('owner_api_intake',default=None)

class ApiIntake:
    def __init__(self,registry,*,enrollment,authenticated_state,publisher,service):
        self.publisher,self.service=publisher,service
        self.authenticated_state=authenticated_state
        self.receipts=TransportReceipts(registry,enrollment=enrollment,verifier=self._verify)

    def _verify(self):
        active=_ACTIVE.get()
        if not active:raise Denied('Authenticated executor required')
        # Callback reads current authenticated server principal + native run.
        state=self.authenticated_state()
        if type(state) is not dict or set(state)!={'principal','profile','endpoint','session','run'}:
            raise Denied('Authenticated executor required')
        return dict(state,digest=active[1])

    @contextmanager
    def executor(self,request,user_message):
        # Called INSIDE authenticated native executor, after session binding.
        # Only locator from HTTP; all authority comes from authenticated_state.
        header=request.headers.get('X-Hermes-Intake-Receipt')
        token=_ACTIVE.set(None)
        try:
            if header is None:
                yield
                return
            if type(user_message) is not str or not user_message or len(user_message)>10000:
                raise Denied('Exact current message required')
            _ACTIVE.set((header,hashlib.sha256(user_message.encode()).hexdigest()))
            self.receipts.claim(header)
            yield
        finally:_ACTIVE.reset(token)

    def current_claim(self):
        active = _ACTIVE.get()
        if not active:
            raise Denied('Current authenticated intake required')
        return self.receipts.claim(active[0])

    def invoke(self,args):
        active=_ACTIVE.get()
        if not active:raise Denied('Current authenticated intake required')
        claim=self.receipts.claim(active[0])
        # Run a copied context: native clear_session_vars is NOT stack-safe.
        # No caller's API platform, profile, or async-delivery state is changed.
        def run():
            i=claim['identity'];registry=self.receipts.registry
            with registry._db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM intake_sources WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
                if row:
                    if row['identity']!=encoded(i) or row['digest']!=claim['digest'] or row['expires']!=claim['expires'] or row['revoked']:raise Denied('Source already bound')
                else:
                    db.execute('INSERT INTO intake_sources(platform,message,identity,digest,expires) VALUES(?,?,?,?,?)',(i['platform'],i['message'],encoded(i),claim['digest'],claim['expires']))
                db.execute('COMMIT')
            set_session_vars(platform='photon',chat_type='dm',chat_id=i['conversation'],user_id=i['sender'],message_id=i['message'],session_id=i['session'],async_delivery=False)
            trusted_intake._CURRENT.set((encoded(i),claim['digest']))
            outer=self
            class CheckedService:
                def prepare(self,identity):
                    outer.receipts.claim(active[0])
                    result=outer.service.prepare(identity)
                    outer.receipts.claim(active[0])
                    return result
                def verified_url(self,metadata):
                    outer.receipts.claim(active[0])
                    return outer.service.verified_url(metadata)
            return self.publisher.run(args,CheckedService())
        result=contextvars.copy_context().run(run)
        self.receipts.claim(active[0])
        return result

class ApiServerVerifier:
    """Bind after _check_auth succeeds, inside the native executor.

    native_* arguments are server profile/run-record values, never JSON fields.
    The caller must capture the successful request's authenticated principal
    before its executor hop; a bare header cannot produce this context.
    """
    def __init__(self):self._state=contextvars.ContextVar('verified_api_intake_run',default=None)
    @contextmanager
    def bind(self,request,*,native_profile,native_endpoint,native_session,native_run):
        principal=request.get('hermes.authenticated_principal')
        if not principal or principal!=native_profile:raise Denied('Authenticated profile required')
        token=self._state.set(dict(principal=principal,profile=native_profile,endpoint=native_endpoint,session=native_session,run=native_run))
        try:yield
        finally:self._state.reset(token)
    def __call__(self):
        state=self._state.get()
        if state is None:raise Denied('Authenticated run required')
        return state.copy()

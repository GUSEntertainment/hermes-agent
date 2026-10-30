"""Staged native composition. No model-facing tool, background task or polling.

Activate jointly with coordinator patch. The private ready marker is written
only after the mirror and both coordinator write guards have been verified.
"""
import datetime as dt
import importlib.util
import re
from pathlib import Path
from .corrections import Engine, Journal, SolExtractor, LIKELY
from .http_transport import HTTPTransport
from .ingress import gateway_envelope, api_envelope

STATE = Path('/Users/Chris/.hermes/profiles/scarlett/scripts/gbrain-corrections')
LEARNER = Path('/Users/Chris/.hermes/profiles/scarlett/scripts/gbrain-learning')
CONFIRM = re.compile(r'^\s*update that\s+([a-f0-9]{8}):', re.I)


def make_engine():
    # Fail closed before credential access unless joint cutover is completed.
    ready = STATE/'READY'
    if not ready.is_file() or ready.stat().st_mode & 0o077: raise RuntimeError('corrections_not_activated')
    from agent.auxiliary_client import call_llm
    from agent.redact import redact_sensitive_text
    spec = importlib.util.spec_from_file_location('_gbrain_correction_client', LEARNER/'mcp_client.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class LazyClients(dict):
        def __missing__(self, tier):
            if tier not in {'business','chris-only','family'}: raise ValueError('invalid_tier')
            client = (module.Client() if tier == 'business' else
                      module.Client(tier+'-backfill', credential_path=Path('/Users/Chris/.hermes/gbrain-margot/service/realms-credentials.json')))
            self[tier] = client
            return client
    return Engine(Journal(STATE/'corrections.sqlite'),HTTPTransport(LazyClients()),
                  SolExtractor(call_llm),redact_sensitive_text)


def run_envelope(env, engine_factory=make_engine):
    if env is None: return None
    engine = engine_factory()
    try:
        engine.recover()
        confirmation = CONFIRM.match(env.text)
        pending_id = None
        if confirmation:
            rows = engine.journal.db.execute(
                "SELECT event_id FROM corrections WHERE status IN ('pending','resolved') AND event_id LIKE ?",
                (confirmation[1].lower()+'%',)).fetchall()
            if len(rows) != 1:
                return 'No memory was changed. Ask which correction the user means.'
            pending_id = rows[0]['event_id']
        result = engine.capture(env,pending_id=pending_id)
        if result['status'] == 'pending':
            return ('No memory was changed. Ask one short question: Which fact should I update, and what should it say? '
                    'Ask the user to begin the reply with "Update that '+result['event_id'][:8]+':".')
        if result['status'] == 'applied':
            return 'The correction was verified in memory; the previous fact remains in history.'
        if result['status'] in {'uncertain_write','denied','already_resolved'}:
            return 'No new correction was applied on this turn. Do not claim a new memory update.'
        return None
    finally:
        engine.journal.db.close()


def gateway_note(event):
    # Do not even import identity/enrollment or open a journal for normal chat.
    if not isinstance(getattr(event,'text',None),str) or not LIKELY.search(event.text): return None
    try:
        from agent.delegation_context import is_delegated_child_context
        from gateway import session_context as sc
        if is_delegated_child_context(): return None
        profile = sc._SESSION_PROFILE.get()
        # Native single-profile gateways deliberately bind an empty profile;
        # resolve only the host's canonical profile, never caller identity.
        if profile == '':
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name() or 'default'
        if profile not in {'default','chris','margot'}: return None
        photon = None
        if getattr(event.source.platform,'value','') == 'photon':
            from plugins.platforms.photon.secure_intake import trusted_intake as ti
            from plugins.platforms.photon.secure_intake.gateway_binding import current_identity
            from plugins.platforms.photon.secure_intake.canonical_enrollment import canonical_enrollment
            photon = (current_identity(),ti._CURRENT.get(),getattr(event,'_secure_intake_capture',None),
                      ti._CAPTURE,canonical_enrollment)
        env = gateway_envelope(event,profile,delegated=is_delegated_child_context,
                               session_context=sc,photon=photon)
        return run_envelope(env)
    except Exception:
        # No private facts, error strings, raw inbound content or provider output in logs.
        return 'The correction could not be verified. Do not claim memory was updated.'


def api_note(text, run_id, conversation_ref):
    if not isinstance(text,str) or not LIKELY.search(text): return None
    try:
        from gateway.josh_board_authority import _execution
        from agent.delegation_context import is_delegated_child_context
        env = api_envelope(text,scope=_execution,run_id=run_id,
            received_at=dt.datetime.now(dt.timezone.utc),conversation_ref=conversation_ref,
            delegated=is_delegated_child_context)
        return run_envelope(env)
    except Exception:
        return 'The correction could not be verified. Do not claim memory was updated.'

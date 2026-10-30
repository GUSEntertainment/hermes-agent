"""Native ingress resolvers, not tools. Fail closed without transport evidence.

No environment, display-name, phone-string or model-argument identity fallback.
These functions are called only inside native handlers with their original event.
"""
import datetime as dt
import hashlib
import json
import time
from .corrections import Envelope, LIKELY


def _text(var):
    value = var.get()
    return value if isinstance(value, str) else ''


def gateway_envelope(event, profile, *, delegated, session_context, photon=None):
    if delegated() or getattr(event, 'internal', False): return None
    if profile not in {'default', 'chris', 'margot'}: return None
    source = event.source
    platform = getattr(source.platform, 'value', '')
    if platform not in {'telegram', 'photon'}: return None
    # Keep existing GBrain DM audience boundary; this task grants no group access.
    if source.chat_type not in {'dm', 'private'}: return None
    if not event.message_id or not source.user_id: return None
    if (_text(session_context._SESSION_PLATFORM) != platform or
        _text(session_context._SESSION_USER_ID) != source.user_id or
        _text(session_context._SESSION_CHAT_ID) != source.chat_id or
        _text(session_context._SESSION_MESSAGE_ID) != event.message_id): return None
    if platform == 'telegram':
        raw = event.raw_message
        sender = getattr(raw, 'from_user', None)
        if (not sender or sender.is_bot or str(sender.id) != '1963997029' or
            str(sender.id) != source.user_id or source.chat_id != source.user_id or
            str(raw.message_id) != event.message_id or str(raw.chat.id) != source.chat_id): return None
        # Never use combined quoted context, forwarded text, captions or transcripts.
        if getattr(raw, 'forward_origin', None): return None
        text = raw.text
        date = getattr(raw, 'edit_date', None) or raw.date
    else:
        if photon is None: return None
        identity, witness, capture, seal, enrollment = photon
        expected = {'platform':'photon', 'chat_type':'dm', 'conversation':source.chat_id,
                    'sender':source.user_id, 'message':event.message_id}
        if any(identity.get(k) != v for k, v in expected.items()): return None
        if (not capture or capture.seal is not seal or capture.captured+3600 <= time.time() or
            capture.transport != tuple(expected[k] for k in
                ('platform','chat_type','conversation','sender','message'))): return None
        raw = event.raw_message
        content = raw.get('content', {}) if isinstance(raw, dict) else {}
        for _ in range(4):
            if not isinstance(content, dict) or content.get('type') != 'reply': break
            content = content.get('content', {})
        if not isinstance(content, dict) or content.get('type') != 'text': return None
        text = content.get('text')
        if not isinstance(text, str): return None
        digest = hashlib.sha256(text.encode()).hexdigest()
        if not witness or witness[1] != digest or capture.digest != digest: return None
        # Identity encoding comparison is supplied by the actual witness provider.
        if witness[0] != json.dumps(identity,sort_keys=True,separators=(',',':')): return None
        grant = enrollment(identity)
        if (grant.get('owner') != 'chris' or grant.get('enabled') is not True or
            grant.get('revoked') is not False or grant.get('tenant') != 'gus' or
            grant.get('sender') != source.user_id or grant.get('conversation') != source.chat_id or
            not isinstance(grant.get('generation'), str) or len(grant['generation']) != 64): return None
        date = event.timestamp
    if not isinstance(text, str) or not LIKELY.search(text): return None
    if not isinstance(date, dt.datetime) or date.tzinfo is None: return None
    # Include revision date: edited corrections are distinct idempotent events.
    reference = platform+':'+source.chat_id+':'+event.message_id+':'+date.isoformat()
    conversation = platform+':'+source.chat_id+':'+str(getattr(source,'thread_id','') or '')
    return Envelope('chris',platform,profile,'business',True,reference,date.isoformat(),
                    text,conversation_ref=conversation)


def api_envelope(text, *, scope, run_id, received_at, conversation_ref, delegated):
    """scope is execution_scope ContextVar, not request JSON.

    run_id and received_at MUST be freshly supplied by the server-owned run
    record, never accepted from the HTTP request body's provenance fields.
    """
    if not isinstance(text, str) or not LIKELY.search(text) or delegated(): return None
    current = scope.get()
    if not current: return None
    profile, principal = current
    if principal != 'chris' or profile not in {None,'default','chris','margot'}: return None
    if not isinstance(run_id, str) or not run_id or not conversation_ref: return None
    if not isinstance(received_at, dt.datetime) or received_at.tzinfo is None: return None
    return Envelope('chris','app',profile or 'default','business',True,'app-run:'+run_id,
                    received_at.isoformat(),text,conversation_ref=conversation_ref)

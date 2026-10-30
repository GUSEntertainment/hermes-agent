"""Gateway boundary for the private, fixed text-continuation service."""
import json
import subprocess
from pathlib import Path

RELEASE = Path('/Users/Chris/.hermes/phone-runtimes/live-20260920-v25-app-work')
PYTHON = '/Users/Chris/.hermes/releases/hermes-agent-20260906-kanban-lifecycle-v7/.venv/bin/python'


def live_identity():
    from gateway.session_context import get_bound_session_env
    names = {'platform': 'PLATFORM', 'chat_type': 'CHAT_TYPE', 'space': 'CHAT_ID',
             'sender': 'USER_ID', 'message_id': 'MESSAGE_ID'}
    return {k: get_bound_session_env('HERMES_SESSION_' + v) for k, v in names.items()}


def invoke(operation, identity, **fields):
    if identity.get('platform') != 'photon' or identity.get('chat_type') != 'dm':
        return {'eligible': False, 'accepted': False}
    proc = subprocess.run([PYTHON, str(RELEASE / 'processors/photon_text_continuation.py')],
                          input=json.dumps(dict(operation=operation, identity=identity, **fields)),
                          capture_output=True, text=True, timeout=50 if operation == 'submit' else 8)
    result = json.loads(proc.stdout)
    if not isinstance(result, dict):
        raise ValueError('invalid continuation response')
    return result


def capture(*, space, sender, message_id, chat_type, target, text):
    identity = dict(platform='photon', chat_type=chat_type, space=space, sender=sender, message_id=message_id)
    return invoke('capture', identity, target=target, text=text).get('eligible') is True


def submit_current(args):
    if args:
        return json.dumps({'accepted': False, 'message': 'This tool takes no arguments.'})
    from agent.delegation_context import is_delegated_child_context
    if is_delegated_child_context():
        return json.dumps({'accepted': False, 'message': 'Only the current private conversation can request the revision.'})
    try:
        return json.dumps(invoke('submit', live_identity()))
    except Exception:
        return json.dumps({'accepted': False, 'message': 'The request has not been verified as accepted. Do not claim it has started.'})


def pending_current():
    try:
        return invoke('pending', live_identity()).get('pending') is True
    except Exception:
        # Fail closed for interim execution claims when admission is unavailable.
        return True

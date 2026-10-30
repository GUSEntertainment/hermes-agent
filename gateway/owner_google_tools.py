"""Owner-pinned Google tools for an authenticated, enrolled owner (t_70638db4).

Proxies the operator-installed owner-google MCP launcher for the CURRENT
authenticated principal only. No owner, script path or token is accepted from
tool arguments; the launcher itself is pinned to one owner and never returns
tokens. Write tools keep their own confirmation notes from the MCP server.
"""
import json
import re
import subprocess
import threading
from pathlib import Path

TOOL = 'owner_google'
ROOT = Path('/Users/Chris/.hermes/agent-os/owner-google-mcp')
ACTIONS = (
    'google_account_status', 'gmail_search', 'gmail_read_message',
    'calendar_list_calendars', 'calendar_list_events', 'gmail_send',
    'gmail_create_draft', 'calendar_create_event', 'calendar_update_event',
    'calendar_delete_event',
)


def _launcher():
    from gateway.owner_device_tools import _binding
    owner = _binding()[0]  # authenticated, enrolled, same-profile owner only
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', owner):
        raise PermissionError('Invalid owner')
    path = ROOT / f'run-{owner}.sh'
    if path.is_symlink() or not path.is_file() or path.resolve().parent != ROOT:
        raise PermissionError('No Google connection installed for this owner')
    text = path.read_text()
    if f'OWNER_GOOGLE_MCP_OWNER={owner}' not in text:
        raise PermissionError('Google launcher is not pinned to this owner')
    return str(path)


def enabled():
    try:
        _launcher()
        return True
    except Exception:
        return False


def invoke(args, **kwargs):
    if type(args) is not dict or set(args) - {'action', 'arguments'}:
        raise ValueError('Only action and arguments are supported')
    action = args.get('action')
    if action not in ACTIONS:
        raise ValueError('Unknown Google action')
    arguments = args.get('arguments') or {}
    if type(arguments) is not dict:
        raise ValueError('arguments must be an object')
    launcher = _launcher()
    lines = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': action, 'arguments': arguments}},
    ]
    proc = subprocess.Popen([launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(60, proc.kill)
    timer.start()
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write('\n'.join(json.dumps(x) for x in lines) + '\n')
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get('id') == 2:
                result = msg.get('result') or {}
                text = ''.join(c.get('text', '') for c in result.get('content', []))
                return json.dumps({'ok': not result.get('isError'), 'result': text})
        return json.dumps({'ok': False, 'result': 'Google service gave no answer'})
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def register():
    if not enabled():
        return
    from tools.registry import registry
    registry.register(name=TOOL, toolset='josh_status_pilot', handler=invoke, schema={
        'name': TOOL,
        'description': ("Use your own connected Google account. action=google_account_status shows the account and "
                        "whether send/calendar-edit access is granted (call it yourself before answering any connection "
                        "question). Other actions: gmail_search{query,limit}, gmail_read_message{id}, "
                        "calendar_list_calendars, calendar_list_events{timeMin,timeMax,query,limit}, "
                        "gmail_create_draft/gmail_send{to[],subject,body,cc,bcc,threadId}, "
                        "calendar_create_event/calendar_update_event/calendar_delete_event. Send and calendar edits "
                        "only on the owner's explicit instruction. If access is read-only, the owner reconnects at "
                        "https://margot.gusentertainment.com/connections."),
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS)},
            'arguments': {'type': 'object'}},
            'required': ['action'], 'additionalProperties': False}})

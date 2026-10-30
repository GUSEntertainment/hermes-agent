"""Remote commands for an authenticated, enrolled owner's mapped computer.

No profile/host/key is accepted from tool arguments; no backend credential,
skill, cache or environment is copied to the personal computer.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import yaml
from plugins.platforms.photon.secure_intake.errors import Denied

TOOL = 'owner_computer'
PROFILE_ROOT = Path('/Users/Chris/.hermes/profiles')
KEY_ROOT = Path('/Users/Chris/.ssh')


def _binding():
    from gateway.josh_board_authority import _execution, admitted_board, restricted
    from hermes_constants import get_hermes_home
    from plugins.platforms.photon.secure_intake.enrollment_guard import protected_file
    scope = _execution.get()
    if not scope or scope[0] != scope[1] or not restricted():
        raise PermissionError('Authenticated enrolled device owner required')
    admitted_board(None)  # includes revocation and delegated-child checks
    home = Path(get_hermes_home())
    if home != PROFILE_ROOT / scope[0]:
        raise PermissionError('Device profile mismatch')
    return profile_binding(scope[0])


def profile_binding(owner):
    from plugins.platforms.photon.secure_intake.enrollment_guard import protected_file
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", owner):
        raise PermissionError("Invalid device owner")
    home = PROFILE_ROOT / owner
    path = home / 'config.yaml'
    before = protected_file(path)
    raw = path.read_bytes()
    signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if signature(protected_file(path)) != signature(before):
        raise PermissionError('Device configuration changed')
    config = yaml.safe_load(raw)
    if config.get('owner_device_enabled') is not True:
        raise PermissionError('Computer access is not enabled')
    t = config.get('terminal', {})
    if t.get('backend') != 'ssh' or t.get('ssh_sync_files') is not False:
        raise PermissionError('No-copy remote device mapping required')
    host, user, key = (t.get(k, '') for k in ('ssh_host', 'ssh_user', 'ssh_key'))
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}', host) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]{0,63}', user):
        raise PermissionError('Invalid remote device mapping')
    keypath = Path(key)
    if not keypath.is_absolute() or keypath.parent != KEY_ROOT:
        raise PermissionError('Operator-managed SSH key required')
    protected_file(keypath)
    port = t.get('ssh_port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise PermissionError('Invalid SSH port')
    return (owner, host, user, str(keypath), port, hashlib.sha256(raw).hexdigest())


def enabled():
    try:
        _binding()
        return True
    except (PermissionError, OSError, ValueError, TypeError, Denied, yaml.YAMLError):
        return False


def _execute(binding, command, timeout):
    _, host, user, key, port, _ = binding
    # Ignore user SSH config, forwarding, ambient keys and provider env. Keep
    # strict existing known-host verification; an unknown key requires setup.
    argv = ['/usr/bin/ssh', '-F', '/dev/null', '-i', key, '-p', str(port),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=6',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
            f'{user}@{host}', 'bash -c ' + shlex.quote(command)]
    # Bounded returned output; private temp handles never enter shared artifacts.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out,
                stderr=err, timeout=timeout, env={'PATH':'/usr/bin:/bin',
                'HOME':'/Users/Chris', 'LANG':'en_US.UTF-8'})
        except subprocess.TimeoutExpired:
            return {'status':'timeout', 'execution':'unknown',
                'message':'The computer did not finish in time. Check the result before retrying; the command may still be running remotely.'}
        out.seek(0);err.seek(0)
        stdout = out.read(32001);stderr = err.read(8001)
        if result.returncode == 255:
            detail = stderr.decode('utf-8','replace').lower()
            reason = ('host_key' if 'host key' in detail else 'authentication' if 'permission denied' in detail or 'load key' in detail else 'network' if any(x in detail for x in ('timed out','no route','network is unreachable','connection refused','could not resolve')) else 'ssh_failed')
            return {'status':'unavailable', 'reason':reason, 'message':'Your mapped computer is offline or its remote login is unavailable. The mapping is retained; try again when it is reachable. No local fallback was used.'}
        return {'status':'completed', 'exit_code':result.returncode,
            'stdout':stdout[:32000].decode('utf-8','replace'),
            'stderr':stderr[:8000].decode('utf-8','replace'),
            'truncated':len(stdout)>32000 or len(stderr)>8000}


def invoke(args, **kwargs):
    return invoke_bound(args, _binding)


def invoke_bound(args, resolve_binding, execute=None):
    if type(args) is not dict or set(args) - {'command','timeout'}:
        raise ValueError('Only command and timeout are supported')
    command = args.get('command');timeout = args.get('timeout',30)
    if type(command) is not str or not 1 <= len(command) <= 12000 or '\0' in command:
        raise ValueError('A bounded command is required')
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError('Timeout must be 1 to 60 seconds')
    binding = resolve_binding()
    result = (execute or _execute)(binding, command, timeout)
    if resolve_binding() != binding:
        raise PermissionError('Device authority changed; result withheld')
    return json.dumps(result)


def register():
    if not enabled():
        return
    from tools.registry import registry
    registry.register(name=TOOL, toolset='josh_status_pilot', handler=invoke,
        schema={'name':TOOL,
        'description':'Run a shell command on your own mapped Mac. Use this to inspect files or operate installed command-line apps on your computer. This is remote command access, not a screen-control driver. Offline devices return unavailable; never substitute the backend computer. Do not automatically retry a timed-out action.',
        'parameters':{'type':'object','properties':{'command':{'type':'string','maxLength':12000},'timeout':{'type':'integer','minimum':1,'maximum':60}},'required':['command'],'additionalProperties':False}})


# ---- Owner-pinned Google (Gmail + Calendar) for the authenticated owner ----
GOOGLE_TOOL = 'owner_google'
GOOGLE_ROOT = Path('/Users/Chris/.hermes/agent-os/owner-google-mcp')


def _google_launcher():
    from gateway.josh_board_authority import _execution, restricted
    scope = _execution.get()
    if not scope or scope[0] != scope[1] or not restricted():
        raise PermissionError('Authenticated owner required')
    owner = scope[0]
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', owner):
        raise PermissionError('Invalid owner')
    path = GOOGLE_ROOT / f'run-{owner}.sh'
    if not path.is_file():
        raise PermissionError('Google is not connected for this owner')
    return str(path)


def google_enabled():
    try:
        _google_launcher()
        return True
    except (PermissionError, OSError, ValueError):
        return False


def google_invoke(args, **kwargs):
    if type(args) is not dict or set(args) - {'action', 'arguments'}:
        raise ValueError('Only action and arguments are supported')
    action = args.get('action') or 'google_account_status'
    params = args.get('arguments') or {}
    if type(action) is not str or not re.fullmatch(r'[a-z_]{3,60}', action) or type(params) is not dict:
        raise ValueError('Invalid Google action')
    launcher = _google_launcher()
    import select, time
    p = subprocess.Popen([launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    def send(o):
        p.stdin.write(json.dumps(o) + '\n'); p.stdin.flush()
    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'owner_google', 'version': '1'}}})
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        method, rp = ('tools/list', {}) if action == 'list_actions' else ('tools/call', {'name': action, 'arguments': params})
        send({'jsonrpc': '2.0', 'id': 2, 'method': method, 'params': rp})
        end = time.time() + 60
        while time.time() < end:
            r, _, _ = select.select([p.stdout], [], [], 1)
            if not r:
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get('id') != 2:
                continue
            if action == 'list_actions':
                return json.dumps({t['name']: t.get('inputSchema', {}).get('properties', {}) for t in msg.get('result', {}).get('tools', [])})
            if 'result' in msg:
                return '\n'.join(c.get('text', '') for c in msg['result'].get('content', []))
            return json.dumps(msg.get('error') or msg)
        return json.dumps({'error': 'Google did not answer in time'})
    finally:
        p.kill()


def register_google():
    if not google_enabled():
        return
    from tools.registry import registry
    registry.register(name=GOOGLE_TOOL, toolset='josh_status_pilot', handler=google_invoke,
        schema={'name': GOOGLE_TOOL,
        'description': "Use the owner's own connected Google account (Gmail and Google Calendar). action is one of: google_account_status, gmail_search, gmail_read_message, gmail_create_draft, gmail_send, calendar_list_calendars, calendar_list_events, calendar_create_event, calendar_update_event, calendar_delete_event, or list_actions to see every action and its arguments. arguments is the JSON object for that action, e.g. calendar_list_events {\"timeMin\":\"2026-10-22T00:00:00-04:00\",\"timeMax\":\"2026-10-23T00:00:00-04:00\"}. Always call this before saying anything about the owner's email or calendar; never claim Google is not connected without calling google_account_status. Confirm with the owner before the first send or delete.",
        'parameters': {'type': 'object', 'properties': {'action': {'type': 'string'}, 'arguments': {'type': 'object'}}, 'required': ['action'], 'additionalProperties': False}})

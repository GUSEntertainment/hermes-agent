"""Own-device commands for the existing legacy Photon DM route.

Authority comes from current captured ingress and canonical enrollment, never
from model arguments or a caller-selected profile. Named Josh runs retain their
existing tool path and do not use this adapter.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import yaml
from pathlib import Path
from tools.registry import registry, no_cache_check_fn
from gateway.owner_device_tools import profile_binding, invoke_bound
from plugins.platforms.photon.secure_intake import composition
from plugins.platforms.photon.secure_intake.gateway_binding import current_identity
from plugins.platforms.photon.secure_intake.request_registry import encoded, identity_valid
from plugins.platforms.photon.secure_intake.trusted_intake import _CURRENT
from plugins.platforms.photon.secure_intake.errors import Denied
from plugins.platforms.photon.secure_intake.enrollment_guard import protected_file


def binding():
    state = composition.start()
    if state is None:
        raise Denied('Verified Photon connection required')
    identity = current_identity()
    identity_valid(identity)
    witness = _CURRENT.get()
    if not witness or witness[0] != encoded(identity):
        raise Denied('Current direct Photon request required')
    publisher = state[1]
    profile = publisher.profile(identity)
    if profile['owner'] != 'connor':
        raise Denied('Device not enabled for this owner')
    with publisher.registry._db() as db:
        source = db.execute('SELECT * FROM intake_sources WHERE platform=? AND message=?',
            (identity['platform'], identity['message'])).fetchone()
        if (not source or source['identity'] != encoded(identity)
            or source['digest'] != witness[1] or source['revoked']
            or source['expires'] <= publisher.clock()):
            raise Denied('Current authenticated source required')
    mapped = profile_binding(profile['owner'])
    if current_identity() != identity or publisher.profile(identity) != profile:
        raise Denied('Device authority changed')
    # Revalidation also binds the result to this message and enrollment generation.
    digest = hashlib.sha256(encoded([mapped[-1], identity, profile['generation'], witness[1]]).encode()).hexdigest()
    return (*mapped[:-1], digest)


def handle(args, **kwargs):
    return invoke_bound(args, binding, execute)


def fallback_allowed():
    # Explicit operator grant only. No caller-controlled target or ambient flag.
    from gateway.owner_device_tools import PROFILE_ROOT
    file = PROFILE_ROOT / 'connor' / 'config.yaml'
    before = protected_file(file)
    raw = file.read_bytes()
    after = protected_file(file)
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise Denied('Device grant changed')
    return yaml.safe_load(raw).get('owner_device_fallback') == 'chris-local'


def execute(mapped, command, timeout):
    from gateway.owner_device_tools import _execute
    allowed = fallback_allowed()
    if not allowed:
        return dict(_execute(mapped, command, timeout), execution_device='connor-mac', fallback=False)
    # Check reachability BEFORE sending the requested command. Never replay a
    # timed-out user command, transport failure after dispatch, or exit code.
    probe = _execute(mapped, 'true', 8)
    if probe.get('status') == 'completed' and probe.get('exit_code') == 0:
        if binding() != mapped:raise Denied('Device authority changed')
        return dict(_execute(mapped, command, timeout), execution_device='connor-mac', fallback=False)
    if probe.get('status') != 'unavailable' or probe.get('reason') != 'network':
        return dict(probe, execution_device='connor-mac', fallback=False, requested_command_started=False)
    if not fallback_allowed() or binding() != mapped:
        raise Denied('Fallback grant changed')
    # Chris explicitly authorized Connor to use this host when his Mac is
    # unreachable. Do not inherit the gateway's model/provider credentials.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(['/bin/bash','--noprofile','--norc','-c',command],
                cwd='/Users/Chris', env={'HOME':'/Users/Chris','PATH':'/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'},
                stdin=subprocess.DEVNULL,stdout=out,stderr=err,timeout=timeout)
        except subprocess.TimeoutExpired:
            return {'status':'timeout','execution':'unknown','execution_device':'chris-mac','fallback':True,
                'message':'The command timed out on Chris’s authorized fallback Mac. Do not repeat it without checking its outcome.'}
        out.seek(0);err.seek(0);stdout=out.read(32001);stderr=err.read(8001)
        return {'status':'completed','exit_code':result.returncode,'execution_device':'chris-mac','fallback':True,
            'message':'Connor’s Mac was unreachable; this ran on Chris’s authorized fallback Mac. Files and open apps belong to Chris’s Mac.',
            'stdout':stdout[:32000].decode('utf-8','replace'),'stderr':stderr[:8000].decode('utf-8','replace'),
            'truncated':len(stdout)>32000 or len(stderr)>8000}


@no_cache_check_fn
def available():
    try:
        binding()
        return True
    except (Denied, PermissionError, OSError, ValueError, TypeError):
        return False


registry.register(name='photon_computer', toolset='kanban', handler=handle,
    check_fn=available,
    schema={'name':'photon_computer',
        'description':'Run a command for the authenticated Connor Photon conversation. The server chooses Connor’s Mac, or Chris’s explicitly authorized fallback Mac only when Connor’s is unreachable before execution. Tell the user which execution_device was used; do not assume the same files or apps exist on both. Never automatically retry a timed-out action. Use installed tools for device operations; SSH success does not prove screen access.',
        'parameters':{'type':'object','properties':{'command':{'type':'string','maxLength':12000},'timeout':{'type':'integer','minimum':1,'maximum':60}},'required':['command'],'additionalProperties':False}})

#!/usr/bin/env python3
"""Guarded Personal-only activation. Dry-run by default; never runs a model.

Run using the installed Hermes Python. --apply activates the reviewed package;
--rollback RECEIPT restores exact operator backups. Only the Personal launchd
label is stopped/started. Main gateway/phone services are never controlled.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import tempfile
import time
import urllib.request

import yaml

HOME = Path('/Users/Chris')
ROOT = HOME / '.hermes'
LABEL = 'com.gus.hermes.profile.app-owner-chris-margot.gateway'
PORT = 56711
PACKAGE_ROOT = Path(__file__).resolve().parent.parent / 'releases'
DEFAULT_PACKAGE = PACKAGE_ROOT / '20261004-personal-board-control'
BASE_CONTROLLER = ROOT / 'releases/hermes-agent-20261004-member-board-controller'
WRAPPER_SHA = '581bd37d50ded8f685b8cd422c427a91600a545c2925dde221923ea8eadcb170'
EXPECTED_TOOLS = {'own_work_list', 'own_work_show', 'own_work_comment', 'own_work_manage'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def targets():
    profile = ROOT / 'profiles/app-owner-chris-margot'
    return {'configuration': profile/'config.yaml', 'soul': profile/'SOUL.md',
            'launchagent': HOME/'Library/LaunchAgents'/f'{LABEL}.plist',
            'wrapper': ROOT/'scripts/hermes-kanban-guarded'}


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022 or before.st_size > 2000000:
            raise PermissionError('Untrusted operator file')
        raw = os.read(fd, 2000001)
        if os.fstat(fd).st_mtime_ns != before.st_mtime_ns:
            raise RuntimeError('Operator file changed')
        return raw, stat.S_IMODE(before.st_mode)
    finally:
        os.close(fd)


def atomic(path, raw, mode):
    fd, temporary = tempfile.mkstemp(prefix='.personal-stage-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def prepare(package):
    package = Path(package)
    if package.parent.resolve() != PACKAGE_ROOT.resolve() or package.resolve() != package.absolute():
        raise PermissionError('Use an installed reviewed release package')
    report = json.loads(read(package/'verification.json')[0])
    manifest = json.loads(read(package/'manifest.json')[0])
    for lane in ('gateway','controller'):
        candidate = Path(report[lane])
        if candidate.parent != ROOT/'releases' or candidate.resolve() != candidate or not candidate.is_dir():
            raise PermissionError('Canonical installed candidate required')
        if manifest['lanes'][lane]['candidate'] != str(candidate):
            raise ValueError('Package candidate mismatch')
        for record in manifest['lanes'][lane]['files']:
            name = Path(record['path'])
            if name.is_absolute() or '..' in name.parts or digest(read(candidate/name)[0]) != record['after_sha256']:
                raise ValueError('Candidate differs from reviewed manifest')
    selected = targets()
    original = {kind: read(path) for kind,path in selected.items()}
    for kind in ('configuration','soul','launchagent'):
        if report[kind]['path'] != str(selected[kind]) or digest(original[kind][0]) != report[kind]['expected_before_sha256']:
            raise ValueError('Operator file differs from reviewed input')
    if digest(original['wrapper'][0]) != WRAPPER_SHA:
        raise ValueError('Controller wrapper changed; review before activation')
    config = yaml.safe_load(original['configuration'][0])
    old_extra = config['platforms']['api_server']['extra']
    if old_extra.get('port') != PORT or old_extra.get('host') != '127.0.0.1':
        raise ValueError('Personal listener moved')
    config['toolsets'] = ['owner_context','owner_board_control']
    config['platform_toolsets']['api_server'] = ['owner_context','owner_board_control']
    old_extra['cockpit_principal'] = 'chris'
    config['app_private_board_control'] = dict(enabled=True,principal='chris',board='chris-home',scope_contract='margot-owner-private-v1')
    soul = original['soul'][0].decode()
    if soul.count(report['soul']['old_sentence']) != 1:
        raise ValueError('Personal guidance changed')
    soul = soul.replace(report['soul']['old_sentence'], report['soul']['new_sentence'])
    soul += '\n\n' + report['soul']['append'] + '\n'
    launch = plistlib.loads(original['launchagent'][0])
    if launch.get('Label') != LABEL:
        raise ValueError('Wrong service label')
    args = launch['ProgramArguments']
    paths = [i for i,a in enumerate(args) if a.startswith('PYTHONPATH=')]
    interpreters = [i for i,a in enumerate(args) if a.endswith('/.venv/bin/python')]
    if len(paths) != 1 or len(interpreters) != 1:
        raise ValueError('Unexpected Personal launcher')
    interpreter = Path(report['runtime_python'])
    if not interpreter.is_file() or not interpreter.is_relative_to(ROOT/'releases'):
        raise ValueError('Installed interpreter required')
    args[paths[0]] = 'PYTHONPATH=' + report['gateway']
    args[interpreters[0]] = str(interpreter)
    launch['WorkingDirectory'] = report['gateway']
    old_rel = 'REL=' + str(BASE_CONTROLLER)
    wrapper = original['wrapper'][0].decode()
    if wrapper.count(old_rel+'\n') != 1:
        raise ValueError('Unexpected controller selection')
    wrapper = wrapper.replace(old_rel+'\n','REL='+report['controller']+'\n')
    prepared = {'configuration':yaml.safe_dump(config,sort_keys=False).encode(),
                'soul':soul.encode(), 'launchagent':plistlib.dumps(launch), 'wrapper':wrapper.encode()}
    return report, selected, original, prepared


def remaining(deadline):
    seconds = deadline-time.monotonic()
    if seconds <= 0: raise TimeoutError('Personal startup deadline reached')
    return seconds


def ctl(*args, deadline):
    # The label is fixed; never dispatch main ai.hermes.gateway commands.
    return subprocess.run(['/bin/launchctl',*args], capture_output=True,
                          timeout=min(10,remaining(deadline)), check=False)


def stopped(deadline):
    domain = f'gui/{os.getuid()}/{LABEL}'
    ctl('bootout',domain,deadline=deadline)
    while True:
        registered = ctl('print',domain,deadline=deadline).returncode == 0
        with socket.socket() as probe:
            try: probe.bind(('127.0.0.1',PORT)); free = True
            except OSError: free = False
        if not registered and free: return
        time.sleep(min(1,remaining(deadline)))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def response(path, key, deadline):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}'+path,
                                 headers={'Authorization':'Bearer '+key})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=min(3,remaining(deadline))) as res:
        raw = res.read(1048577)
        if res.status != 200 or len(raw)>1048576: raise ValueError('Unexpected health response')
        return json.loads(raw)


def credential():
    from dotenv import dotenv_values
    file = ROOT/'profiles/app-owner-chris-margot/.env'
    _, mode = read(file)
    if mode & 0o077: raise PermissionError('Private profile credential required')
    key = dotenv_values(file).get('API_SERVER_KEY','')
    if not isinstance(key,str) or len(key)<16: raise PermissionError('Profile credential missing')
    return key


def restart(launch_path, key, *, expect_controls, deadline=None):
    deadline = time.monotonic()+90 if deadline is None else deadline
    stopped(deadline)
    boot = ctl('bootstrap',f'gui/{os.getuid()}',str(launch_path),deadline=deadline)
    if boot.returncode: raise RuntimeError('Personal bootstrap failed')
    while True:
        try:
            response('/health',key,deadline)
            cap = response('/v1/capabilities',key,deadline)
            if not expect_controls: return
            names = cap.get('features',{}).get('owner_board_control',{}).get('tools',[])
            if len(names)==4 and set(names)==EXPECTED_TOOLS: return
        except Exception:
            pass
        time.sleep(min(1,remaining(deadline)))


def restore(receipt, *, restart_service=True):
    record = json.loads(read(receipt)[0])
    selected = targets()
    if set(record['files']) != set(selected): raise ValueError('Unexpected backup receipt')
    items = {}
    for kind,path in selected.items():
        entry = record['files'][kind]
        if entry['path'] != str(path): raise ValueError('Backup target mismatch')
        backup = Path(receipt).parent/(kind+'.before')
        raw,_ = read(backup)
        if digest(raw)!=entry['before_sha256'] or digest(read(path)[0]) not in (entry['before_sha256'],entry['after_sha256']):
            raise ValueError('Backup or current file changed; refusing to overwrite it')
        items[kind] = (path,raw,entry['mode'])
    # Stop the new private listener before withdrawing its controller route.
    deadline = time.monotonic()+90
    if restart_service: stopped(deadline)
    for kind in ('configuration','soul','launchagent','wrapper'):
        atomic(*items[kind])
    if restart_service: restart(selected['launchagent'],credential(),expect_controls=False,deadline=deadline)
    record['status']='rolled_back';atomic(Path(receipt),json.dumps(record,indent=2).encode()+b'\n',0o600)


def apply(package):
    report,selected,original,prepared = prepare(package)
    key = credential()
    parent = ROOT/'private/board-owner-control/personal-activations'
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if parent.resolve()!=parent or parent.stat().st_mode&0o077: raise PermissionError('Private backup directory required')
    folder = Path(tempfile.mkdtemp(prefix='activation-',dir=parent))
    files = {}
    for kind,path in selected.items():
        raw,mode = original[kind]
        atomic(folder/(kind+'.before'),raw,0o600)
        files[kind]=dict(path=str(path),mode=mode,before_sha256=digest(raw),after_sha256=digest(prepared[kind]))
    receipt = folder/'receipt.json'
    record = dict(status='prepared',package=str(package),files=files)
    atomic(receipt,json.dumps(record,indent=2).encode()+b'\n',0o600)
    print(json.dumps({'status':'prepared','receipt':str(receipt)}),flush=True)
    touched = False
    try:
        for kind in ('wrapper','configuration','soul','launchagent'):
            if read(selected[kind])[0] != original[kind][0]: raise ValueError('Operator file changed before activation')
            atomic(selected[kind],prepared[kind],original[kind][1])
        touched = True
        print(json.dumps({'status':'reloading_personal_only'}),flush=True)
        restart(selected['launchagent'],key,expect_controls=True)
    except Exception as exc:
        print(json.dumps({'status':'restoring_previous_personal','reason_type':type(exc).__name__}),flush=True)
        restore(receipt,restart_service=touched)
        raise RuntimeError('Personal activation failed; prior files restored') from None
    record['status']='active';atomic(receipt,json.dumps(record,indent=2).encode()+b'\n',0o600)
    print(json.dumps({'status':'active','service':LABEL,'port':PORT,'authenticated_board_tools':sorted(EXPECTED_TOOLS),'receipt':str(receipt)}))


@contextmanager
def activation_lock():
    path = ROOT/'private/board-owner-control/personal-activation.lock'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd,str(os.getpid()).encode());os.close(fd)
        yield
    finally:
        path.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,default=DEFAULT_PACKAGE)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    if args.rollback:
        if not args.rollback.resolve().is_relative_to(ROOT/'private/board-owner-control/personal-activations'):
            raise PermissionError('Protected activation receipt required')
        with activation_lock(): restore(args.rollback)
        print(json.dumps({'status':'rolled_back','service':LABEL}));return
    if args.apply:
        with activation_lock(): apply(args.package)
    else:
        report,_,_,_=prepare(args.package)
        print(json.dumps({'status':'ready_not_applied','service':LABEL,'gateway':report['gateway'],'controller':report['controller'],'port':PORT}))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}))
        raise SystemExit(1)

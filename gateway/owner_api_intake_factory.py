"""Protected server composition using existing intake config and native services."""
import os,json
from pathlib import Path
from plugins.platforms.photon.secure_intake.paths import private_file
from plugins.platforms.photon.secure_intake.composition import load_config
from plugins.platforms.photon.secure_intake.canonical_enrollment import canonical_enrollment,canonical_transport_enrollment
from plugins.platforms.photon.secure_intake.request_registry import RequestRegistry
from plugins.platforms.photon.secure_intake.trusted_intake import Publisher
from plugins.platforms.photon.secure_intake.node_service import NodeService
from agent.delegation_context import is_delegated_child_context
from plugins.platforms.photon.josh_onboarding.api_intake import ApiIntake,ApiServerVerifier

def _read(path):
    path=Path(path);before=private_file(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        opened=os.fstat(fd);raw=os.read(fd,8193);after=os.fstat(fd)
    finally:os.close(fd)
    current=private_file(path)
    sig=lambda s:(s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(raw)>8192 or not sig(before)==sig(opened)==sig(after)==sig(current):raise PermissionError('Intake config changed')
    return raw

def build(profile,port):
    # profile and port are actual server-selected values, not model arguments.
    if type(profile)!=str or not profile or type(port)!=int or not 1<=port<=65535:
        raise PermissionError('Native named listener required')
    path=os.environ.get('HERMES_PHOTON_INTAKE_CONFIG')
    if not path:raise PermissionError('Owner intake is not configured')
    original=_read(path);c=load_config(path)
    if _read(path)!=original:raise PermissionError('Intake config changed')
    def current(identity,*,transport=False):
        if _read(path)!=original:raise PermissionError('Intake config changed')
        value=(canonical_transport_enrollment if transport else canonical_enrollment)(identity)
        if value['owner']!=profile:raise PermissionError('Owner intake mismatch')
        if _read(path)!=original:raise PermissionError('Intake config changed')
        return value
    endpoint=f'http://127.0.0.1:{port}/p/{profile}/v1/runs'
    def enrollment(transport):
        identity=dict(zip(('platform','chat_type','conversation','sender','message'),transport))
        value=current(identity,transport=True)
        return {k:value[k] for k in ('owner','generation','enabled','revoked')}|{'profile':value['owner'],'endpoint':endpoint}
    registry=RequestRegistry(c['registry_file'])
    publisher=Publisher(registry,enrollment=current,delegated=is_delegated_child_context)
    service=NodeService(registry_file=c['registry_file'],form_directory=c['form_directory'],node=c['node'],origin=c['origin'],secure_core=c['secure_core'])
    verifier=ApiServerVerifier()
    intake=ApiIntake(registry,enrollment=enrollment,authenticated_state=verifier,publisher=publisher,service=service)
    if _read(path)!=original:raise PermissionError('Intake config changed')
    return intake,verifier,endpoint

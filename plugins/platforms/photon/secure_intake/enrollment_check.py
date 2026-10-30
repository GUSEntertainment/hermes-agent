"""Fixed local read-only checker. Invoked only by configured form server.

stdin contains original six-field identity from the locked registry, not owner
claims. All failures output false, without tracebacks or credential material.
"""
import json
import os
import pathlib
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[4]))
from plugins.platforms.photon.secure_intake.canonical_enrollment import canonical_enrollment,PINNED
from plugins.platforms.photon.secure_intake.enrollment_guard import EnrollmentGuard
if __name__=='__main__':
    result={'allowed':False,'revoked':False}
    try:
        sys.path.insert(0,'/Users/Chris/.hermes/releases/hermes-agent-20260906-kanban-lifecycle-v7')
        sys.path.insert(0,str(PINNED))
        data=sys.stdin.read(4097)
        if len(data)>4096:raise ValueError()
        result=EnrollmentGuard(os.environ['SECURE_PHOTON_REGISTRY_FILE'],enrollment=canonical_enrollment).state(json.loads(data))
    except Exception:pass
    print(json.dumps(result))

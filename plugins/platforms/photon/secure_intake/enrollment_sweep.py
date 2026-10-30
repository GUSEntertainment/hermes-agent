"""Operator startup reconciliation, not a tool, daemon or provider action."""
import os
import sys
import pathlib
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[4]))
from plugins.platforms.photon.secure_intake.canonical_enrollment import canonical_enrollment,PINNED
from plugins.platforms.photon.secure_intake.enrollment_guard import EnrollmentGuard
if __name__=='__main__':
    try:
        sys.path.insert(0,'/Users/Chris/.hermes/releases/hermes-agent-20260906-kanban-lifecycle-v7')
        sys.path.insert(0,str(PINNED))
        result=EnrollmentGuard(os.environ['SECURE_PHOTON_REGISTRY_FILE'],enrollment=canonical_enrollment).sweep()
        print('Revoked reserved forms:',result['revoked'])
    except Exception:
        print('Enrollment reconciliation unavailable',file=sys.stderr);sys.exit(1)

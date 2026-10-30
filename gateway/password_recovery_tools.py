"""Issue private app reset links only for the current verified Photon owner."""
import json,subprocess
from pathlib import Path
TOOL='margot_password_reset'
SCRIPT=Path('/Users/Chris/projects/gus-agent-os-stable/scripts/password-recovery/recovery.py')
PYTHON='/Users/Chris/.hermes/hermes-agent/.venv/bin/python'

def identity():
    from agent.delegation_context import is_delegated_child_context
    if is_delegated_child_context():raise PermissionError('Direct owner request required')
    from gateway.owner_api_intake import _ACTIVE
    native=_ACTIVE.get()
    if native is not None:
        from plugins.platforms.photon.josh_onboarding.api_intake import _ACTIVE as source
        active=source.get()
        if not active:raise PermissionError('Current owner request required')
        claim=native.receipts.claim(active[0])
        i=claim['identity'];p=native.publisher.profile(i)
        return i,p
    from plugins.platforms.photon.secure_intake import composition
    from plugins.platforms.photon.secure_intake.gateway_binding import current_identity
    from plugins.platforms.photon.secure_intake.request_registry import encoded,identity_valid
    from plugins.platforms.photon.secure_intake.trusted_intake import _CURRENT
    state=composition.start()
    if state is None:raise PermissionError('Verified Photon connection required')
    i=current_identity();identity_valid(i);witness=_CURRENT.get()
    if not witness or witness[0]!=encoded(i):raise PermissionError('Current direct source required')
    publisher=state[1];p=publisher.profile(i)
    with publisher.registry._db() as db:
        row=db.execute('SELECT * FROM intake_sources WHERE platform=? AND message=?',(i['platform'],i['message'])).fetchone()
        if not row or row['identity']!=encoded(i) or row['digest']!=witness[1] or row['revoked'] or row['expires']<=publisher.clock():raise PermissionError('Current authenticated source required')
    return i,p

def invoke(args,**kwargs):
    try:
        if args not in ({},None):raise PermissionError('No account parameters accepted')
        before=identity()
        if not SCRIPT.is_file():raise PermissionError('Recovery unavailable')
        r=subprocess.run([PYTHON,'-I',str(SCRIPT),'issue'],input=json.dumps({'identity':before[0]}),text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30)
        if r.returncode or len(r.stdout)>4096 or identity()!=before:raise PermissionError('Recovery unavailable')
        result=json.loads(r.stdout)
        if not result.get('url','').startswith('https://margot.gusentertainment.com/login/reset#'):raise PermissionError('Invalid recovery result')
        return json.dumps(dict(result,instructions='Send this private link only in this conversation. The person chooses their password on the form. Never request or repeat their password in chat.'))
    except Exception:return json.dumps({'state':'unavailable','text':'I couldn’t create a verified password-reset link. Your password has not been changed.'})

SCHEMA={'name':TOOL,'description':'When the person explicitly asks for a forgotten-password/reset link for the Margot app, create a private 15-minute one-use reset form for their own verified account. No username or password arguments. Does not change the password until they submit the form.','parameters':{'type':'object','properties':{},'additionalProperties':False}}
def register(toolset='kanban'):
    from tools.registry import registry
    name = TOOL if toolset == 'kanban' else 'owner_password_reset'
    registry.register(name=name,toolset=toolset,schema=dict(SCHEMA,name=name),handler=invoke)

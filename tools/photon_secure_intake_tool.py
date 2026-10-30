import json
from tools.registry import registry
from plugins.platforms.photon.secure_intake.trusted_intake import SCHEMA
from plugins.platforms.photon.secure_intake import composition

def handle(args,**kwargs):return json.dumps(composition.invoke(args),ensure_ascii=False)
registry.register(name='photon_secure_intake',toolset='kanban',schema=SCHEMA,handler=handle,check_fn=composition.available,emoji='🔒')

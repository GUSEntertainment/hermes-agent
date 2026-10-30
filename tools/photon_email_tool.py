"""Fixed immutable owner-email adapter; no model supplied identity or paths."""
import importlib.util
import json
from pathlib import Path
from tools.registry import registry

ADAPTER = Path('/Users/Chris/.hermes/phone-runtimes/live-20260922-v28-owner-email/photon_email_gateway_adapter.py')

def _handle(args, **kwargs):
    try:
        spec = importlib.util.spec_from_file_location('margot_owner_email_gateway_v28', ADAPTER)
        if spec is None or spec.loader is None:
            raise RuntimeError('Email adapter unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return json.dumps(module.invoke(args), ensure_ascii=True)
    except Exception:
        return json.dumps({'status':'unavailable','message':'I could not verify access to your live email.'})

registry.register(
    name='photon_email_read', toolset='kanban', handler=_handle,
    check_fn=lambda: ADAPTER.is_file(),
    schema={'name':'photon_email_read',
            'description':'Read your own live email using the current private Photon conversation. Never send or modify email. A tool result is required before claiming a fresh inbox check.',
            'parameters':{'type':'object','properties':{'query':{'type':'string','maxLength':500},'limit':{'type':'integer','minimum':1,'maximum':20}},'additionalProperties':False}},
)

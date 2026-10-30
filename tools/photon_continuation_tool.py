"""Narrow Photon document continuation; no model-supplied authority or routes."""
from tools.registry import registry


def _handle(args, **kwargs):
    from plugins.platforms.photon.continuation import submit_current
    return submit_current(args)


def _available():
    from pathlib import Path
    return Path('/Users/Chris/.hermes/phone-runtimes/live-20260919-v21-text-continuation/processors/photon_text_continuation.py').is_file()


registry.register(
    name='photon_continue_document', toolset='kanban',
    schema={'name': 'photon_continue_document',
            'description': 'Submit the current private Photon reply as a revision of its verified delivered document. Use when the user explicitly requests an edit to a previously delivered phone-work document. Takes no arguments: the server binds the exact current message, original artifact, caller, owner, board and return thread. Do not use for thanks, questions or status lookups. Call before promising work has started; only accepted=true confirms durable worker acceptance. Do not substitute delegate_task or generic kanban_create for this route.',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    handler=_handle, check_fn=_available, emoji='📄',
)

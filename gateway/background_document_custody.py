"""Original inbound-event custody for /bg; no owner registry or replay authority.

Foreground custody remains the only storage, recipient resolver and sender.
This adapter retains a frozen enqueue-time observation across the async task.
"""
import asyncio
import dataclasses
import json


@dataclasses.dataclass(frozen=True)
class BackgroundOrigin:
    event: object
    binding_json: str
    authority_json: str
    source_profile: object

    @property
    def source(self):
        return self.event.source


def supported(source):
    return str(getattr(source.platform, "value", source.platform)) in ("telegram", "photon")


async def prepare_origin(runner, event):
    if getattr(event, "internal", False) or not runner._is_user_authorized_for_source(event.source):
        raise PermissionError("Background command requires an authorized user event")
    if not supported(event.source):
        return None  # Existing non-Desk transport behavior, no guessed owner.
    from gateway.session import build_session_context
    from gateway import direct_document_custody as custody
    from gateway.gateway_document_custody import current

    # Detach mutable transport fields so later adapter reuse cannot redirect a
    # background reply. This is still a genuine event bound by this runner.
    source = dataclasses.replace(event.source, message_id=str(event.message_id or ""))
    original = dataclasses.replace(event, source=source, media_urls=list(event.media_urls or []), media_types=list(event.media_types or []))
    if not source.message_id or getattr(source, "profile_route_rejected", False):
        raise PermissionError("Background command has no valid original message")
    entry = await runner.async_session_store.get_or_create_session(source)
    if not runner._is_user_authorized_for_source(source):
        raise PermissionError("Background source authorization revoked")
    tokens = runner._set_session_env(build_session_context(source, runner.config, entry))
    try:
        custody.bind_event(original)
        binding_json = _bound_binding(original, source)
    finally:
        runner._clear_session_env(tokens)
    authority = await asyncio.to_thread(current, json.loads(binding_json))
    proof = BackgroundOrigin(original, binding_json, custody.encode(authority), source.profile)
    require_origin(proof, source)
    if not runner._is_user_authorized_for_source(source):
        raise PermissionError("Background source authorization revoked")
    return proof


def _bound_binding(event, source, metadata=None):
    from gateway import direct_document_custody as custody
    # Reuse the foreground registry's genuine Python object identity, not an
    # event attribute, serialized marker, environment value or model argument.
    token = custody._events.get(id(event))
    if token is None or token[0]() is not event or event.source is not source or getattr(event, "internal", False):
        raise PermissionError("Original background document event unavailable")
    binding = json.loads(token[1])
    custody._assert_destination(event, binding, metadata)
    if (custody.sha(event.text.encode()) != binding["request_sha256"]
            or event.timestamp.timestamp() != binding["received_at"]
            or (source.chat_type or "dm") != binding["target"]["chat_type"]
            or (source.profile is not None and source.profile != binding["profile"])):
        raise PermissionError("Original background document context changed")
    return token[1]


def require_origin(proof, source, metadata=None):
    if (type(proof) is not BackgroundOrigin
            or source.profile != proof.source_profile
            or _bound_binding(proof.event, source, metadata) != proof.binding_json):
        raise PermissionError("Original background document proof unavailable")


async def validate_delivery(runner, proof, source, metadata=None):
    """Fresh recipient/destination fence before a protected background send."""
    if proof is None and not supported(source):
        return
    from gateway import direct_document_custody as custody
    from gateway.gateway_document_custody import current
    require_origin(proof, source, metadata)
    if not runner._is_user_authorized_for_source(source):
        raise PermissionError("Background source authorization revoked")
    authority = await asyncio.to_thread(current, json.loads(proof.binding_json))
    # A configured recipient becoming absent is revocation, not permission to
    # use the unconfigured raw-send fallback. Re-grants or owner changes also
    # cannot reuse this run. None->None preserves genuinely unknown surfaces.
    if custody.encode(authority) != proof.authority_json:
        raise PermissionError("Background recipient authority changed")
    require_origin(proof, source, metadata)
    if not runner._is_user_authorized_for_source(source):
        raise PermissionError("Background source authorization revoked")

"""Import fixed public SDK code before serving API traffic; never build clients."""
import asyncio
import logging
logger = logging.getLogger('hermes.api_startup_prewarm')


def _load_public_dependencies():
    # No config, profile selection, auth, network request, or client construction.
    import httpx  # noqa: F401
    from agent.process_bootstrap import _load_openai_cls
    _load_openai_cls()  # existing class cache only; no instance


async def prewarm_api_dependencies():
    try:
        await asyncio.to_thread(_load_public_dependencies)
    except Exception:
        # Preserve prior startup availability if an optional SDK is unavailable.
        # Its original per-turn import/error behavior remains authoritative.
        logger.warning('Public API dependency prewarm unavailable')

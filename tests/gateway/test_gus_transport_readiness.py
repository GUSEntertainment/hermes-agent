import asyncio,json,time
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from gateway.config import Platform,PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter,_POLLING_STALL_TIMEOUT
from tests.gateway.test_telegram_conflict import _build_polling_app,_cancel_heartbeat

@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr('plugins.platforms.telegram.adapter.discover_fallback_ips',AsyncMock(return_value=[]))
    monkeypatch.setattr('plugins.platforms.telegram.adapter.HTTPXRequest',lambda **kw:MagicMock())
    return TelegramAdapter(PlatformConfig(enabled=True,token='test-only'))

@pytest.mark.asyncio
async def test_restart_preserves_pending_queue(monkeypatch,adapter):
    captured=_build_polling_app(monkeypatch,adapter)
    try:
        assert await adapter.connect()
        assert captured['drop_pending_updates'] is False
        assert adapter.transport_health()['status']=='ok'
    finally:await _cancel_heartbeat(adapter)

@pytest.mark.asyncio
async def test_failed_polling_cannot_mark_reconnect_ready(monkeypatch,adapter):
    _build_polling_app(monkeypatch,adapter)
    monkeypatch.setattr(adapter,'_start_polling_resilient',AsyncMock(return_value=False))
    try:
        assert not await adapter.connect(is_reconnect=True)
        assert not adapter.is_connected
        assert adapter.transport_health()['status']=='degraded'
    finally:await _cancel_heartbeat(adapter)

@pytest.mark.asyncio
async def test_health_uses_current_generation_and_expires_stalled_progress(adapter):
    adapter._running=True
    generation,_=adapter._begin_polling_generation()
    assert adapter.transport_health()['status']=='degraded'
    adapter._record_polling_progress(generation)
    assert adapter.transport_health()['status']=='ok'
    new_generation,_=adapter._begin_polling_generation()
    adapter._record_polling_progress(generation)
    assert adapter.transport_health()['status']=='degraded'
    adapter._record_polling_progress(new_generation)
    adapter._polling_last_progress_monotonic=time.monotonic()-_POLLING_STALL_TIMEOUT-1
    assert adapter.transport_health()['status']=='degraded'

@pytest.mark.asyncio
async def test_http_liveness_does_not_masquerade_as_transport_readiness(adapter):
    api=object.__new__(APIServerAdapter)
    api.gateway_runner=SimpleNamespace(config=SimpleNamespace(platforms={Platform.TELEGRAM:PlatformConfig(enabled=True,token='test')}),adapters={Platform.TELEGRAM:adapter})
    resp=await api._handle_health(None)
    data=json.loads(resp.text)
    assert data['status']=='ok'
    assert data['transport_readiness']['telegram']['status']=='degraded'
    api.gateway_runner.adapters={}
    assert api._transport_readiness()['telegram']['reason']=='adapter_unavailable'

@pytest.mark.asyncio
async def test_detailed_health_degrades_when_telegram_has_not_proven_progress(monkeypatch,adapter):
    api=object.__new__(APIServerAdapter)
    api.gateway_runner=SimpleNamespace(config=SimpleNamespace(platforms={Platform.TELEGRAM:PlatformConfig(enabled=True,token='test')}),adapters={Platform.TELEGRAM:adapter})
    api._check_auth=lambda request:None
    api._readiness_work_counts=lambda:(0,0,0)
    monkeypatch.setattr('gateway.platforms.api_server.collect_runtime_readiness',lambda **kw:{'status':'ok','checks':{}})
    monkeypatch.setattr('gateway.status.read_runtime_status',lambda:{'gateway_state':'running','active_agents':0})
    monkeypatch.setattr('gateway.run._resolve_gateway_model',lambda:'test-model')
    response=await api._handle_health_detailed(None)
    data=json.loads(response.text)
    assert data['status']=='degraded'
    assert data['readiness']['checks']['telegram']['status']=='degraded'

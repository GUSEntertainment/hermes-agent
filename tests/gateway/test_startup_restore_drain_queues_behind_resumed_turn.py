"""Regression (t_4d40d61d, 2026-10-07 13:27).

Gateway restarted mid-turn. Startup auto-resume restarted the interrupted turn
on Chris's DM session while two Telegram messages were queued by the startup
restore gate. When the queue drained, the second message hit the per-session
turn lease held by the resumed turn, timed out after the short 5s live budget
and was bounced with "Another turn is still running ... resend it".

Drained startup-restore messages were already accepted; they must queue
(serialized, in order) behind the resumed turn, never be rejected.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.turn_lease import (
    STARTUP_RESTORE_REPLAY_LEASE_WAIT,
    SessionTurnLeaseRegistry,
    TurnLeaseTimeoutError,
)


class _Stop(Exception):
    """Raised from load_transcript: the turn got past the lease."""


def _replay_event(text, msg_id):
    from tests.gateway.test_duplicate_user_message import _event

    ev = _event()
    ev.text = text
    ev.message_id = msg_id
    setattr(ev, "_hermes_startup_restore_replay", True)
    return ev


def test_replay_budget_is_long_and_bounded():
    assert 600 <= STARTUP_RESTORE_REPLAY_LEASE_WAIT <= 3600


@pytest.mark.asyncio
async def test_restart_resumed_turn_then_two_drained_messages_all_run_in_order(
    monkeypatch, tmp_path
):
    from tests.gateway.test_duplicate_user_message import _bootstrap, _source

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._turn_leases = SessionTurnLeaseRegistry()
    key = "agent:main:telegram:group:-1001:12345"

    # 1. The resumed (restart-interrupted) turn owns the session lease.
    resumed = await runner._turn_leases.acquire(
        "sess-dedup", owner_key="resume-key", generation=1, timeout=1
    )
    # Live contention budget stays short (this is what bounced the message).
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.02")

    order = []

    def _load(session_id):
        order.append(session_id)
        raise _Stop()

    runner.session_store.load_transcript.side_effect = _load
    runner._run_agent = pytest.fail

    # 2. Two queued messages drain while the resumed turn is still running.
    first = asyncio.ensure_future(
        runner._handle_message_with_agent(_replay_event("one", "m1"), _source(), key, 1)
    )
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(
        runner._handle_message_with_agent(_replay_event("two", "m2"), _source(), key, 2)
    )
    # Well past the 0.02s live budget: neither may have been rejected.
    await asyncio.sleep(0.3)
    assert not first.done(), "drained message 1 was rejected instead of queued"
    assert not second.done(), "drained message 2 was rejected instead of queued"
    assert order == []

    # 3. Resumed turn finishes -> drained message 1 runs, then message 2.
    assert runner._turn_leases.release(resumed) is True
    with pytest.raises(_Stop):
        await asyncio.wait_for(first, timeout=2)
    assert not second.done()
    tok1 = runner._session_state(key).turn.lease_tokens.get(1)  # v0.21.5: tokens keyed by run generation
    assert tok1 is not None and tok1.generation == 1
    assert runner._turn_leases.release(tok1) is True

    with pytest.raises(_Stop):
        await asyncio.wait_for(second, timeout=2)
    tok2 = runner._session_state(key).turn.lease_tokens.get(2)
    assert tok2.generation == 2
    runner._turn_leases.release(tok2)
    assert order == ["sess-dedup", "sess-dedup"]


@pytest.mark.asyncio
async def test_live_message_still_fails_closed_fast(monkeypatch, tmp_path):
    """Only drained replays get the long wait; live contention is unchanged."""
    from tests.gateway.test_duplicate_user_message import (
        _bootstrap,
        _event,
        _source,
    )

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._turn_leases = SessionTurnLeaseRegistry()
    holder = await runner._turn_leases.acquire(
        "sess-dedup", owner_key="h", generation=1, timeout=1
    )
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.02")
    runner._run_agent = pytest.fail
    try:
        with pytest.raises(TurnLeaseTimeoutError):
            await asyncio.wait_for(
                runner._handle_message_with_agent(
                    _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
                ),
                timeout=2,
            )
    finally:
        runner._turn_leases.release(holder)


@pytest.mark.asyncio
async def test_drain_marks_every_queued_message_as_replay(monkeypatch, tmp_path):
    from tests.gateway.test_duplicate_user_message import _bootstrap, _event

    runner = _bootstrap(monkeypatch, tmp_path)
    seen = []
    adapter = SimpleNamespace(
        handle_message=AsyncMock(
            side_effect=lambda ev: seen.append(
                getattr(ev, "_hermes_startup_restore_replay", False)
            )
        )
    )
    runner._intake_adapter_for = lambda _src: adapter  # renamed upstream in v0.21.5
    runner._startup_restore_queue = [_event(), _event()]
    assert await runner._drain_startup_restore_queue() == 2
    assert seen == [True, True]

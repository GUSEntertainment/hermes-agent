"""Cancellation must release summary capacity without committing stale history."""
import copy
import threading
from unittest.mock import Mock
import pytest
import agent.auxiliary_client as aux
from agent.conversation_compression import CompressionCommitFence, compress_context
from tests.agent.test_compression_stall_interrupt_96775 import _build_agent, _messages


def test_cancelled_waiter_never_dispatches_or_steals_permit(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    checked = threading.Event()
    cancelled = threading.Event()
    done = threading.Event()
    outcomes = []
    dispatch = Mock()
    monkeypatch.setattr(aux, "_acquire_sync_aux_semaphore", lambda task: sem)
    monkeypatch.setattr(aux, "_call_llm_impl", dispatch)
    def check():
        checked.set()
        return cancelled.is_set()
    def waiter():
        try:
            with aux.aux_interrupt_protection(cancel_check=check):
                aux.call_llm(task="compression", messages=[])
        except aux.AuxiliaryExplicitCancellation:
            outcomes.append("cancelled")
        finally:
            done.set()
    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    try:
        assert checked.wait(3)
        cancelled.set()
        assert done.wait(3), "cancelled queue waiter stayed blocked"
        assert outcomes == ["cancelled"]
        dispatch.assert_not_called()
        assert not sem.acquire(blocking=False), "waiter released another request's permit"
    finally:
        cancelled.set()
        sem.release()
        t.join(3)
    assert sem.acquire(blocking=False)
    sem.release()


@pytest.mark.parametrize("limited", [True, False])
def test_precancelled_request_cannot_dispatch(monkeypatch, limited):
    sem = threading.BoundedSemaphore(1) if limited else None
    dispatch = Mock()
    monkeypatch.setattr(aux, "_acquire_sync_aux_semaphore", lambda task: sem)
    monkeypatch.setattr(aux, "_call_llm_impl", dispatch)
    with aux.aux_interrupt_protection(cancel_check=lambda: True):
        with pytest.raises(aux.AuxiliaryExplicitCancellation):
            aux.call_llm(task="compression", messages=[])
    dispatch.assert_not_called()
    if sem:
        assert sem.acquire(blocking=False)
        sem.release()


def test_real_compression_fence_reaches_aux_request_and_preserves_history(tmp_path):
    db, agent = _build_agent(tmp_path, "GUS_FENCE_CANCEL")
    source = _messages()
    messages = copy.deepcopy(source)
    fence = CompressionCommitFence()
    seen = []
    def summary(msgs, **kwargs):
        assert aux._aux_interrupt_protected()
        assert not aux._aux_interrupt_cancel_requested()
        fence.cancel_before_commit()
        seen.append(aux._aux_interrupt_cancel_requested())
        if seen[-1]:
            raise aux.AuxiliaryExplicitCancellation()
        raise AssertionError("host cancellation did not reach summary provider")
    agent.context_compressor.compress = summary
    try:
        result, _ = compress_context(agent, messages, "sys", approx_tokens=3000,
                                     commit_fence=fence)
        assert seen == [True]
        assert result == source and messages == source
        assert agent.session_id == "GUS_FENCE_CANCEL"
        assert not aux._aux_interrupt_protected()
    finally:
        agent.close()
        db.close()

"""Proactive owner messages must be in the live session when the owner replies.

Live failure 2026-09-30 13:13 EDT: no_agent cron 777fb73703cc
("Shellworks + NH bottler reply -> Photon", deliver=photon, origin=None)
sent Chris "Heads up, Rosalie just got back to you on the bottles. Want the
details, or should I start on a reply?" over Photon via the live adapter.
The Photon DM session (20260917_224803_6b637b50) got no matching turn, so at
13:25 "Yea let's work it out" was answered with a Hermes-restart instruction.

Root cause: deliver=photon resolves through the bare-platform home-channel
branch (no _resolved_from tag) -> _target_mirror_eligible() False ->
_maybe_mirror_cron_delivery(enabled=False). Fix: any delivery to a chat whose
live gateway session is a 1:1 DM is mirrored as an assistant turn with a
hidden provenance note.
"""

import asyncio
import types

import pytest

CHAT = "any;-;+15550001111"
PROACTIVE = (
    "Heads up, Rosalie just got back to you on the bottles. "
    "Want the details, or should I start on a reply?"
)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "platforms:\n  photon:\n    enabled: true\n"
        f"    home_channel:\n      platform: photon\n      chat_id: '{CHAT}'\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PHOTON_HOME_CHANNEL", CHAT)

    import importlib
    import hermes_state
    importlib.reload(hermes_state)
    db = hermes_state.SessionDB(db_path=home / "state.db")
    sid = "20260930_photon_owner_dm"
    db.create_session(
        sid, "photon", user_id="+15550001111",
        session_key=f"agent:main:photon:dm:{CHAT}", chat_id=CHAT, chat_type="dm",
    )
    db.append_message(sid, "user", "Anything back from Shellworks?")
    db.append_message(sid, "assistant", "Not yet, nothing new since this morning.")
    db.close()

    sent = []

    class FakePhoton:
        platform = types.SimpleNamespace(value="photon")

        async def send(self, chat_id, content, metadata=None, **kw):
            sent.append((chat_id, content))
            return types.SimpleNamespace(success=True, message_id="spc-msg-1", error=None)

    return {"home": home, "sid": sid, "sent": sent, "adapter": FakePhoton(),
            "SessionDB": lambda: hermes_state.SessionDB(db_path=home / "state.db")}


def _conversation(env):
    db = env["SessionDB"]()
    try:
        return db.get_messages_as_conversation(env["sid"])
    finally:
        db.close()


def test_cron_photon_proactive_lands_in_owner_dm_then_reply_has_context(env, monkeypatch):
    import gateway.mirror as mirror
    from gateway.config import Platform
    import cron.scheduler as sched

    # Real failing job shape.
    job = {"id": "777fb73703cc", "name": "Shellworks + NH bottler reply → Photon",
           "deliver": "photon", "origin": None, "no_agent": True}

    targets = sched._resolve_delivery_targets(job)
    assert targets and targets[0]["chat_id"] == CHAT
    # Pre-fix gate: this target is NOT mirror-eligible (the bug).
    from cron.scheduler_delivery import _target_mirror_eligible  # moved upstream in v0.21.5
    assert not _target_mirror_eligible(job, targets[0], global_mirror=False)

    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        err = sched._deliver_result(job, PROACTIVE,
                                    adapters={Platform("photon"): env["adapter"]}, loop=loop)
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert err is None, err
    assert env["sent"] and PROACTIVE in env["sent"][0][1]

    # Owner replies referencing it.
    db = env["SessionDB"]()
    db.append_message(env["sid"], "user", "Yes let's go over it")
    db.close()

    conv = _conversation(env)
    roles = [m["role"] for m in conv]
    texts = [str(m.get("content") or "") for m in conv]
    idx = next(i for i, x in enumerate(texts) if PROACTIVE in x)
    assert roles[idx] == "assistant"
    assert "sent proactively via cron job" in texts[idx]
    assert "777fb73703cc" in texts[idx]
    assert texts[idx + 1] == "Yes let's go over it"

    # Alternation repair (pass 0) merges assistant;assistant and keeps the text.
    from agent.agent_runtime_helpers import repair_message_sequence
    msgs = [dict(m) for m in conv]
    repair_message_sequence(types.SimpleNamespace(), msgs)
    assert any(PROACTIVE in str(m.get("content")) and m["role"] == "assistant" for m in msgs)
    print("\nAGENT CONTEXT AFTER REPLY:")
    for m in msgs:
        print(f"  {m['role']}: {str(m.get('content'))[:160]!r}")


def test_group_target_without_opt_in_is_not_mirrored(env):
    """Group/broadcast chats keep the old opt-in rule."""
    db = env["SessionDB"]()
    db.create_session("grp", "photon", user_id="u", session_key="agent:main:photon:group:G1",
                      chat_id="G1", chat_type="group")
    db.close()
    from gateway.mirror import find_owner_dm_session
    assert find_owner_dm_session("photon", "G1") is None
    assert find_owner_dm_session("photon", CHAT) == env["sid"]


def test_delegated_return_note_mirrors_into_owner_dm(env):
    from gateway.mirror import find_owner_dm_session, mirror_proactive_message
    sid = find_owner_dm_session("photon", CHAT)
    assert mirror_proactive_message("photon", CHAT, "Your NDA draft is ready.",
                                    source="delegated work return", ref="task t_x",
                                    session_id=sid)
    texts = [str(m.get("content")) for m in _conversation(env)]
    assert any("Your NDA draft is ready." in x and "task t_x" in x for x in texts)

"""Synthetic principals only; never calls a real model or sends a notification."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from gateway.platforms import api_server_app_shared as shared


class FakeServer:
    config = SimpleNamespace(extra={"app_shared": {"enabled": True,
        "contacts": {"alice": {"name": "Alice"}, "bob": {"name": "Bob"},
                     "eve": {"name": "Eve"}},
        "agents": {"friend": {"name": "Friend"}}}})

    def _expected_api_key(self):
        return "synthetic"

    def _check_auth(self, request):
        request["hermes.authenticated_principal"] = request.principal
        return None

    async def _read_json_body(self, request):
        return request.body, None


class Request(dict):
    def __init__(self, principal, action, room=None, body=None, invite=None):
        super().__init__()
        self.principal = principal
        self.headers = {"X-Hermes-Shared-Principal":principal}
        self.method = "POST" if action in {"create", "message", "invite", "approve", "remove", "settings"} else "GET"
        self.match_info = {"id": room, "invite": invite}
        self.body = body or {}


def call(principal, action, room=None, body=None, invite=None):
    if action == "approve" and body and isinstance(body.get("text"),str) and not body["text"].endswith(shared._URL+room):
        body = {**body,"text":body["text"]+"\n"+shared._URL+room}
    response = asyncio.run(shared.handle(FakeServer(), Request(principal, action, room, body, invite), action))
    return response.status, json.loads(response.body)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(shared.hosted_rooms, "default_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(shared.hosted_rooms, "local_authority_gateway_id", lambda: "install:synthetic")
    # No real model invocation; check the pending hosted event instead.
    monkeypatch.setattr(shared.asyncio, "create_task", lambda coroutine: coroutine.close())
    return tmp_path / "app-shared.db"


def test_membership_approval_duplicates_and_removal(store):
    room = str(uuid.uuid4())
    status, detail = call("alice", "create", body={"id":room,"agent":"friend","name":"Test", "summary":"Only room context", "contact":"bob"})
    assert status == 201
    assert detail["room"]["members"] == [{"id":"alice","name":"Alice"}]
    assert detail["invites"][0]["status"] == "pending"
    invite = detail["invites"][0]["id"]
    assert call("bob", "get", room)[0] == 404
    assert call("bob", "message", room, {"id":str(uuid.uuid4()),"text":"no"})[0] == 404
    assert call("eve", "approve", room, {"text":"yes"}, invite)[0] == 404
    assert call("alice", "approve", room, {"text":"Edited by Alice"}, invite)[0] == 200
    assert call("alice", "approve", room, {"text":"Different"}, invite)[0] == 400
    status, bob = call("bob", "get", room)
    assert status == 200 and bob["room"]["owner"] is False and bob["invites"] == []
    message = str(uuid.uuid4())
    assert call("bob", "message", room, {"id":message,"text":"Hello"})[0] == 200
    assert call("bob", "message", room, {"id":message,"text":"Hello"})[0] == 200
    assert len(call("alice", "get", room)[1]["messages"]) == 1
    assert call("eve", "get", room)[0] == 404
    assert call("bob", "remove", room, {"contact":"alice"})[0] == 403
    assert call("alice", "remove", room, {"contact":"bob"})[0] == 200
    assert call("bob", "get", room)[0] == 404
    assert call("bob", "message", room, {"id":str(uuid.uuid4()),"text":"no"})[0] == 404
    assert call("bob", "list")[1]["rooms"] == []


def test_unknown_and_private_room_denial(store):
    assert call("alice", "contacts")[1]["sendWithoutAsking"] is False
    assert call("alice", "create", body={"id":str(uuid.uuid4()),"agent":"friend","name":"Test","summary":"Summary","contact":"unknown"})[0] == 400
    assert call("eve", "get", str(uuid.uuid4()))[0] == 404
    assert call("alice", "settings", body={"sendWithoutAsking":True})[0] == 200
    assert call("bob", "contacts")[1]["sendWithoutAsking"] is False


def test_text_only_transport_contract(monkeypatch):
    from agent import auxiliary_client
    seen = {}
    def fake_call(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Model answer",tool_calls=None))])
    monkeypatch.setattr(auxiliary_client, "call_llm", fake_call)
    result = shared._run_agent("Shared summary", [{"author":{"kind":"human","name":"Bob"},"text":"Hi"}])
    assert result == "Model answer"
    assert seen["tools"] == [] and len(seen["messages"]) == 2
    assert seen["messages"][1] == {"role":"user","content":"Bob: Hi"}
    assert "Shared summary" in seen["messages"][0]["content"]
    # No AIAgent constructor, profile, filesystem, memory or tool dispatcher.
    import inspect
    code = inspect.getsource(shared._run_agent)
    assert "from run_agent" not in code and "run_conversation(" not in code


def test_owner_setting_and_invite_notifications(store):
    call("alice", "settings", body={"sendWithoutAsking": True})
    room = str(uuid.uuid4())
    status, detail = call("alice", "create", body={"id":room,"agent":"friend","name":"Test","summary":"Summary","contact":"bob"})
    assert status == 201 and detail["invites"][0]["status"] == "sent"
    assert call("bob", "get", room)[0] == 200
    assert len(call("bob", "notifications")[1]["notifications"]) == 1
    assert call("eve", "notifications")[1]["notifications"] == []
    call("alice", "settings", body={"sendWithoutAsking": False})
    invite = str(uuid.uuid4())
    assert call("bob", "invite", room, {"id":invite,"contact":"eve"})[0] == 200
    assert call("eve", "get", room)[0] == 404
    assert call("bob", "approve", room, {"text":"No"}, invite)[0] == 403
    assert call("alice", "approve", room, {"text":"Owner approved"}, invite)[0] == 200
    assert call("eve", "get", room)[0] == 200


def test_real_hosted_events_feed_only_room_summary(store,monkeypatch):
    room = str(uuid.uuid4())
    call("alice", "create", body={"id":room,"agent":"friend","name":"Test","summary":"PUBLIC SEED","contact":"bob"})
    call("alice", "message", room, {"id":str(uuid.uuid4()),"text":"PUBLIC MESSAGE"})
    observed=[]
    def model(summary,messages):
        observed.append((summary,messages));return "TEST DOUBLE RESPONSE"
    monkeypatch.setattr(shared,"_run_agent",model)
    shared._process(store,room,FakeServer.config.extra["app_shared"]["contacts"])
    shared._process(store,room,FakeServer.config.extra["app_shared"]["contacts"])
    assert len(observed)==1
    assert observed[0][0]=="Agent: Friend\nPUBLIC SEED"
    assert [m["text"] for m in observed[0][1]] == ["PUBLIC MESSAGE"]
    detail=call("alice","get",room)[1]
    assert len(detail["messages"])==2
    assert detail["messages"][1]["author"]["kind"]=="agent"


def test_shared_store_cannot_be_read_through_generic_hosted_store(store):
    room = str(uuid.uuid4())
    call("alice", "create", body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
    with pytest.raises(shared.hosted_rooms.RoomNotFoundError):
        shared.hosted_rooms.room_state(store.with_name("state.db"),room_id=room)
    parent = str(uuid.uuid4())
    shared.hosted_rooms.create_room(store.with_name("state.db"),room_id=parent,name="Private",members=[{"member_id":"friend","handle":"friend","profile":"alice","display_name":"Friend"}],authority_gateway_id="install:synthetic")
    assert call("bob","get",parent)[0] == 404
    assert call("alice","get",parent)[0] == 404


def test_binding_assertion_rejected_before_store_write(store):
    request=Request("alice","settings",body={"sendWithoutAsking":True})
    request.headers["X-Hermes-Shared-Principal"]="bob"
    response=asyncio.run(shared.handle(FakeServer(),request,"settings"))
    assert response.status==403
    assert not store.exists()


def test_pending_turns_drain_and_expired_lease_recovers(store,monkeypatch):
    room=str(uuid.uuid4())
    call("alice","create",body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
    for text in ["First","Second"]:
        call("alice","message",room,{"id":str(uuid.uuid4()),"text":text})
    answers=[]
    monkeypatch.setattr(shared,"_run_agent",lambda summary,messages: answers.append(messages) or "Answer")
    with shared._connect(store) as db:
        db.execute("UPDATE app_shared_rooms SET lease_until=?,lease_token='other'",(shared.time.time()+300,))
    shared._process(store,room,FakeServer.config.extra["app_shared"]["contacts"])
    assert answers==[]
    with shared._connect(store) as db:
        db.execute("UPDATE app_shared_rooms SET lease_until=0")
    shared._process(store,room,FakeServer.config.extra["app_shared"]["contacts"])
    assert len(answers)==2
    assert [m["text"] for m in answers[0]]==["First"]
    assert any(m["text"]=="Answer" for m in answers[1])
    assert len(call("alice","get",room)[1]["messages"])==4


def test_guarded_invite_delivery_exactly_once_only_after_owner_approval(store,monkeypatch):
    from enum import Enum
    class Platform(Enum): PHOTON="photon"
    sent=[]
    class Adapter:
        async def send(self,target,text,metadata):
            sent.append((target,text,metadata));return SimpleNamespace(success=True)
    server=FakeServer()
    server.gateway_runner=SimpleNamespace(adapters={Platform.PHOTON:Adapter()})
    contacts={**server.config.extra["app_shared"]["contacts"],"bob":{"name":"Bob","photon_chat_id":"synthetic-known-target"}}
    room=str(uuid.uuid4())
    _,detail=call("alice","create",body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
    asyncio.run(shared._deliver_pending(server,store,contacts))
    assert sent==[]
    invitation=detail["invites"][0]
    call("alice","approve",room,{"text":invitation["text"]},invitation["id"])
    asyncio.run(shared._deliver_pending(server,store,contacts))
    asyncio.run(shared._deliver_pending(server,store,contacts))
    assert len(sent)==1 and sent[0][0]=="synthetic-known-target"
    assert sent[0][1]==invitation["text"]
    assert sent[0][2]=={"logical_message_id":"shared-invite:"+invitation["id"]}
    assert call("alice","get",room)[1]["invites"][0]["delivery"]=={"channel":"photon","state":"delivered"}


def test_removal_cancels_later_claimed_delivery(store):
    from enum import Enum
    class Platform(Enum): PHOTON="photon"
    rooms=[]
    for _ in range(2):
        room=str(uuid.uuid4())
        _,detail=call("alice","create",body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
        invite=detail["invites"][0]
        call("alice","approve",room,{"text":invite["text"]},invite["id"])
        rooms.append(room)
    sent=[]
    class Adapter:
        async def send(self,target,text,metadata):
            sent.append(text)
            with shared._connect(store) as db:
                db.execute("DELETE FROM app_shared_members WHERE room_id=? AND principal='bob'",(rooms[1],))
                db.execute("DELETE FROM app_shared_invites WHERE room_id=?",(rooms[1],))
            return SimpleNamespace(success=True)
    server=FakeServer()
    server.gateway_runner=SimpleNamespace(adapters={Platform.PHOTON:Adapter()})
    contacts={**server.config.extra["app_shared"]["contacts"],"bob":{"name":"Bob","photon_chat_id":"synthetic-target"}}
    asyncio.run(shared._deliver_pending(server,store,contacts))
    assert len(sent)==1


def test_active_app_session_prevents_photon_fallback(store):
    room=str(uuid.uuid4())
    _,detail=call("alice","create",body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
    call("bob","contacts")
    invitation=detail["invites"][0]
    call("alice","approve",room,{"text":invitation["text"]},invitation["id"])
    asyncio.run(shared._deliver_pending(FakeServer(),store,FakeServer.config.extra["app_shared"]["contacts"]))
    assert call("alice","get",room)[1]["invites"][0]["delivery"]=={"channel":"app","state":"delivered"}


def test_room_file_acl_and_removal(store):
    room=str(uuid.uuid4())
    _,detail=call("alice","create",body={"id":room,"agent":"friend","name":"Test","summary":"Seed","contact":"bob"})
    file_id=str(uuid.uuid4())
    request=Request("alice","upload",room,body={"id":file_id,"name":"shared.txt","data":"SGVsbG8="})
    request.method="POST"
    assert asyncio.run(shared.handle(FakeServer(),request,"upload")).status==200
    def download(principal):
        request=Request(principal,"download",room);request.match_info["file"]=file_id
        return asyncio.run(shared.handle(FakeServer(),request,"download"))
    assert download("bob").status==404
    invitation=detail["invites"][0]
    call("alice","approve",room,{"text":invitation["text"]},invitation["id"])
    assert json.loads(download("bob").body)["data"]=="SGVsbG8="
    assert download("eve").status==404
    call("alice","remove",room,{"contact":"bob"})
    assert download("bob").status==404


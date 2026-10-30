"""Real durable service/driver, only model transport replaced (no paid calls)."""
import importlib.util
import sys
import types
import uuid
from pathlib import Path

from test_app_rooms import app, Server, call
from gateway import hosted_room_driver as driver
from tui_gateway.hosted_room_driver import HostedRoomBinding

spec = importlib.util.spec_from_file_location('app_room_service_under_test', Path(__file__).parents[1] / 'tui_gateway/hosted_room_service.py')
service_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_module)

class RPC:
    def __init__(self): self.prompts=[]
    def resolve_exact(self, **kw): return None
    def create(self, **kw): return {'session_id': kw['profile']+'-session', 'title': kw['title']}
    def resume(self, **kw): return {'session_id': kw['session_id']}
    def submit(self, **kw):
        self.prompts.append((kw['profile'], kw['prompt']))
        kw['on_terminal']({'status':'settled','text':f"Reply {len(self.prompts)} from {kw['profile']}"})
        return {'accepted':True}
    def history(self, **kw): return []
    def info(self, **kw): return {'active':False,'task_id':None}
    def interrupt(self, **kw): return {'interrupted':True}

def test_real_driver_exchange_interjection_cap_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_approval_ready', lambda: True)
    db=tmp_path/'state.db'
    (tmp_path/'profiles'/'bob').mkdir(parents=True)
    monkeypatch.setattr(app,'_db',lambda:db)
    monkeypatch.setattr(app.hosted_rooms,'local_authority_gateway_id',lambda:'test-gateway')
    service=service_module.HostedRoomService(types.SimpleNamespace(),db_path=db)
    rpc=RPC()
    service.rpc=rpc
    service.runtime.rpc=rpc
    monkeypatch.setattr(app,'_service',lambda *_:service)
    server=Server(); room_id=str(uuid.uuid4())
    status,data=call(server,'create','alice',{'id':room_id,'name':'Ideas','agents':['ada','bob']})
    assert status==201,data
    status,data=call(server,'message','alice',{'id':str(uuid.uuid4()),'text':'Discuss the options'},room_id)
    assert status==200,data
    for _ in range(15): service.runtime._process_room(service.bindings()[0])
    status,data=call(server,'get','alice',room_id=room_id)
    assert status==200,data
    assert {'ada','bob'} <= {m['agent'] for m in data['messages']}
    assert 'Reply 1' in rpc.prompts[1][1]
    first=len(rpc.prompts)
    status,data=call(server,'message','alice',{'id':str(uuid.uuid4()),'text':'Consider the budget too'},room_id)
    assert status==200,data
    for _ in range(20): service.runtime._process_room(service.bindings()[0])
    assert len(rpc.prompts)>first
    assert len(rpc.prompts)<=12
    # Each settled two-agent exchange contributes to the same room budget.
    for index in range(4):
        status, data = call(server,'message','alice',{'id':str(uuid.uuid4()),'text':f'Another angle {index}'},room_id)
        assert status == 200, data
        for _ in range(20): service.runtime._process_room(service.bindings()[0])
    assert len(rpc.prompts)==12
    assert call(server,'get','alice',room_id=room_id)[1]['status']=='paused'
    saved_messages=call(server,'get','alice',room_id=room_id)[1]['messages']
    # Reconstruct service from disk; state and budget survive a gateway reload.
    for lease in service.runtime._leases.values():
        driver.release_lease(db, lease=lease, clock=service.runtime.clock)
    replacement=service_module.HostedRoomService(types.SimpleNamespace(),db_path=db)
    replacement.rpc=rpc;replacement.runtime.rpc=rpc
    monkeypatch.setattr(app,'_service',lambda *_:replacement)
    assert call(server,'get','alice',room_id=room_id)[1]['messages']==saved_messages
    for _ in range(3): replacement.runtime._process_room(replacement.bindings()[0])
    assert len(rpc.prompts)==12
    limits=call(server,'get','alice',room_id=room_id)[1]['limits']
    assert limits == {'usedTurns':12,'currentTurnLimit':12,'lifetimeTurnLimit':48,'canContinue':True}
    # Typing or reloading cannot consent to another batch.
    call(server,'message','alice',{'id':str(uuid.uuid4()),'text':'Another question while paused'},room_id)
    for _ in range(10): replacement.runtime._process_room(replacement.bindings()[0])
    assert len(rpc.prompts)==12
    assert call(server,'resume','alice',{},room_id)[0]==200
    for _ in range(40): replacement.runtime._process_room(replacement.bindings()[0])
    assert 12 < len(rpc.prompts) <= 24
    assert call(server,'stop','alice',{},room_id)[1]['status']=='stopped'
    assert call(server,'get','eve',room_id=room_id)[0]==404

def test_approval_owner_exact_request_once_only(tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_approval_ready', lambda: True)
    db=tmp_path/'state.db'
    monkeypatch.setattr(app,'_db',lambda:db)
    monkeypatch.setattr(app.hosted_rooms,'local_authority_gateway_id',lambda:'test-gateway')
    service=service_module.HostedRoomService(types.SimpleNamespace(),db_path=db)
    (tmp_path/'profiles'/'bob').mkdir(parents=True)
    approved=[]
    rpc=RPC()
    rpc.approve=lambda **kw: approved.append(kw) or {'ok':True}
    service.rpc=rpc; service.runtime.rpc=rpc
    monkeypatch.setattr(app,'_service',lambda *_:service)
    server=Server(); room_id=str(uuid.uuid4())
    assert call(server,'create','alice',{'id':room_id,'name':'Approval','agents':['ada','bob']})[0]==201
    service._set_pending_action(room_id,'ada',{'kind':'approval','task_id':'task-one','execution_generation':1,'request_id':'approval-one','session_id':'private-session','approval':{'command':'Send the requested email'}})
    detail=call(server,'get','alice',room_id=room_id)[1]
    a=detail['approvals'][0]
    assert a['description']=='Send the requested email'
    assert 'private-session' not in str(detail)
    payload={k:v for k,v in a.items() if k!='description'}|{'choice':'once'}
    assert call(server,'approval','eve',payload,room_id)[0]==404
    assert not approved
    assert call(server,'approval','alice',payload|{'request':'stale'},room_id)[0]==409
    assert not approved
    assert call(server,'approval','alice',payload|{'choice':'always'},room_id)[0]==400
    assert call(server,'approval','alice',payload,room_id)[0]==200
    assert len(approved)==1 and approved[0]['choice']=='once'
    assert call(server,'approval','alice',payload,room_id)[0]==409
    assert len(approved)==1

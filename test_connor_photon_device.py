import json,time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest,yaml
from tools import photon_computer_tool as t
from gateway import owner_device_tools as d
from plugins.platforms.photon.secure_intake.request_registry import RequestRegistry,encoded
from plugins.platforms.photon.secure_intake.trusted_intake import Publisher,Capture,_CAPTURE,_CURRENT
from plugins.platforms.photon.secure_intake.errors import Denied

@pytest.fixture
def source(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    f=tmp_path/'registry.sqlite';f.touch(mode=0o600)
    registry=RequestRegistry(f)
    identity=dict(platform='photon',chat_type='dm',conversation='fixture-thread',sender='fixture-sender',message='fixture-message',session='fixture-session')
    profile=dict(owner='connor',tenant='gus',sender=identity['sender'],conversation=identity['conversation'],enabled=True,revoked=False,generation='a'*64)
    publisher=Publisher(registry,enrollment=lambda _:profile.copy(),delegated=lambda:False)
    capture=Capture(_CAPTURE,(), 'b'*64,int(time.time()));publisher.capture(identity,capture)
    token=_CURRENT.set((encoded(identity),capture.digest))
    monkeypatch.setattr(t,'current_identity',lambda:identity.copy())
    monkeypatch.setattr(t.composition,'start',lambda:(None,publisher,None))
    root=tmp_path/'profiles';root.mkdir(mode=0o700);home=root/'connor';home.mkdir(mode=0o700)
    keys=tmp_path/'keys';keys.mkdir(mode=0o700);key=keys/'connor';key.write_text('fixture');key.chmod(0o600)
    config={'owner_device_enabled':True,'terminal':{'backend':'ssh','ssh_sync_files':False,'ssh_host':'connor.invalid','ssh_user':'connor','ssh_key':str(key)}}
    cfg=home/'config.yaml';cfg.write_text(yaml.safe_dump(config));cfg.chmod(0o600)
    monkeypatch.setattr(d,'PROFILE_ROOT',root);monkeypatch.setattr(d,'KEY_ROOT',keys)
    execute=Mock(return_value={'status':'completed','stdout':'fixture-connor'});monkeypatch.setattr(d,'_execute',execute)
    try:yield SimpleNamespace(identity=identity,profile=profile,publisher=publisher,registry=registry,config=config,cfg=cfg,execute=execute)
    finally:_CURRENT.reset(token)

def test_actual_registry_to_mapped_device(source):
    assert t.available()
    assert json.loads(t.handle({'command':'hostname'}))['stdout']=='fixture-connor'
    assert source.execute.call_args.args[0][:3]==('connor','connor.invalid','connor')
    assert source.execute.call_count==1

@pytest.mark.parametrize('owner',['chris','joshgarcia','other'])
def test_other_owner_never_uses_connor_mac(source,owner):
    source.profile['owner']=owner
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_no_captured_current_message(source):
    _CURRENT.set(None)
    assert not t.available()
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_wrong_message(source):
    source.identity['message']='different'
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_revoked_source(source):
    with source.registry._db() as db:db.execute('UPDATE intake_sources SET revoked=1')
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_expired_source(source):
    with source.registry._db() as db:db.execute('UPDATE intake_sources SET expires=0')
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_no_profile_override(source):
    with pytest.raises(ValueError):t.handle({'command':'hostname','profile':'chris'})
    source.execute.assert_not_called()

def test_delegation_denied(source):
    source.publisher.delegated=lambda:True
    with pytest.raises(Denied):t.handle({'command':'hostname'})
    source.execute.assert_not_called()

def test_enrollment_changes_during_execution(source):
    def change(*args):source.profile['generation']='c'*64;return {'stdout':'private'}
    source.execute.side_effect=change
    with pytest.raises(PermissionError):t.handle({'command':'hostname'})
    assert source.execute.call_count==1

def test_no_copy_is_required(source):
    source.config['terminal']['ssh_sync_files']=True;source.cfg.write_text(yaml.safe_dump(source.config))
    with pytest.raises(PermissionError):t.handle({'command':'hostname'})
    source.execute.assert_not_called()


def test_registry_availability_never_cached_across_users(source):
    from tools.registry import _check_fn_cached
    assert _check_fn_cached(t.available)
    source.profile['owner']='chris'
    assert not _check_fn_cached(t.available)
    source.profile['owner']='connor'
    assert _check_fn_cached(t.available)


def allow_fallback(source):
    source.config['owner_device_fallback']='chris-local'
    source.cfg.write_text(yaml.safe_dump(source.config))


def test_explicit_network_fallback_before_action(source,monkeypatch):
    import subprocess
    allow_fallback(source)
    source.execute.return_value={'status':'unavailable','reason':'network'}
    run=Mock(return_value=subprocess.CompletedProcess([],0));monkeypatch.setattr(t.subprocess,'run',run)
    result=json.loads(t.handle({'command':'echo fixture'}))
    assert result['execution_device']=='chris-mac' and result['fallback']
    assert source.execute.call_count==1 and source.execute.call_args.args[1]=='true'
    assert run.call_count==1 and run.call_args.args[0][-1]=='echo fixture'
    assert set(run.call_args.kwargs['env'])=={'HOME','PATH','LANG'}

@pytest.mark.parametrize('result',[{'status':'timeout','execution':'unknown'}, {'status':'unavailable','reason':'authentication'},{'status':'unavailable','reason':'host_key'},{'status':'completed','exit_code':255}])
def test_probe_uncertainty_or_security_failure_never_falls_back(source,monkeypatch,result):
    allow_fallback(source);source.execute.return_value=result
    run=Mock();monkeypatch.setattr(t.subprocess,'run',run)
    r=json.loads(t.handle({'command':'echo fixture'}))
    assert not r['fallback'] and r['requested_command_started'] is False
    run.assert_not_called()


def test_timeout_after_remote_dispatch_is_never_replayed(source,monkeypatch):
    allow_fallback(source);source.execute.side_effect=[{'status':'completed','exit_code':0},{'status':'timeout','execution':'unknown'}]
    run=Mock();monkeypatch.setattr(t.subprocess,'run',run)
    r=json.loads(t.handle({'command':'echo fixture'}))
    assert r['execution']=='unknown' and r['execution_device']=='connor-mac'
    assert source.execute.call_count==2
    run.assert_not_called()


def test_fallback_revoked_during_probe(source,monkeypatch):
    allow_fallback(source)
    def revoke(*args):
        source.config.pop('owner_device_fallback');source.cfg.write_text(yaml.safe_dump(source.config))
        return {'status':'unavailable','reason':'network'}
    source.execute.side_effect=revoke
    run=Mock();monkeypatch.setattr(t.subprocess,'run',run)
    with pytest.raises(Denied):t.handle({'command':'echo fixture'})
    run.assert_not_called()


def test_actual_local_process_on_controlled_fallback(source):
    allow_fallback(source)
    source.execute.return_value={'status':'unavailable','reason':'network'}
    r=json.loads(t.handle({'command':'printf MARGOT_FALLBACK_CANARY','timeout':5}))
    assert r['stdout']=='MARGOT_FALLBACK_CANARY'
    assert r['execution_device']=='chris-mac' and r['exit_code']==0
    assert source.execute.call_args.args[1]=='true'

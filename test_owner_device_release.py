import json,os,subprocess
from pathlib import Path
from unittest.mock import Mock,patch
import pytest,yaml
from gateway import owner_device_tools as d
from gateway import josh_board_authority as a
from hermes_constants import set_hermes_home_override,reset_hermes_home_override
from tools.environments.ssh import SSHEnvironment

@pytest.fixture
def owner(tmp_path,monkeypatch):
    root=tmp_path/'profiles';root.mkdir(mode=0o700);home=root/'joshgarcia';home.mkdir(mode=0o700)
    keys=tmp_path/'keys';keys.mkdir(mode=0o700);key=keys/'device';key.write_text('fixture');key.chmod(0o600)
    cfg={'owner_device_enabled':True,'terminal':{'backend':'ssh','ssh_sync_files':False,'ssh_host':'fixture.invalid','ssh_user':'fixture','ssh_key':str(key)}}
    p=home/'config.yaml';p.write_text(yaml.safe_dump(cfg));p.chmod(0o600)
    monkeypatch.setattr(d,'PROFILE_ROOT',root);monkeypatch.setattr(d,'KEY_ROOT',keys)
    enrollment={'owner':'joshgarcia','profile':'joshgarcia','board':'josh-home','fingerprint':'a'*64}
    monkeypatch.setattr(a,'_current',lambda _:enrollment.copy())
    token=set_hermes_home_override(home)
    try:
        with a.execution_scope('joshgarcia','joshgarcia'):yield p,cfg,enrollment
    finally:reset_hermes_home_override(token)

def test_own_authority_and_no_host_override(owner,monkeypatch):
    run=Mock(return_value={'status':'completed'});monkeypatch.setattr(d,'_execute',run)
    assert d.enabled();assert json.loads(d.invoke({'command':'hostname'}))['status']=='completed'
    assert run.call_args.args[0][1]=='fixture.invalid'
    with pytest.raises(ValueError):d.invoke({'command':'hostname','host':'other'})
    assert run.call_count==1

def test_wrong_principal_denied(owner):
    token=a._execution.set(('joshgarcia','connor'))
    try:
        assert not d.enabled()
        with pytest.raises(PermissionError):d.invoke({'command':'hostname'})
    finally:a._execution.reset(token)

def test_revocation_denied(owner,monkeypatch):
    p,cfg,enrollment=owner;enrollment['fingerprint']='b'*64
    run=Mock();monkeypatch.setattr(d,'_execute',run)
    assert not d.enabled()
    with pytest.raises(PermissionError):d.invoke({'command':'hostname'})
    run.assert_not_called()

def test_disable_and_sync_rejected(owner):
    p,cfg,_=owner
    cfg['terminal']['ssh_sync_files']=True;p.write_text(yaml.safe_dump(cfg));assert not d.enabled()
    cfg['terminal']['ssh_sync_files']=False;cfg['owner_device_enabled']=False;p.write_text(yaml.safe_dump(cfg));assert not d.enabled()

def test_changed_authority_withholds_result(owner,monkeypatch):
    p,cfg,_=owner
    def execute(*args):
        cfg['owner_device_enabled']=False;p.write_text(yaml.safe_dump(cfg));return {'stdout':'private'}
    monkeypatch.setattr(d,'_execute',execute)
    with pytest.raises(PermissionError):d.invoke({'command':'hostname'})

def test_delegated_context_denied(owner,monkeypatch):
    import agent.delegation_context as dc
    monkeypatch.setattr(dc,'is_delegated_child_context',lambda:True)
    assert not d.enabled()

def test_offline_then_online_has_no_local_fallback(monkeypatch):
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs['env']))
        if len(calls)==1:return subprocess.CompletedProcess(argv,255)
        kwargs['stdout'].write(b'fixture-remote');return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(d.subprocess,'run',run)
    b=('joshgarcia','fixture.invalid','fixture','/fixture',22,'digest')
    assert d._execute(b,'hostname',10)['status']=='unavailable'
    assert d._execute(b,'hostname',10)['stdout']=='fixture-remote'
    assert all(x[0][0]=='/usr/bin/ssh' and 'fixture@fixture.invalid' in x[0] for x in calls)
    assert set(calls[0][1])=={'PATH','HOME','LANG'}

def test_timeout_never_retries(monkeypatch):
    run=Mock(side_effect=subprocess.TimeoutExpired(['ssh'],1));monkeypatch.setattr(d.subprocess,'run',run)
    assert d._execute(('p','fixture','u','/key',22,'digest'),'echo hi',1)['execution']=='unknown'
    assert run.call_count==1

def test_no_sync_lifecycle(monkeypatch,tmp_path):
    manager=Mock();monkeypatch.setattr('tools.environments.ssh.FileSyncManager',manager)
    monkeypatch.setattr(SSHEnvironment,'_establish_connection',lambda self:None)
    monkeypatch.setattr(SSHEnvironment,'_detect_remote_home',lambda self:'/Users/fixture')
    mkdir=Mock();monkeypatch.setattr(SSHEnvironment,'_ensure_remote_dirs',mkdir)
    monkeypatch.setattr(SSHEnvironment,'init_session',lambda self:None)
    env=SSHEnvironment('fixture.invalid','fixture',sync_files=False)
    env.control_socket=tmp_path/'absent';env._before_execute();env.cleanup()
    manager.assert_not_called();mkdir.assert_not_called()

def test_sync_opt_in_preserved(monkeypatch,tmp_path):
    manager=Mock();monkeypatch.setattr('tools.environments.ssh.FileSyncManager',manager)
    monkeypatch.setattr(SSHEnvironment,'_establish_connection',lambda self:None)
    monkeypatch.setattr(SSHEnvironment,'_detect_remote_home',lambda self:'/Users/fixture')
    monkeypatch.setattr(SSHEnvironment,'_ensure_remote_dirs',lambda self:None)
    monkeypatch.setattr(SSHEnvironment,'init_session',lambda self:None)
    env=SSHEnvironment('fixture.invalid','fixture',sync_files=True);env.control_socket=tmp_path/'absent'
    env._before_execute();env.cleanup()
    manager.return_value.sync.assert_any_call(force=True);manager.return_value.sync_back.assert_called_once()

@pytest.mark.parametrize('error,reason',[('Host key verification failed.','host_key'),('Permission denied (publickey).','authentication'),('No route to host','network')])
def test_connection_failure_reason(monkeypatch,error,reason):
    def run(argv,**kwargs):kwargs['stderr'].write(error.encode());return subprocess.CompletedProcess(argv,255)
    monkeypatch.setattr(d.subprocess,'run',run)
    assert d._execute(('p','host','u','/key',22,'digest'),'true',10)['reason']==reason

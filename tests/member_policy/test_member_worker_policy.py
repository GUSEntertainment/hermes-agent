import json, sqlite3
from pathlib import Path
import pytest
from gateway import member_worker_policy as p
from gateway import app_business_authority as a, gus_worker_authority as g

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(a,'protected_bytes',lambda path,anchor:Path(path).read_bytes())
    monkeypatch.setattr(g,'protected',lambda *args:None)
    monkeypatch.setattr(g,'enrolled_profiles',lambda *args:{'claire','grace'})
    monkeypatch.setattr(a,'current',lambda app,principal,root:{'board':principal+'-home','fingerprint':'verified'})
    for rel,key,rows in [('private/phone-assistant/caller-profiles.json','profiles',[{'profile_key':'connor','board':'connor-home'},{'profile_key':'chris','board':'chris-home'}]),('private/photon-intake/custody-enrollments.json','enrollments',[])]:
        x=tmp_path/rel;x.parent.mkdir(parents=True,exist_ok=True);x.write_text(json.dumps({key:rows}))
    x=tmp_path/'profiles/app-gus-connor/config.yaml';x.parent.mkdir(parents=True);x.write_text('app_business_authority:\n  board: connor-home\n  principal: connor\n')
    db=tmp_path/'kanban/boards/connor-home/kanban.db';db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE tasks(id TEXT, assignee TEXT)');c.execute("INSERT INTO tasks VALUES('t_fixture','claire')")
    return tmp_path,x

def test_member_is_confined_without_tenant_or_realm(setup):
    root,_=setup;policy=p.select('t_fixture','connor-home','claire',root=root)
    assert policy['principal']=='connor'

def test_deleted_binding_fails_closed(setup):
    root,path=setup;path.unlink()
    with pytest.raises(PermissionError):p.select('t_fixture','connor-home','claire',root=root)

def test_revoked_binding_fails_closed(setup,monkeypatch):
    root,_=setup
    def denied(*a,**k):raise PermissionError('revoked')
    monkeypatch.setattr(a,'current',denied)
    with pytest.raises(PermissionError):p.select('t_fixture','connor-home','claire',root=root)

def test_chris_unchanged_without_db(setup):
    root,_=setup;assert p.select('t_owner','chris-home','claire',root=root) is None

def test_reviewer_same_confinement(setup):
    root,_=setup;assert p.select('t_fixture','connor-home','grace',root=root)['worker']=='grace'

def test_non_gus_denied(setup):
    root,_=setup
    with pytest.raises(PermissionError):p.select('t_fixture','connor-home','julia',root=root)

def test_ambiguity_denied(setup):
    root,_=setup;x=root/'private/photon-intake/custody-enrollments.json';x.write_text(json.dumps({'enrollments':[{'owner':'joshgarcia','board':'connor-home','enabled':False}]}))
    with pytest.raises(PermissionError):p.select('t_fixture','connor-home','claire',root=root)

def test_overlay_preserves_budget_authorized_delegation():
    cfg={'model':'gpt-6-astra','delegation':{'max_depth':2,'auto_approve':True,'max_concurrent':3},'budget':{'usd':5},'mcp_servers':{'private':{}},'toolsets':['browser']}
    result=p.effective(cfg,{'version':p.VERSION})
    assert result['budget']==cfg['budget'] and result['model']==cfg['model']
    for key in ('max_depth','auto_approve','max_concurrent'):assert result['delegation'][key]==cfg['delegation'][key]
    assert result['delegation']['inherit_mcp_toolsets'] is False
    assert not result['memory']['memory_enabled'] and not result['memory']['user_profile_enabled']
    assert result['security']['workspace_confinement']['enabled']
    assert result['mcp_servers']=={} and cfg['toolsets']==['browser']

def test_tools_child_and_revocation(monkeypatch):
    from agent import private_worker_mode as pwm,delegation_context as dc
    contract={'member_company_policy':{'version':p.VERSION},'toolsets':['terminal','file','kanban','delegation','worker_skills']}
    monkeypatch.setattr(pwm,'get_authenticated_worker_contract',lambda:contract)
    monkeypatch.setattr(p,'verify',lambda policy:None)
    monkeypatch.setattr(dc,'is_delegated_child_context',lambda:False)
    assert p.allowed_tool('terminal') and p.allowed_tool('delegate_task')
    assert not p.allowed_tool('browser_navigate') and not p.allowed_tool('mcp_private_read')
    monkeypatch.setattr(dc,'is_delegated_child_context',lambda:True)
    assert p.allowed_tool('terminal') and not p.allowed_tool('delegate_task') and not p.allowed_tool('kanban_list_tasks')

def test_actual_fd_common_and_cli_config(tmp_path,monkeypatch):
    import importlib.util,os
    from agent import private_worker_mode as pwm
    from hermes_cli import kanban_db as kb
    source=Path(__file__).parents[1]/'agent/test_private_worker_mode.py'
    spec=importlib.util.spec_from_file_location('fixture_private',source);fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    pwm._reset_private_worker_contract_for_tests()
    conn,db,contract,digest,run=fixture._live_claim(tmp_path,monkeypatch)
    contract['member_company_policy']={'version':p.VERSION}
    contract['toolsets']=list(p.TOOLSETS)
    digest=pwm.contract_digest(contract);encoded=pwm._canonical_bytes(contract).decode()
    conn.execute('UPDATE tasks SET worker_admission_digest=?,worker_admission_contract=? WHERE id=?',(digest,encoded,contract['task']))
    conn.execute('UPDATE task_runs SET worker_admission_digest=?,worker_admission_contract=? WHERE id=?',(digest,encoded,run));conn.commit()
    kb._set_worker_pid(conn,contract['task'],os.getpid());conn.close()
    monkeypatch.setattr(p,'verify',lambda policy:None) # trust-catalog fixture only; durable validation is real
    read,write=os.pipe();os.write(write,json.dumps({'contract':contract,'query':'controlled fixture'}).encode());os.close(write)
    monkeypatch.setenv('HERMES_WORKER_ADMISSION_FD',str(read))
    try:
        from hermes_cli.config import load_config
        config=load_config() # consumes actual pipe and validates durable task/run/PID
        assert config['security']['workspace_confinement']['enabled']
        import cli
        effective=cli.load_cli_config()
        assert effective['memory']['memory_enabled'] is False
        assert effective['memory']['user_profile_enabled'] is False
        assert effective['mcp_servers']=={}
        import model_tools
        assert 'outside current member task admission' in model_tools.handle_function_call('browser_navigate',{})
        assert 'outside current member task admission' in model_tools.handle_function_call('tool_call',{'name':'mcp_private_read','arguments':{}})
        from agent.delegation_context import delegated_child_context
        from tools.workspace_confinement import prepare_sandbox_launch, resolve_confined_path, WorkspaceConfinementError
        import subprocess
        workspace=Path(contract['workspace']['path']);(workspace/'inside.txt').write_text('controlled')
        sibling=tmp_path/'private.txt';sibling.write_text('fixture-private')
        with delegated_child_context('controlled-child'):
            assert p.allowed_tool('terminal') and not p.allowed_tool('delegate_task')
            assert pwm.get_authenticated_worker_contract()['member_company_policy']==contract['member_company_policy']
            assert resolve_confined_path(workspace/'inside.txt',access='read')==workspace/'inside.txt'
            with pytest.raises(WorkspaceConfinementError):resolve_confined_path(sibling,access='read')
            launch=prepare_sandbox_launch('/bin/cat inside.txt',workspace)
            result=subprocess.run(launch.args,cwd=workspace,env=launch.env,capture_output=True,text=True)
            assert result.returncode==0 and result.stdout=='controlled'
    finally:pwm._reset_private_worker_contract_for_tests()

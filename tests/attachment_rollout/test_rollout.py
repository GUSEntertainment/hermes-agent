import importlib.util,json,sys
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('rollout',Path(__file__).with_name('deploy_gateway.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_failed_new_health_restores_and_bootstraps_old(tmp_path,monkeypatch):
 policy=tmp_path/'policy.json';policy.write_bytes(b'before')
 live=tmp_path/'gateway.plist';live.write_bytes(b'before')
 item={'path':str(live),'before_sha':m.sha(b'before'),'after_sha':m.sha(b'after'),'prefix':'launch','mode':0o600}
 monkeypatch.setattr(m,'B',tmp_path);monkeypatch.setattr(m,'H',tmp_path)
 monkeypatch.setattr(m,'P',dict(m.P,changes=[item,dict(item,path=str(policy),prefix='policy')]))
 monkeypatch.setattr(sys,'argv',['deploy_gateway.py','--apply'])
 monkeypatch.setattr(m,'validate',lambda:None);monkeypatch.setattr(m,'idle',lambda:None)
 states=iter([{'gateway_state':'running','pid':10},{'gateway_state':'draining','active_agents':0},{'gateway_state':'running','active_agents':0},{'gateway_state':'draining','active_agents':0}])
 monkeypatch.setattr(m,'query_gateway_control',lambda *a:next(states))
 monkeypatch.setattr(m,'write_drain_request',lambda **kw:(tmp_path/'.drain_request.json').write_text('our drain'))
 monkeypatch.setattr(m,'replace',lambda item,suffix:Path(item['path']).write_bytes(suffix.encode()))
 commands=[];monkeypatch.setattr(m.subprocess,'run',lambda args,**kw:commands.append(args))
 waits=[];monkeypatch.setattr(m,'wait_gone',lambda pid:waits.append(pid))
 monkeypatch.setattr(m,'service_pid',lambda:20)
 starts=[]
 def start(pid):
  starts.append(pid)
  if len(starts)==1:raise RuntimeError('new API failed')
  return 30
 monkeypatch.setattr(m,'bootstrap_health',start)
 with pytest.raises(RuntimeError,match='new API failed'):m.main()
 assert live.read_bytes()==b'before' and policy.read_bytes()==b'before'
 assert starts==[10,10] and waits==[10,20,10]
 assert len([x for x in commands if x[1]=='bootout'])==2
 receipt=json.loads((tmp_path/'gateway-deployment.json').read_text())
 assert receipt['state']=='rolled_back_api_healthy' and receipt['rollback_pid']==30
 assert not (tmp_path/'.drain_request.json').exists()

def test_new_bootstrap_only_after_old_pid_exit(tmp_path,monkeypatch):
 # A hung old PID must prevent bootstrap and leave explicit rollback failure.
 live=tmp_path/'gateway.plist';live.write_bytes(b'before');item={'path':str(live),'before_sha':m.sha(b'before'),'after_sha':m.sha(b'after'),'prefix':'launch','mode':0o600}
 monkeypatch.setattr(m,'B',tmp_path);monkeypatch.setattr(m,'H',tmp_path);monkeypatch.setattr(m,'P',dict(m.P,changes=[item]))
 monkeypatch.setattr(sys,'argv',['deploy_gateway.py','--apply']);monkeypatch.setattr(m,'validate',lambda:None);monkeypatch.setattr(m,'idle',lambda:None)
 states=iter([{'gateway_state':'running','pid':10},{'gateway_state':'draining','active_agents':0},{'gateway_state':'running','active_agents':0},{'gateway_state':'draining','active_agents':0}]);monkeypatch.setattr(m,'query_gateway_control',lambda *a:next(states))
 monkeypatch.setattr(m,'write_drain_request',lambda **kw:(tmp_path/'.drain_request.json').write_text('ours'))
 monkeypatch.setattr(m,'replace',lambda item,suffix:Path(item['path']).write_bytes(suffix.encode()));monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:None)
 def noexit(pid):raise RuntimeError('old still running')
 monkeypatch.setattr(m,'wait_gone',noexit);monkeypatch.setattr(m,'service_pid',lambda:10)
 starts=[];monkeypatch.setattr(m,'bootstrap_health',lambda pid:starts.append(pid))
 with pytest.raises(RuntimeError):m.main()
 assert not starts
 assert json.loads((tmp_path/'gateway-deployment.json').read_text())['state']=='rollback_failed_requires_attention'

def test_actual_default_board_idle_gate(tmp_path,monkeypatch):
 import sqlite3
 monkeypatch.setattr(m,'H',tmp_path)
 path=tmp_path/'kanban/kanban.db';path.parent.mkdir()
 with sqlite3.connect(path) as c:
  c.execute('CREATE TABLE tasks(status TEXT,current_run_id INTEGER,worker_pid INTEGER)')
  c.execute("INSERT INTO tasks VALUES ('running',1,100)")
 with pytest.raises(RuntimeError,match='Kanban worker'):m.idle()

def test_josh_worker_idle_gate(tmp_path,monkeypatch):
 import sqlite3
 monkeypatch.setattr(m,'H',tmp_path)
 path=tmp_path/'kanban/boards/joshgarcia-personal/kanban.db';path.parent.mkdir(parents=True)
 with sqlite3.connect(path) as c:
  c.execute('CREATE TABLE tasks(status TEXT,current_run_id INTEGER,worker_pid INTEGER)')
  c.execute("INSERT INTO tasks VALUES ('running',1,100)")
 with pytest.raises(RuntimeError,match='Kanban worker'):m.idle()

def test_new_work_blocks_disruptive_rollback(tmp_path,monkeypatch):
 live=tmp_path/'gateway.plist';live.write_bytes(b'before');item={'path':str(live),'before_sha':m.sha(b'before'),'after_sha':m.sha(b'after'),'prefix':'launch','mode':0o600}
 monkeypatch.setattr(m,'B',tmp_path);monkeypatch.setattr(m,'H',tmp_path);monkeypatch.setattr(m,'P',dict(m.P,changes=[item]))
 monkeypatch.setattr(sys,'argv',['deploy_gateway.py','--apply']);monkeypatch.setattr(m,'validate',lambda:None);monkeypatch.setattr(m,'idle',lambda:None)
 states=iter([{'gateway_state':'running','pid':10},{'gateway_state':'draining','active_agents':0},{'gateway_state':'running','active_agents':1}])
 monkeypatch.setattr(m,'query_gateway_control',lambda *a:next(states))
 monkeypatch.setattr(m,'write_drain_request',lambda **kw:(tmp_path/'.drain_request.json').write_text('ours'))
 monkeypatch.setattr(m,'replace',lambda item,suffix:Path(item['path']).write_bytes(suffix.encode()))
 commands=[];monkeypatch.setattr(m.subprocess,'run',lambda args,**kw:commands.append(args))
 monkeypatch.setattr(m,'wait_gone',lambda pid:None);monkeypatch.setattr(m,'service_pid',lambda:20)
 def failed_health(pid):raise RuntimeError('new API health unavailable')
 monkeypatch.setattr(m,'bootstrap_health',failed_health)
 with pytest.raises(RuntimeError):m.main()
 assert len([x for x in commands if x[1]=='bootout'])==1
 assert live.read_bytes()==b'after'
 receipt=json.loads((tmp_path/'gateway-deployment.json').read_text())
 assert receipt['state']=='rollback_failed_requires_attention'
 assert 'newly active gateway work' in receipt['rollback_reason']

def test_zero_byte_default_placeholder_is_skipped(tmp_path,monkeypatch):
 import sqlite3
 monkeypatch.setattr(m,'H',tmp_path)
 path=tmp_path/'kanban/kanban.db';path.parent.mkdir();path.touch()
 phone=tmp_path/'private/phone-assistant/actions.sqlite';phone.parent.mkdir(parents=True)
 with sqlite3.connect(phone) as c:
  c.execute('CREATE TABLE phone_turn_jobs(state TEXT,expires_at INTEGER,lease_expires_at INTEGER)')
  c.execute('CREATE TABLE phone_calls(terminal_status TEXT,revoked_at INTEGER,expires_at INTEGER)')
 m.idle()
 assert path.stat().st_size==0

def test_nonempty_missing_tasks_table_fails_closed(tmp_path,monkeypatch):
 import sqlite3
 monkeypatch.setattr(m,'H',tmp_path)
 path=tmp_path/'kanban/kanban.db';path.parent.mkdir()
 with sqlite3.connect(path) as c:c.execute('CREATE TABLE unrelated(value TEXT)')
 with pytest.raises(RuntimeError,match='Nonempty native board has no tasks table'):m.idle()


def test_final_source_approval_is_required(tmp_path,monkeypatch):
 monkeypatch.setattr(m,'verify_imports',lambda cfg:{})
 monkeypatch.setattr(m.plistlib,'loads',lambda raw:{})
 private=tmp_path/'private';private.mkdir();(private/'gateway.after').write_text('{}')
 monkeypatch.setattr(m,'PRIVATE',private)
 monkeypatch.setattr(m,'B',tmp_path)
 monkeypatch.setattr(m,'P',dict(m.P,final_manifest='final.json'))
 (tmp_path/'final.json').write_text(json.dumps({'approved':False,'runtime':m.P['runtime']}))
 with pytest.raises(RuntimeError,match='not approved'):m.validate()


def test_wrong_pythonpath_fails_before_import(monkeypatch):
 config={'ProgramArguments':['python'],'EnvironmentVariables':{'PYTHONPATH':'old','VIRTUAL_ENV':'old'}}
 with pytest.raises(RuntimeError,match='environment'):m.verify_imports(config)

def test_import_probe_uses_exact_launch_env(monkeypatch):
 from types import SimpleNamespace
 config={'ProgramArguments':['python'],'EnvironmentVariables':{'PYTHONPATH':m.P['runtime'],'VIRTUAL_ENV':m.P['runtime']+'/.venv'}}
 def run(args,**kw):
  assert '-P' in args and kw['env']['PYTHONPATH']==m.P['runtime']
  return SimpleNamespace(stdout=json.dumps({'tools.kanban_tools':m.P['runtime']+'/tools/kanban_tools.py'}))
 monkeypatch.setattr(m.subprocess,'run',run)
 assert m.verify_imports(config)

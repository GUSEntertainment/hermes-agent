"""Reviewed main-gateway-only cutover. Default is validation, --apply mutates."""
import argparse,hashlib,json,os,plistlib,re,socket,sqlite3,subprocess,sys,time,urllib.request
from pathlib import Path
B=Path(__file__).resolve().parent
P=json.loads((B/'gateway-plan.json').read_text())
H=Path('/Users/Chris/.hermes');PRIVATE=B/'private'
DOMAIN=f'gui/{os.getuid()}';LABEL='ai.hermes.gateway';TARGET=DOMAIN+'/'+LABEL
PLIST=Path('/Users/Chris/Library/LaunchAgents')/(LABEL+'.plist')
sha=lambda data:hashlib.sha256(data).hexdigest()
sys.path.insert(0,P['base'])
from gateway.control_socket import query_gateway_control
from gateway.drain_control import write_drain_request

def idle():
    # All native API work, including Josh's independent gateway.
    homes=[H,*sorted((H/'profiles').glob('*'))]
    for home in homes:
        path=home/'runs_idempotency.db'
        if not path.is_file():continue
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
            if any(json.loads(x[0]).get('status') not in {'completed','failed','cancelled'} for x in c.execute('select status_json from run_idempotency')):
                raise RuntimeError('Native API work is active; no cutover')
    # Native dispatcher workers are independent of the gateway active-agent count.
    boards={H/'kanban.db',H/'kanban/kanban.db',*H.glob('kanban/boards/*/kanban.db'),*H.glob('profiles/*/kanban.db'),*H.glob('profiles/*/kanban/boards/*/kanban.db')}
    for path in boards:
        if not path.is_file():continue
        # Observed legacy default placeholder is empty, not an initialized board.
        # Nonempty malformed boards fail closed rather than hiding active work.
        if path.stat().st_size == 0:continue
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                raise RuntimeError('Nonempty native board has no tasks table; idle unknown')
            if c.execute("select 1 from tasks where status='running' or current_run_id is not null or worker_pid is not null limit 1").fetchone():
                raise RuntimeError('Native Kanban worker is active; no cutover')
    path=H/'private/phone-assistant/actions.sqlite'
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
        now=time.time()
        if c.execute("select 1 from phone_turn_jobs where state not in ('completed','failed','cancelled') and (expires_at>? or coalesce(lease_expires_at,0)>?) limit 1",(now,now)).fetchone():raise RuntimeError('Phone work active')
        if c.execute('select 1 from phone_calls where terminal_status is null and revoked_at is null and expires_at>? limit 1',(int(now),)).fetchone():raise RuntimeError('Call active')

def verify_imports(config):
    env=os.environ.copy();env.update(config.get('EnvironmentVariables',{}))
    if env.get('PYTHONPATH')!=P['runtime'] or env.get('VIRTUAL_ENV')!=P['runtime']+'/.venv':
        raise RuntimeError('Runtime environment does not match reviewed source')
    code="import json,hermes_cli.main,tools.kanban_tools,tools.task_attachment_read,gateway.native_task_routing,gateway.member_worker_policy;mods=[hermes_cli.main,tools.kanban_tools,tools.task_attachment_read,gateway.native_task_routing,gateway.member_worker_policy];print(json.dumps({m.__name__:m.__file__ for m in mods}))"
    result=subprocess.run([config['ProgramArguments'][0],'-P','-c',code],env=env,cwd=config.get('WorkingDirectory'),capture_output=True,text=True,timeout=15,check=True)
    paths=json.loads(result.stdout)
    if not all(Path(value).is_relative_to(P['runtime']) for value in paths.values()):
        raise RuntimeError('Effective import source differs from reviewed runtime')
    return paths

def validate():
    verify_imports(plistlib.loads((PRIVATE/'gateway.after').read_bytes()))
    final=json.loads((B/P['final_manifest']).read_text())
    if final.get('approved') is not True or final.get('runtime')!=P['runtime']:
        raise RuntimeError('Final combined source review is not approved')
    if not isinstance(final.get('sources'),dict) or not final['sources']:
        raise RuntimeError('Final source manifest missing')
    for name,digest in P['sources'].items():
        if final['sources'].get(name)!=digest:raise RuntimeError('Reviewed routing source pin changed')
    for name,digest in final['sources'].items():
        if sha((Path(P['runtime'])/name).read_bytes())!=digest:raise RuntimeError('Final combined source changed')
    for name,digest in P['preserve_consumers'].items():
        if sha(Path(name).read_bytes())!=digest:raise RuntimeError('Preserved phone consumer changed')
    for name,digest in P['sources'].items():
        if sha((Path(P['runtime'])/name).read_bytes())!=digest:raise RuntimeError('Runtime source changed')
    for name,digest in P['processor_sources'].items():
        if sha((Path(P['processors'])/name).read_bytes())!=digest:raise RuntimeError('Phone processor source changed')
    for item in P['changes']:
        if sha(Path(item['path']).read_bytes())!=item['before_sha']:raise RuntimeError('Live config CAS mismatch')
        for suffix in ['before','after']:
            q=PRIVATE/(item['prefix']+'.'+suffix)
            if q.stat().st_mode&0o777!=0o600:raise RuntimeError('Private candidate permissions changed')
            if sha(q.read_bytes())!=item[suffix+'_sha']:raise RuntimeError('Private candidate changed')

def replace(item,suffix):
    p=Path(item['path']);tmp=p.with_name(p.name+'.v34-stage')
    with open(tmp,'xb') as f:
        os.chmod(tmp,0o600);f.write((PRIVATE/(item['prefix']+'.'+suffix)).read_bytes());f.flush();os.fsync(f.fileno())
    os.chmod(tmp,item['mode']);os.replace(tmp,p)

def service_pid():
    r=subprocess.run(['launchctl','print',TARGET],capture_output=True,text=True,timeout=15)
    m=re.search(r'^\s*pid = (\d+)',r.stdout,re.M)
    return int(m.group(1)) if m else None

def wait_gone(pid):
    if not pid:return
    deadline=time.monotonic()+P['old_pid_exit_timeout_seconds']
    while time.monotonic()<deadline:
        try:os.kill(pid,0)
        except ProcessLookupError:return
        time.sleep(1)
    raise RuntimeError('Old gateway process did not exit')

def wait_port():
    deadline=time.monotonic()+90
    while True:
        probe=socket.socket()
        try:probe.bind(('127.0.0.1',8642));return
        except OSError:
            if time.monotonic()>=deadline:raise RuntimeError('API listener address remains unavailable')
            time.sleep(1)
        finally:probe.close()

def bootstrap_health(previous_pid):
    wait_port()
    subprocess.run(['launchctl','bootstrap',DOMAIN,str(PLIST)],check=True,capture_output=True,timeout=30)
    deadline=time.monotonic()+P['startup_timeout_seconds']
    while time.monotonic()<deadline:
        pid=service_pid()
        if pid and pid!=previous_pid:
            try:
                status=query_gateway_control(H,'status')
                with urllib.request.urlopen(P['health_url'],timeout=2) as r:health=json.load(r)
                if status.get('gateway_state')=='running' and int(status.get('pid') or 0)==pid and health.get('status')=='ok':return pid
            except Exception:pass
        time.sleep(1)
    raise RuntimeError('New PID and API health did not become ready')

def main():
    args=argparse.ArgumentParser();args.add_argument('--apply',action='store_true');a=args.parse_args()
    validate()
    if not a.apply:
        print(json.dumps({'state':'validated_not_applied','runtime':P['runtime'],'idle_gate':'required at apply','source_count':len(P['sources'])}));return
    receipt={'state':'preflight','messages_sent':0,'calls_placed':0,'restarted_services':[]}
    def save(): (B/'gateway-deployment.json').write_text(json.dumps(receipt,indent=2)+'\n')
    marker=H/'.drain_request.json'
    if marker.exists():raise RuntimeError('Existing drain belongs to another controller')
    idle();status=query_gateway_control(H,'status')
    if status['gateway_state']!='running':raise RuntimeError('Gateway is not running')
    old_pid=int(status['pid']);receipt['previous_pid']=old_pid;save()
    write_drain_request(principal='codex-reviewed-v34-cutover',suppress_notification=True,home=H)
    marker_bytes=marker.read_bytes();applied=[];stopped=False
    def clear_own_marker():
        if marker.exists() and marker.read_bytes()==marker_bytes:marker.unlink()
    try:
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            s=query_gateway_control(H,'status')
            if s.get('gateway_state')=='draining' and s.get('active_agents')==0:break
            time.sleep(1)
        else:raise RuntimeError('Gateway busy after drain')
        idle();validate()
        for item in P['changes']:replace(item,'after');applied.append(item)
        # Recheck independent dispatchers immediately before stopping the gateway.
        idle()
        stopped=True  # treat an ambiguous bootout failure as needing recovery
        subprocess.run(['launchctl','bootout',TARGET],check=True,capture_output=True,timeout=30)
        wait_gone(old_pid);clear_own_marker()
        pid=bootstrap_health(old_pid)
        for item in P['changes']:
            if sha(Path(item['path']).read_bytes())!=item['after_sha']:raise RuntimeError('Post-health live config CAS mismatch')
        receipt['import_resolution']=verify_imports(plistlib.loads(PLIST.read_bytes()))
        receipt.update(state='new_pid_api_healthy_pending_functional_acceptance',new_pid=pid,runtime=P['runtime'],restarted_services=[LABEL]);save()
        print(json.dumps(receipt))
    except BaseException as exc:
        receipt.update(state='failed',error_type=type(exc).__name__)
        try:
            rollback_pid=service_pid() if stopped else None
            if stopped and rollback_pid and rollback_pid != old_pid:
                # New arrivals must never be interrupted by automatic rollback.
                idle()
                current=query_gateway_control(H,'status')
                if current.get('active_agents') != 0:
                    raise RuntimeError('Rollback blocked by newly active gateway work')
                if marker.exists() and marker.read_bytes()!=marker_bytes:
                    raise RuntimeError('Rollback drain belongs to another controller')
                write_drain_request(principal='codex-reviewed-v34-rollback',suppress_notification=True,home=H)
                marker_bytes=marker.read_bytes()
                deadline=time.monotonic()+45
                while time.monotonic()<deadline:
                    current=query_gateway_control(H,'status')
                    if current.get('gateway_state')=='draining' and current.get('active_agents')==0:break
                    time.sleep(1)
                else:raise RuntimeError('Rollback cannot safely drain new gateway')
                idle()
            for item in reversed(applied):
                if sha(Path(item['path']).read_bytes())!=item['after_sha']:raise RuntimeError('Rollback CAS conflict')
                replace(item,'before')
            if stopped:
                new_pid=service_pid()
                subprocess.run(['launchctl','bootout',TARGET],capture_output=True,timeout=30)
                wait_gone(new_pid);wait_gone(old_pid)
            clear_own_marker()
            if stopped:
                receipt['rollback_pid']=bootstrap_health(old_pid)
                receipt['state']='rolled_back_api_healthy'
            else:receipt['state']='aborted_previous_service_preserved'
        except BaseException as rollback_exc:
            receipt.update(state='rollback_failed_requires_attention',rollback_error_type=type(rollback_exc).__name__,rollback_reason=str(rollback_exc)[:180])
        save();raise
    finally:clear_own_marker()

if __name__=='__main__':main()

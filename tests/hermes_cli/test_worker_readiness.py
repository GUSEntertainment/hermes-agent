from __future__ import annotations
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import worker_readiness as wr


def contract():
    return {"goal_id":"synthetic-goal", "objective":"test readiness", "owner_profile":"owner",
        "owner_role_alias":"engineering", "runtime_profile":"worker", "security_realm":"company",
        "router_mode":"single", "risk_class":"low", "capability_bundle":"engineering",
        "worker_budget":{"producers":1,"reviewers":0,"extras":0}, "targets":[],
        "acceptance_test":{"expected":"pass"},"rollback":{"procedure_ref":"test"},
        "callback":{"source_surface":"test","return_surface":"test","report_policy":"on_demand"},
        "approval_gates":["publish"], "delegation_roles":{"maker":"worker","reviewer":None}}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home=tmp_path/'.hermes'
    home.mkdir()
    home.joinpath('config.yaml').write_text('kanban:\n  worker_readiness:\n    enabled: true\n')
    for profile in ('worker','owner'):
        p=home/'profiles'/profile
        p.mkdir(parents=True)
        p.joinpath('config.yaml').write_text('model:\n  provider: anthropic\n  default: test-model\nplatform_toolsets:\n  cli: [file, kanban]\nsecurity:\n  workspace_confinement:\n    enabled: true\n')
        skill=p/'skills'/'sdlc-review';skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: sdlc-review\ndescription: Review.\n---\nReview.')
    monkeypatch.setenv('HERMES_HOME',str(home))
    # Installation authority is isolated explicitly; HOME alone is insufficient.
    import sqlite3
    from gateway import member_worker_policy as member_policy
    from gateway import private_action_worker_policy as private_policy
    from gateway import owner_delegation
    monkeypatch.setattr(member_policy, 'ROOT', home)
    monkeypatch.setattr(private_policy, 'ROOT', home)
    monkeypatch.setattr(owner_delegation, 'ROOT', home)
    original_select = member_policy.select
    monkeypatch.setattr(member_policy, 'select', lambda task, board, worker, **kw:
                        original_select(task, board, worker, root=home))
    slug = 'readiness-fixture'
    database = home / 'kanban' / 'boards' / slug / 'kanban.db'
    database.parent.mkdir(parents=True)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', slug)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(database))
    for relative, payload in (
        ('private/phone-assistant/caller-profiles.json', {'profiles': [{'profile_key': 'chris', 'board': slug}]}),
        ('private/photon-intake/custody-enrollments.json', {'enrollments': []}),
    ):
        path = home / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload)); path.chmod(0o600)
    with sqlite3.connect(home / 'private/phone-assistant/actions.sqlite') as actions:
        actions.execute('CREATE TABLE phone_task_links(task_id TEXT,board TEXT,authority_realm TEXT)')
    monkeypatch.setattr(kb.Path,'home',lambda:tmp_path)
    monkeypatch.setattr(kb,'_memory_pressure_level',lambda *a,**kw:'unknown')
    kb.init_db();conn=kb.connect()
    yield conn,home
    conn.close()


def declared(**kw):
    return '```worker-requirements\n'+json.dumps(kw)+'\n```'


@pytest.mark.parametrize('kind', ['preparation', 'direct', 'review'])
def test_request_budget_recovery_keeps_same_task_and_review(board, monkeypatch, kind):
    from hermes_cli import kanban_completion as completion
    conn, _ = board
    caps = {**completion.DEFAULTS, 'task_budgets': {}}
    monkeypatch.setattr(completion, 'policy', lambda: caps)
    root = kb.create_task(conn, title='Local document revision', assignee='worker',
                          tenant='company-topic', owner_cell_contract=contract())
    tid = root
    if kind == 'preparation':
        assert wr.queue_preparation(conn, kb.get_task(conn, root), 'ready',
                                    {'issues': [{'code': 'missing_input', 'resource': 'source.txt'}]})
        tid = conn.execute('SELECT parent_id FROM task_links WHERE child_id=?', (root,)).fetchone()[0]
    else:
        caps['task_budgets'][tid] = {}
    if kind == 'review':
        from hermes_cli import owner_cell as oc
        with kb.write_txn(conn):
            g = dict(oc.goal_projection(conn, tid)); ct = json.loads(g['contract_json'])
            ct.update(router_mode='reviewed', required_reviewer_profile='owner',
                      owner_acceptance={'mode':'accountable_owner'},
                      delegation_roles={'maker': 'worker', 'reviewer': 'owner'},
                      worker_budget={'producers': 1, 'reviewers': 1, 'extras': 0})
            oc.validate_goal_contract(ct)
            conn.execute("UPDATE owner_cell_goals SET contract_json=?, phase='review', runtime_profile='owner', "
                         "router_mode='reviewed', required_reviewer_profile='owner', reviewer_profile='owner', "
                         "review_state_version=1,review_artifact_ref='artifact.pdf',review_artifact_digest=? WHERE task_id=?",
                         (json.dumps(ct), 'a'*64, tid))
            conn.execute("UPDATE tasks SET assignee='owner',runtime_profile='owner',reviewer_profile='owner',router_mode='reviewed' WHERE id=?", (tid,))
    before = dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone())
    task = kb.get_task(conn, tid)
    completion.record_admission(conn, tid, completion.task_limits(caps, task))
    with kb.write_txn(conn):
        run = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,error) "
                           "VALUES (?,?,'gave_up',1,2,'gave_up','kanban_request_token_budget')", (tid,task.assignee)).lastrowid
        conn.execute("UPDATE tasks SET status='blocked',consecutive_failures=1,last_failure_error='kanban_request_token_budget' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'gave_up', {'completion_budget_reason':'kanban_request_token_budget',
            'retry_status':'review' if kind == 'review' else 'ready'}, run_id=run)
    assert completion.recover_request_budget_failures(conn) == [tid]
    assert completion.recover_request_budget_failures(conn) == []
    assert kb.get_task(conn, tid).status == ('review' if kind == 'review' else 'ready')
    assert kb.get_task(conn, tid).consecutive_failures == 1
    assert dict(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone()) == before
    assert 'Bounded context recovery' in kb.build_worker_context(conn, tid)
    if kind == 'preparation':
        assert kb.get_task(conn, root).status == 'todo'
        spawned = []
        def spawn(task, workspace, admission_contract=None, **kw):
            spawned.append(task.id)
        dispatched = kb.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid], conn.execute("SELECT worker_admission_contract FROM tasks WHERE id=?",(tid,)).fetchone()[0]
        assert kb.complete_task(conn, tid, summary='Sources staged and verified', metadata={'readiness_repair':{
            'requirements':{'toolsets':['file']}, 'evidence':'Verified bounded local source content.'}})
        assert kb.get_task(conn, root).status == 'ready'
        kb.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid, root]
    else:
        # A second identical failure cannot manufacture a new recovery allowance.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            kb._append_event(conn, tid, 'gave_up', {'completion_budget_reason':'kanban_request_token_budget'}, run_id=run)
        assert completion.recover_request_budget_failures(conn) == []


@pytest.mark.parametrize('hold', ['manual', 'needs_input', 'budget_spent'])
def test_request_budget_recovery_preserves_holds_and_admission_limits(board, monkeypatch, hold):
    from hermes_cli import kanban_completion as completion
    conn, _ = board
    tid = kb.create_task(conn, title='Do authorized work', assignee='worker', owner_cell_contract=contract())
    caps = {**completion.DEFAULTS, 'task_budgets': {tid: {}}}
    monkeypatch.setattr(completion, 'policy', lambda: caps)
    completion.record_admission(conn, tid, completion.task_limits(caps, kb.get_task(conn, tid)))
    if hold == 'budget_spent':
        completion.record_admission(conn, tid, completion.task_limits(caps, kb.get_task(conn, tid)))
    with kb.write_txn(conn):
        run = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,error) "
            "VALUES (?,'worker','gave_up',1,2,'gave_up','kanban_request_token_budget')", (tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='blocked',last_failure_error='kanban_request_token_budget' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'gave_up', {'completion_budget_reason':'kanban_request_token_budget'},run_id=run)
        if hold == 'manual': kb._append_event(conn, tid, 'blocked', {'reason':'Explicit owner hold'})
        if hold == 'needs_input': conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
    assert completion.recover_request_budget_failures(conn) == []
    assert kb.get_task(conn, tid).status == 'blocked'


def test_inference_catches_existing_metricool_cards_and_offline_phase_can_narrow():
    assert wr.requirements('Schedule through Metricool','')['mcp']=={'metricool':[]}
    req=wr.requirements('Metricool code review',declared(commands=['python3']))
    assert req['mcp']=={} and req['commands']==['python3']


@pytest.mark.parametrize('body', [
    'Use only the supplied files. No Metricool/Mission Control writes or publishing in this phase.',
    'No external sends, publish/schedule/Metricool/public links.',
])
def test_forbidden_service_mentions_do_not_create_mcp_requirements(body):
    req = wr.requirements('Prepare local business analysis', body)
    assert req['source'] == 'inferred'
    assert req['mcp'] == {}


def test_service_not_required_suffix_does_not_create_mcp_requirement():
    req = wr.requirements('Offline audit', 'Metricool is not required for this phase.')
    assert req['mcp'] == {}


def test_affirmative_service_mentions_still_create_mcp_requirements():
    metricool = wr.requirements('Schedule through Metricool', '')
    mission_control = wr.requirements('Mission Control handoff', 'Check Mission Control status before final.')
    assert metricool['mcp'] == {'metricool': []}
    assert mission_control['mcp'] == {'mission_control': []}


def test_ambiguous_nonnegative_service_mentions_keep_legacy_mcp_check():
    req = wr.requirements('Later service coordination', 'Coordinate Metricool account access in a later phase.')
    assert req['mcp'] == {'metricool': []}


def test_bare_documented_service_in_code_style_keeps_legacy_mcp_check():
    req = wr.requirements('Service inventory', 'Documented service: `Metricool`.')
    assert req['mcp'] == {'metricool': []}


@pytest.mark.parametrize('body', [
    'No Metricool writes. Read Metricool status before final. No Mission Control writes.',
    'Read Metricool status but do not write or publish anything.',
    'No publishing in Metricool, but read Metricool status.',
])
def test_mixed_forbidden_and_affirmative_service_mentions_keep_positive_need(body):
    req = wr.requirements('Read-only publishing audit', body)
    assert req['mcp'] == {'metricool': []}


def test_forbidden_service_heading_applies_to_all_bullets_until_next_heading():
    req = wr.requirements(
        'Scoped service audit',
        'Forbidden services:\n- Metricool writes\n- Mission Control writes',
    )
    assert req['mcp'] == {}


def test_service_heading_and_bullet_scope_respects_forbidden_and_required_sections():
    req = wr.requirements(
        'Scoped service audit',
        'Forbidden services:\n- Metricool writes\nRequired services:\n- Mission Control status read',
    )
    assert req['mcp'] == {'mission_control': []}


def test_declared_mcp_requirements_remain_authoritative_over_service_prose():
    req = wr.requirements(
        'Metricool and Mission Control production run',
        'Use Metricool and Mission Control now.\n\n' + declared(mcp={}),
    )
    assert req['source'] == 'declared'
    assert req['mcp'] == {}


def test_inferred_missing_required_input_is_checked(tmp_path):
    missing=tmp_path/'shoot-brief.md'
    req=wr.requirements('Prepare the shoot plan',f'Read `{missing}` before starting. This input is required.')
    assert req['source']=='inferred'
    assert req['read_paths']==[str(missing)]
    report=wr.probe({},req,str(tmp_path),['file'])
    assert {'code':'missing_input','resource':str(missing)} in report['issues']


def test_inferred_relative_input_path_is_checked(tmp_path):
    req=wr.requirements('Prepare transform','Load input file `fixtures/raw.csv` before starting.')
    assert req['read_paths']==['fixtures/raw.csv']
    report=wr.probe({},req,str(tmp_path),['file'])
    assert report['issues']==[{'code':'missing_input','resource':'fixtures/raw.csv'}]


def test_direct_read_intent_wins_over_later_output_discussion(tmp_path):
    req=wr.requirements('Prepare transform','Read required `input.txt` to create the output.')
    assert req['read_paths']==['input.txt']
    assert req['write_paths']==[]
    report=wr.probe({},req,str(tmp_path),['file'])
    assert report['issues']==[{'code':'missing_input','resource':'input.txt'}]


def test_inferred_output_path_is_not_missing_input(tmp_path):
    output=tmp_path/'reports'/'shoot-plan.md'
    output.parent.mkdir()
    req=wr.requirements('Create deliverable',f'Write the final output to `{output}`.')
    assert req['read_paths']==[]
    assert req['write_paths']==[str(output)]
    report=wr.probe({},req,str(tmp_path),['file'])
    assert report['issues']==[]
    assert f'write_paths:{output}' in report['checked']


def test_ambiguous_code_path_mentions_do_not_become_requirements():
    req=wr.requirements('Review implementation','The patch changes `hermes_cli/worker_readiness.py` and runs `python scripts/check.py`.')
    assert req['read_paths']==[]
    assert req['write_paths']==[]


def test_copy_move_source_mentions_are_not_writable_by_implication():
    copy_req=wr.requirements('Prepare copy','Copy `input.txt` to create the output.')
    move_req=wr.requirements('Prepare move','Move `source.csv` into the staged deliverable folder.')
    assert copy_req['read_paths']==[]
    assert copy_req['write_paths']==[]
    assert move_req['read_paths']==[]
    assert move_req['write_paths']==[]


def test_declared_requirements_override_inferred_text(tmp_path):
    missing=tmp_path/'shoot-brief.md'
    req=wr.requirements('Prepare shoot',f'Read `{missing}` first.\n\n'+declared(toolsets=['file']))
    assert req['source']=='declared'
    assert req['toolsets']==['file']
    assert req['read_paths']==[]


@pytest.mark.parametrize('body',[declared(commands=['python;rm']),declared(unknown=[]),declared(mcp={'x':'bad'}),declared(terminal_network='true'),declared()+ '\n'+declared()])
def test_malformed_requirements_fail_closed(body):
    with pytest.raises((ValueError,TypeError)):
        wr.requirements('task',body)


def test_relative_command_path_is_checked_inside_workspace(tmp_path):
    root = tmp_path / 'workspace'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    runner = scripts / 'run_tests.sh'
    runner.write_text('#!/bin/sh\nexit 0\n')
    runner.chmod(0o755)
    req = wr.requirements('Verify repair', declared(commands=['scripts/run_tests.sh']))
    report = wr.probe({'security': {'workspace_confinement': {'enabled': True}}}, req, str(root), ['file'])
    assert report['issues'] == []
    assert 'command:scripts/run_tests.sh' in report['checked']


def test_relative_command_path_missing_or_outside_workspace_fails_closed(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside' / 'run_tests.sh'
    outside.parent.mkdir()
    outside.write_text('#!/bin/sh\nexit 0\n')
    outside.chmod(0o755)
    missing = wr.probe(
        {'security': {'workspace_confinement': {'enabled': True}}},
        wr.requirements('Verify repair', declared(commands=['scripts/run_tests.sh'])),
        str(root),
        ['file'],
    )
    assert missing['issues'] == [{'code': 'missing_command', 'resource': 'scripts/run_tests.sh'}]
    escaped = wr.probe(
        {'security': {'workspace_confinement': {'enabled': True}}},
        wr.requirements('Verify repair', declared(commands=['../outside/run_tests.sh'])),
        str(root),
        ['file'],
    )
    assert escaped['issues'] == [{'code': 'command_outside_workspace', 'resource': '../outside/run_tests.sh'}]


def test_paths_and_network_are_checked_against_confinement(tmp_path):
    root=tmp_path/'workspace';root.mkdir()
    outside=tmp_path/'secret.txt';outside.write_text('not disclosed')
    (root/'input.txt').write_text('approved input')
    (root/'escape').symlink_to(outside)
    req=wr.requirements('task',declared(read_paths=['input.txt','escape','missing.txt'],terminal_network=True,localhost_server=True))
    report=wr.probe({'security':{'workspace_confinement':{'enabled':True}}},req,str(root),['file'])
    assert 'read_paths:input.txt' in report['checked']
    assert {r['code'] for r in report['issues']}=={'path_outside_workspace','missing_input','terminal_network_denied','localhost_listen_denied'}
    assert 'not disclosed' not in json.dumps(report)


def test_real_localhost_probe_calls_listen(tmp_path):
    report=wr.probe({},wr.requirements('local',declared(localhost_server=True)),str(tmp_path),[])
    assert report['issues']==[] and report['checked']==['localhost:bind+listen']


def test_disabled_mcp_is_not_connected(tmp_path,monkeypatch):
    from tools import mcp_tool
    monkeypatch.setattr(mcp_tool,'register_mcp_servers',lambda *_:pytest.fail('disabled server contacted'))
    report=wr.probe({'mcp_servers':{'metricool':{'enabled':False}}},wr.requirements('Metricool',''),str(tmp_path),['metricool'])
    assert report['issues']==[{'code':'missing_mcp_server','resource':'metricool'}]


@pytest.mark.parametrize('lane',['ready','review'])
def test_dispatch_prepares_then_rechecks_and_resumes_original(board,lane):
    conn,home=board
    tid=kb.create_task(conn,title='Metricool task',assignee='worker',tenant='private-business-topic',owner_cell_contract=contract())
    if lane=='review':
        with kb.write_txn(conn):conn.execute("UPDATE tasks SET status='review' WHERE id=?",(tid,))
    spawned=[]
    def spawn(task,workspace,admission_contract=None,**kw):spawned.append(task.id)
    kb.dispatch_once(conn,spawn_fn=spawn)
    assert spawned==[]
    original=kb.get_task(conn,tid)
    assert original.status=='todo' and original.current_run_id is None
    link=conn.execute('SELECT parent_id FROM task_links WHERE child_id=?',(tid,)).fetchone()
    prep=kb.get_task(conn,link[0])
    assert prep.assignee=='owner' and prep.tenant==original.tenant and prep.status=='ready'
    row=conn.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?',(prep.id,)).fetchone()
    pc=json.loads(row[0]);assert pc['security_realm']=='company' and pc['approval_gates']==['publish']
    assert pc['callback']['report_policy']=='on_demand'
    assert not conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',(tid,prep.id)).fetchone()
    assert not wr.queue_preparation(conn,original,lane,{'issues':[{'code':'missing','resource':'x'}]})
    # Real dispatch admits the owner preparation without needing the missing service.
    kb.dispatch_once(conn,spawn_fn=spawn)
    assert spawned==[prep.id]
    # Emulate the owner narrowing the task after it provides verified offline inputs.
    assert kb.complete_task(conn,prep.id,result='Prepared a bounded offline phase',metadata={'readiness_repair':{'requirements':{'toolsets':['file']},'evidence':'Verified bounded offline fixture inputs.'}})
    assert kb.get_task(conn,tid).status==lane
    kb.dispatch_once(conn,spawn_fn=spawn)
    assert spawned==[prep.id,tid]
    admitted=json.loads(conn.execute('SELECT worker_admission_contract FROM tasks WHERE id=?',(tid,)).fetchone()[0])
    assert admitted['skill_dependencies']['readiness']['issues']==[]
    if lane=='review':
        assert 'sdlc-review' in admitted['skills']


def test_changed_task_cannot_queue_stale_preparation(board):
    conn,_=board
    tid=kb.create_task(conn,title='task',assignee='worker',owner_cell_contract=contract())
    task=kb.get_task(conn,tid)
    with kb.write_txn(conn):conn.execute('UPDATE tasks SET body=? WHERE id=?',('new requirements',tid))
    assert not wr.queue_preparation(conn,task,'ready',{'issues':[{'code':'missing','resource':'x'}]})
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1


def test_owner_preparation_preserves_original_admitted_input_workspace(board):
    conn,home=board
    workspace=home/'approved-work';workspace.mkdir()
    (workspace/'source.py').write_text('def transform(value): return value\n')
    tid=kb.create_task(conn,title='Repair approved transform',assignee='worker',tenant='private-business-topic',owner_cell_contract=contract(),workspace_kind='dir',workspace_path=str(workspace))
    task=kb.get_task(conn,tid)
    assert wr.queue_preparation(conn,task,'ready',{'issues':[{'code':'missing_input','resource':'fixture.csv'}]})
    prep_id=conn.execute('SELECT parent_id FROM task_links WHERE child_id=?',(tid,)).fetchone()[0]
    prep=kb.get_task(conn,prep_id)
    assert prep.workspace_kind=='dir' and prep.workspace_path==str(workspace)
    assert kb.get_task(conn,tid).status=='todo'
    assert prep.assignee=='owner' and prep.tenant==task.tenant
    assert (workspace/'source.py').read_text()=='def transform(value): return value\n'
    assert wr.probe({'security':{'workspace_confinement':{'enabled':True}}},wr.requirements('fixture',declared(read_paths=['source.py'])),prep.workspace_path,['file'])['issues']==[]


def test_owner_preparation_does_not_silently_replace_missing_input_directory(board):
    conn,home=board
    tid=kb.create_task(conn,title='Repair',assignee='worker',owner_cell_contract=contract(),workspace_kind='dir',workspace_path=str(home/'missing'))
    assert not wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',{'issues':[{'code':'missing_input','resource':'source.py'}]})
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1


def test_missing_owner_keeps_task_sticky_and_never_grants_access(board):
    conn,_=board
    tid=kb.create_task(conn,title='Metricool task',assignee='worker')
    def spawn(*a,admission_contract=None,**kw):pytest.fail('worker started')
    result=kb.dispatch_once(conn,spawn_fn=spawn)
    assert result.capability_blocked==[tid]
    assert kb.get_task(conn,tid).status=='blocked'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1


def test_real_mcp_discovery_requires_the_exact_operation(board):
    import threading, yaml
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    conn,home=board
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(405); self.end_headers()
        def do_DELETE(self):
            self.send_response(200); self.end_headers()
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'id' not in request:
                self.send_response(202); self.end_headers(); return
            if request['method']=='initialize':
                result={'protocolVersion':request['params']['protocolVersion'], 'capabilities':{'tools':{}}, 'serverInfo':{'name':'fixture','version':'1'}}
            elif request['method']=='tools/list':
                result={'tools':[{'name':'fixture_read','description':'fixture read','inputSchema':{'type':'object','properties':{}}}]}
            else: result={}
            data=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        path=home/'profiles/worker/config.yaml'
        cfg=yaml.safe_load(path.read_text());cfg['mcp_servers']={'fixture':{'url':f'http://127.0.0.1:{server.server_port}/mcp'}}
        path.write_text(yaml.safe_dump(cfg))
        tid=kb.create_task(conn,title='fixture',assignee='worker',body=declared(mcp={'fixture':['fixture_read']}))
        try:
            admitted=kb._build_worker_admission(kb.get_task(conn,tid),'worker')
        except kb.WorkerAdmissionError as exc:
            pytest.fail(json.dumps(exc.readiness))
        assert admitted.contract['skill_dependencies']['readiness']['checked']==['mcp_connected:fixture']
        with kb.write_txn(conn):conn.execute('UPDATE tasks SET body=? WHERE id=?',(declared(mcp={'fixture':['not_available']}),tid))
        with pytest.raises(kb.WorkerAdmissionError) as error:
            kb._build_worker_admission(kb.get_task(conn,tid),'worker')
        assert error.value.readiness['issues']==[{'code':'missing_mcp_operation','resource':'fixture:not_available'}]
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_preparation_is_bounded_and_does_not_recurse(board):
    conn,_=board
    tid=kb.create_task(conn,title='task',assignee='worker',owner_cell_contract=contract())
    report={'issues':[{'code':'missing_input','resource':'input.txt'}]}
    for _ in range(2):
        task=kb.get_task(conn,tid)
        assert wr.queue_preparation(conn,task,'ready',report)
        prep_id=conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY rowid DESC LIMIT 1',(tid,)).fetchone()[0]
        prep=kb.get_task(conn,prep_id)
        assert not wr.queue_preparation(conn,prep,'ready',report)
        assert kb.complete_task(conn,prep_id,result='Attempted repair',metadata={'readiness_repair':{'requirements':{},'evidence':'Verified fixture attempt completed.'}})
    assert not wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',report)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==3
    assert kb.get_task(conn,tid).consecutive_failures==0
    # A genuinely different missing input gets a bounded repair, rather than
    # the prior issue's lifetime cap silently abandoning progressing work.
    changed={'issues':[{'code':'missing_input','resource':'new-source.csv'}]}
    assert wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',changed)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==4


def test_changed_evidence_does_not_create_parallel_owner_repairs(board):
    conn,_=board
    tid=kb.create_task(conn,title='task',assignee='worker',owner_cell_contract=contract())
    assert wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',{'issues':[{'code':'missing_input','resource':'input.txt'}]})
    # Simulate a stale external writer moving the original back to ready.
    with kb.write_txn(conn): conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(tid,))
    assert not wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',{'issues':[{'code':'missing_input','resource':'new-source.csv'}]})
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==2


def test_preparation_transaction_rolls_back_if_owner_contract_is_invalid(board):
    conn,_=board
    tid=kb.create_task(conn,title='task',assignee='worker',owner_cell_contract=contract())
    with kb.write_txn(conn):conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?',(json.dumps({'security_realm':'company'}),tid))
    assert not wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',{'issues':[{'code':'missing','resource':'x'}]})
    assert kb.get_task(conn,tid).status=='ready'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
    assert conn.execute('SELECT COUNT(*) FROM task_links').fetchone()[0]==0


def test_named_profile_dispatch_uses_installation_readiness_policy(board,monkeypatch):
    _,home=board
    monkeypatch.setenv('HERMES_HOME',str(home/'profiles/worker'))
    monkeypatch.setenv('HERMES_PROFILE','worker')
    assert wr.settings()['enabled'] is True


@pytest.mark.parametrize('repair',[None,{'requirements':{},'evidence':'ok'}, {'requirements':{},'evidence':'Verified fixture','runtime_profile':'unrelated-admin'}])
def test_preparation_cannot_complete_without_valid_scoped_handoff(board,repair):
    conn,_=board
    tid=kb.create_task(conn,title='task',assignee='worker',owner_cell_contract=contract())
    assert wr.queue_preparation(conn,kb.get_task(conn,tid),'ready',{'issues':[{'code':'missing','resource':'x'}]})
    prep_id=conn.execute('SELECT parent_id FROM task_links WHERE child_id=?',(tid,)).fetchone()[0]
    with pytest.raises(ValueError):
        kb.complete_task(conn,prep_id,metadata={'readiness_repair':repair})
    assert kb.get_task(conn,prep_id).status=='ready'
    assert kb.get_task(conn,tid).status=='todo'
    assert kb.get_task(conn,tid).assignee=='worker'


def startup_failure(conn, tid, *, phase='ready', observed=True):
    with kb.write_txn(conn):
        run = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) "
            "VALUES (?, 'worker', 'crashed', 1, 2, 'crashed')", (tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='blocked', claim_lock=NULL, worker_pid=NULL, "
            "consecutive_failures=2, current_run_id=? WHERE id=?", (run,tid))
        kb._append_event(conn, tid, 'gave_up', {'retry_status':phase}, run_id=run)
        if observed:
            kb._append_event(conn, tid, 'worker_startup_diagnostic', {
                'returncode':1, 'diagnostic_code':'worker_import_failed',
                'stderr_excerpt':'private prose must not appear in the owner repair task'}, run_id=run)
    return run


@pytest.mark.parametrize('phase',['ready','review'])
def test_failed_startup_returns_to_existing_head_once_and_resumes_phase(board,phase):
    conn,_=board
    tid=kb.create_task(conn,title='Authorized implementation',assignee='worker',owner_cell_contract=contract())
    startup_failure(conn,tid,phase=phase)
    assert wr.recover_failed_startups(conn)==[tid]
    assert wr.recover_failed_startups(conn)==[]
    prep_id=conn.execute('SELECT parent_id FROM task_links WHERE child_id=?',(tid,)).fetchone()[0]
    prep=kb.get_task(conn,prep_id)
    assert prep.assignee=='owner' and 'private prose' not in prep.body
    assert kb.get_task(conn,tid).consecutive_failures==2
    assert kb.complete_task(conn,prep_id,result='Repair verified',metadata={'readiness_repair':{
        'requirements':{'toolsets':['file']},'evidence':'Verified assigned worker source import in the admitted workspace.'}})
    assert kb.get_task(conn,tid).consecutive_failures==0
    assert kb.get_task(conn,tid).status==phase


def test_failed_startup_without_owner_contract_is_one_durable_actionable_case(board):
    conn,_=board
    tid=kb.create_task(conn,title='Legacy work',assignee='worker')
    startup_failure(conn,tid)
    assert wr.recover_failed_startups(conn)==[]
    assert wr.recover_failed_startups(conn)==[]
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
    cases=conn.execute("SELECT payload FROM task_events WHERE kind='owner_startup_repair_case'").fetchall()
    assert len(cases)==1
    case=json.loads(cases[0][0])
    assert case['state']=='owner_action_required' and case['next_action']
    assert kb.get_task(conn,tid).status=='blocked'


def test_failed_startup_does_not_release_manual_hold_or_unobserved_crash(board):
    conn,_=board
    held=kb.create_task(conn,title='Held work',assignee='worker',owner_cell_contract=contract())
    startup_failure(conn,held)
    with kb.write_txn(conn): conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(held,))
    assert kb.block_task(conn,held,reason='Explicit protected action requires exact approval',kind='needs_input')
    unknown=kb.create_task(conn,title='Historic crash',assignee='worker',owner_cell_contract={**contract(),'goal_id':'other','objective':'other unobserved work'})
    startup_failure(conn,unknown,observed=False)
    assert wr.recover_failed_startups(conn,limit=8)==[]
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==2


def test_repeated_same_startup_findings_do_not_reset_owner_repair_budget(board):
    conn,_=board
    tid=kb.create_task(conn,title='Authorized implementation',assignee='worker',owner_cell_contract=contract())
    for attempt in range(3):
        startup_failure(conn,tid)
        assert wr.recover_failed_startups(conn)==([tid] if attempt<2 else [])
        if attempt<2:
            prep_id=conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY rowid DESC LIMIT 1',(tid,)).fetchone()[0]
            kb.complete_task(conn,prep_id,metadata={'readiness_repair':{'requirements':{},'evidence':'Verified bounded repair attempt; retained original progress.'}})
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==3
    case=json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_startup_repair_case' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()[0])
    assert case['state']=='owner_action_required'


def test_real_startup_diagnostic_producer_keeps_budget_across_variable_stderr(board):
    conn,_=board
    tid=kb.create_task(conn,title='Authorized implementation',assignee='worker',owner_cell_contract=contract())
    fingerprints=[]
    for attempt in range(3):
        run=startup_failure(conn,tid,observed=False)
        kb._record_private_worker_startup_diagnostic(board=None,task_id=tid,run_id=run,returncode=1,
            stderr_bytes=f'2026-09-06T12:0{attempt}:00 /private/tmp/run-{attempt}/source.py\nRuntimeError: same startup failure\n'.encode(),stderr_truncated=False)
        row=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_startup_diagnostic' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
        data=json.loads(row[0]);fingerprints.append(data['runtime_source_fingerprint'])
        assert wr.recover_failed_startups(conn)==([tid] if attempt<2 else [])
        if attempt<2:
            prep=json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_queued' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()[0])['preparation_task']
            kb.complete_task(conn,prep,metadata={'readiness_repair':{'requirements':{},'evidence':'Verified unchanged source and retained original inputs.'}})
    assert len(set(fingerprints))==1

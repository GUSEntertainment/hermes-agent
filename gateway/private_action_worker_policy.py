"""Read-only private app task admission; no model-owned authority or producer."""
import contextlib,hashlib,json,os,re,sqlite3,subprocess,sys
from pathlib import Path
ROOT=Path('/Users/Chris/.hermes')
BUNDLE=Path('/Users/Chris/.hermes/context-runtimes/app-work-20260924-v2')
BUNDLE_SHA='cc12054bd9a63c2edef2b9caa1556a70804d342ebd58cf26506eacd8e5556afe'
PYTHON=Path('/Users/Chris/.hermes/releases/hermes-agent-20260925-api-prewarm-v49/.venv/bin/python')
VERSION='owner-private-action-workspace/v1'

def board_database(root,board):
    return root/'kanban/kanban.db' if board=='default' else root/'kanban/boards'/board/'kanban.db'

def needs_private_proof(actions,native,task,board):
    """Classify original private provenance, not the generic preparation event."""
    def linked(identifier):
        return actions.execute("SELECT 1 FROM phone_task_links WHERE task_id=? AND board=? AND authority_realm LIKE 'owner:%' LIMIT 1",(identifier,board)).fetchone() is not None
    if linked(task):return True
    events=native.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_for'",(task,)).fetchall()
    for event in events:
        parent=json.loads(event[0] or '{}').get('task_id')
        if not isinstance(parent,str) or parent==task:raise PermissionError('Invalid preparation source')
        if linked(parent):return True
        if classified_private(native,parent):raise PermissionError('Original private preparation action link missing')
    if classified_private(native,task):raise PermissionError('Original private action link missing')
    return False

def current(task,board,worker,*,review_setup=False):
    for value in (task,board,worker):
        if not isinstance(value,str) or not re.fullmatch('[a-zA-Z0-9_.-]{1,100}',value):raise PermissionError('Invalid private admission identity')
    from gateway.app_business_authority import protected
    source=ROOT/'private/phone-assistant/actions.sqlite'
    protected(source,ROOT)
    with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        native_path=board_database(ROOT,board);protected(native_path,ROOT)
        with contextlib.closing(sqlite3.connect(native_path.as_uri()+'?mode=ro',uri=True)) as native:
            if not needs_private_proof(db,native,task,board):return None
    result=subprocess.run([str(PYTHON),'-I','-B',str(Path(__file__).resolve()),task,board,worker,*(['--review-setup'] if review_setup else [])],capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if result.returncode or len(result.stdout)>4096:raise PermissionError('Private task source unavailable')
    value=json.loads(result.stdout)
    if value is not None and (not isinstance(value,dict) or value.get('version')!=VERSION or value.get('task')!=task or value.get('board')!=board or value.get('worker')!=worker):raise PermissionError('Invalid private task proof')
    return value

def link_authority(row):
    return {key:row.get(key) for key in ('action_id','task_id','subject_key','realm','authority_realm','account','authority_ref','source_surface','return_surface','board','route_contract_json','canonical_action_digest','idempotency_key','owner_profile','runtime_profile','capability_bundle','native_security_realm')}

def record_authority(row):
    if row is None:return None
    return {key:row[key] for key in ('request_key','action_id','source_kind','source_ref','envelope_json','source_digest','grant_fingerprint','projection_json','route_json')}

def resolve(task,board,worker,module,kb,*,root=ROOT,admission=None,review_setup=False):
    from gateway.app_business_authority import protected_bytes,protected
    root=Path(root);admission=admission or module.Admission()
    path=admission.paths.actions
    # No database creation or schema migration through this admission path.
    before=protected(path,root)
    for suffix in ('-wal','-shm'):
        side=Path(str(path)+suffix)
        if side.exists() or side.is_symlink():protected(side,root)
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as actions:
        actions.row_factory=sqlite3.Row
        rows=actions.execute("SELECT * FROM phone_task_links WHERE task_id=? AND board=? AND authority_realm LIKE 'owner:%'",(task,board)).fetchall()
        if not rows:return preparation(task,board,worker,module,kb,root=root,admission=admission)
        if len(rows)!=1:raise PermissionError('Ambiguous private action')
        row=dict(rows[0])
        if row['create_state']!='bound':raise PermissionError('Private source not admitted')
        record=None
        if row['action_id'].startswith('app_'):
            record=actions.execute('SELECT * FROM app_requests WHERE action_id=?',(row['action_id'],)).fetchone()
            if not record:raise PermissionError('Original private app request required')
            projection,g,p=admission.validate_record(dict(record))
        elif row['action_id'].startswith(('act_','txt_')):
            from gateway.private_action_binding import current_binding
            grants=module.private_json(admission.paths.grants)
            matches=[(ref,g) for ref,g in grants.items() if isinstance(g,dict) and g.get('realm')==row['authority_realm'] and g.get('board')==board and g.get('account')==row['account'] and g.get('enabled') is True and not g.get('revoked_at')]
            if len(matches)!=1:raise PermissionError('Unique current private grant required')
            ref,g=matches[0]
            consumer=dict(owner=g['owner'],domain=g['domain'],agent=g['agent'],profile=g['profile'],audience=g['audience'],grantVersion=g['version'])
            g,p,*_=current_binding(module,admission,consumer,ref)
            projection=row
        else:raise PermissionError('Unsupported private source')
        installed_binding=getattr(module,'PRIVATE_WORK_BINDING',None)
        if installed_binding is not None:
            ref=json.loads(record['envelope_json'])['grant_ref'] if record is not None else ref
            if installed_binding.get('workGrantRef')!=ref or installed_binding.get('workGrantVersion')!=g['version'] or installed_binding.get('privateGrantVersion')!=g['private_grant_version'] or installed_binding.get('profile')!=g['profile'] or installed_binding.get('owner')!=g['owner']:raise PermissionError('Private installed work binding changed')
        realm='owner:'+g['owner']
        if g['realm']!=realm or row['authority_realm']!=realm or row['account']!=g['account'] or row['subject_key']!=p['subject_key'] or row['board']!=g['board']:raise PermissionError('Private owner scope mismatch')
        module.validate_parent_source(actions,row,p,admission)
        route=admission.read_route(p,row)
        canonical=lambda x:module.work._canonical_json(module.dataclasses.asdict(x))
        if canonical(route)!=row['route_contract_json'] or (record is not None and canonical(route)!=record['route_json']):raise PermissionError('Private route changed')
        if route.security_realm!=realm or route.authority_realm!=realm or route.board!=board:raise PermissionError('Private realm route required')
        # No reviewer, roster or arbitrary profile exemption. A reviewed private
        # route can admit its explicitly registered reviewer, workspace-only.
        policy_file=root/'private/phone-assistant/private-review-policy.json'
        if not policy_file.exists() and not policy_file.is_symlink():raise PermissionError('Private review policy is not installed')
        raw=protected_bytes(policy_file,True,root);policy=json.loads(raw)
        if set(policy) not in ({'version','bindings'},{'version','default_reviewer','bindings'}) or policy['version']!=1 or not isinstance(policy['bindings'],list):raise PermissionError('Invalid private review policy')
        matched=[r for r in policy['bindings'] if r.get('owner')==g['owner']]
        if len(matched)>1:raise PermissionError('Ambiguous private reviewer')
        if matched:review_policy=matched[0]
        else:
            default=policy.get('default_reviewer')
            if not isinstance(default,str):raise PermissionError('Private reviewer is not registered')
            review_policy=dict(owner=g['owner'],board=board,reviewer=default,enabled=True,scope_contract='owner-private-artifact-review/v1')
        if set(review_policy)!={'owner','board','reviewer','enabled','scope_contract'} or review_policy['enabled'] is not True or review_policy['board']!=board or review_policy['scope_contract']!='owner-private-artifact-review/v1':raise PermissionError('Private reviewer revoked')
        reviewer=review_policy['reviewer']
        if reviewer==route.runtime_profile or not isinstance(reviewer,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',reviewer):raise PermissionError('Independent private reviewer required')
        protected_bytes(root/'profiles'/reviewer/'config.yaml',anchor=root)
        if worker not in {route.runtime_profile,route.required_reviewer_profile,reviewer}:raise PermissionError('Private worker not registered for this route')
        protected_bytes(root/'profiles'/worker/'config.yaml',anchor=root)
        native_path=board_database(root,board);native_before=protected(native_path,root)
        with contextlib.closing(sqlite3.connect(native_path.as_uri()+'?mode=ro',uri=True)) as native:
            native.row_factory=sqlite3.Row
            actual=native.execute('SELECT assignee,status FROM tasks WHERE id=?',(task,)).fetchone()
            if not actual or actual['assignee']!=worker:raise PermissionError('Private task assignment changed')
            goal=native.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(task,)).fetchone()
            contract=json.loads(goal['contract_json']) if goal else {}
            if review_policy is not None and not review_setup and (not goal or goal['router_mode']!='reviewed' or goal['required_reviewer_profile']!=reviewer or contract.get('private_review_policy')!=review_policy):raise PermissionError('Private review workflow not initialized')
            if worker==reviewer:
                goal=native.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(task,)).fetchone()
                contract=json.loads(goal['contract_json']) if goal else {}
                if not goal or goal['phase']!='review' or goal['router_mode']!='reviewed' or goal['required_reviewer_profile']!=reviewer or goal['reviewer_profile']!=reviewer or contract.get('private_review_policy')!=review_policy or contract.get('runtime_profile')!=route.runtime_profile:raise PermissionError('Private reviewer task phase not authorized')
            snapshot=module.work._native_snapshot(native,kb,task,board=board)
            if not snapshot:raise PermissionError('Private native task unavailable')
            module.work._validate_snapshot(module.work.NativeTaskSnapshot(**snapshot),projection=row,route=route)
        again=actions.execute('SELECT * FROM phone_task_links WHERE action_id=?',(row['action_id'],)).fetchone()
        if not again or link_authority(dict(again))!=link_authority(row) or again['create_state']!='bound':raise PermissionError('Private action changed')
        if record is not None:
            fresh_record=actions.execute('SELECT * FROM app_requests WHERE action_id=?',(row['action_id'],)).fetchone()
            if not fresh_record or record_authority(fresh_record)!=record_authority(record) or admission.validate_record(dict(fresh_record))!=(projection,g,p):raise PermissionError('Private authority changed')
        else:
            fresh_g,fresh_p,*_=current_binding(module,admission,consumer,ref)
            if (fresh_g,fresh_p)!=(g,p):raise PermissionError('Private authority changed')
        module.validate_parent_source(actions,row,p,admission)
        if canonical(admission.read_route(p,row))!=canonical(route):raise PermissionError('Private route changed')
        for path,stamp in [(path,before),(native_path,native_before)]:
            after=protected(path,root)
            if (stamp.st_dev,stamp.st_ino)!=(after.st_dev,after.st_ino):raise PermissionError('Private source replaced')
        if review_policy is not None and json.loads(protected_bytes(policy_file,True,root))!=policy:raise PermissionError('Private review policy changed')
        digest=hashlib.sha256(json.dumps({'record':record_authority(record),'link':link_authority(row),'grant':g,'profile':p,'route':canonical(route),'review_policy':review_policy,'installed_binding':installed_binding},sort_keys=True,default=str).encode()).hexdigest()
        return dict(version=VERSION,principal=g['owner'],board=board,task=task,worker=worker,action=row['action_id'],authority_fingerprint=digest,review_policy=review_policy)

def classified_private(native,task):
    row=native.execute("SELECT t.tenant,g.security_realm FROM tasks t LEFT JOIN owner_cell_goals g ON g.task_id=t.id WHERE t.id=?",(task,)).fetchone()
    if row and (str(row[0] or '').startswith('app:private:') or str(row[1] or '').startswith('owner:')):return True
    for event in native.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created'",(task,)):
        value=json.loads(event[0] or '{}')
        if str(value.get('tenant','')).startswith('app:private:'):return True
    return False

def preparation(task,board,worker,module,kb,*,root,admission):
    from gateway.app_business_authority import protected
    path=board_database(root,board);before=protected(path,root)
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as native:
        native.row_factory=sqlite3.Row
        events=native.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_for'",(task,)).fetchall()
        if not events:
            if classified_private(native,task):raise PermissionError('Original private action link missing')
            return None
        if len(events)!=1:raise PermissionError('Ambiguous preparation source')
        event=json.loads(events[0]['payload']);parent=event.get('task_id')
        if not isinstance(parent,str) or parent==task:raise PermissionError('Invalid preparation source')
        # Exactly one original dependency; no arbitrary descendant authority.
        links=native.execute("SELECT child_id,edge_role FROM task_links WHERE parent_id=?",(task,)).fetchall()
        if [(r['child_id'],r['edge_role']) for r in links]!=[(parent,'dependency')]:raise PermissionError('Preparation dependency changed')
        original=native.execute('SELECT * FROM tasks WHERE id=?',(parent,)).fetchone()
        prep=native.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if not original or not prep or prep['tenant']!=original['tenant'] or prep['assignee']!=worker:raise PermissionError('Preparation owner changed')
        # Native enqueue event must link back to this exact preparation task.
        queued=native.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_queued'",(parent,)).fetchall()
        if not any(json.loads(e['payload']).get('preparation_task')==task for e in queued):raise PermissionError('Original preparation evidence missing')
        with contextlib.closing(sqlite3.connect(admission.paths.actions.as_uri()+'?mode=ro',uri=True)) as actions:
            if actions.execute("SELECT 1 FROM phone_task_links WHERE task_id=? AND board=? AND create_state='bound'",(parent,board)).fetchone() is None:raise PermissionError('Preparation must reference original action task')
        # The original remains assigned to its maker while this prerequisite
        # undergoes independent review. Derive source custody from that maker,
        # then authorize the current preparation reviewer on its own contract.
        maker=original['assignee']
        policy=resolve(parent,board,maker,module,kb,root=root,admission=admission)
        if policy is None:raise PermissionError('Private preparation authority missing')
        goal=None
        if worker!=maker:
            review_policy=policy.get('review_policy') or {}
            goal=native.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(task,)).fetchone()
            contract=json.loads(goal['contract_json']) if goal else {}
            if (worker!=review_policy.get('reviewer') or not goal
                    or goal['phase']!='review' or goal['router_mode']!='reviewed'
                    or goal['required_reviewer_profile']!=worker or goal['reviewer_profile']!=worker
                    or prep['runtime_profile']!=worker or goal['runtime_profile']!=worker
                    or contract.get('private_review_policy')!=review_policy
                    or contract.get('runtime_profile')!=maker
                    or contract.get('security_realm')!=original['security_realm']
                    or prep['security_realm']!=original['security_realm']
                    or prep['owner_profile']!=original['owner_profile']
                    or any(prep[k]!=original[k] for k in ('workspace_path','workspace_kind'))
                    or not goal['review_artifact_ref'] or not goal['review_artifact_digest']
                    or goal['review_state_version']!=goal['current_state_version']):
                raise PermissionError('Private preparation reviewer not authorized')
        elif prep['runtime_profile']!=original['runtime_profile']:
            raise PermissionError('Preparation maker runtime changed')
        after=protected(path,root)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise PermissionError('Preparation database replaced')
        fresh=native.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if dict(fresh)!=dict(prep):raise PermissionError('Preparation changed')
        if goal is not None:
            fresh_goal=native.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(task,)).fetchone()
            if not fresh_goal or dict(fresh_goal)!=dict(goal):raise PermissionError('Preparation review changed')
        binding = {
            'original_authority': policy['authority_fingerprint'],
            'task': task, 'parent': parent, 'worker': worker,
            'tenant': prep['tenant'], 'realm': prep['security_realm'],
            'owner': prep['owner_profile'], 'workspace': prep['workspace_path'],
        }
        if goal is not None:
            binding['review'] = {key: goal[key] for key in (
                'contract_json', 'phase', 'router_mode', 'runtime_profile',
                'required_reviewer_profile', 'reviewer_profile',
                'review_artifact_ref', 'review_artifact_digest',
                'review_state_version', 'current_state_version')}
        fingerprint=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return dict(policy,task=task,worker=worker,preparation_for=parent,authority_fingerprint=fingerprint)

def main():
    runtime=Path(__file__).resolve().parents[1];sys.path.insert(0,str(runtime))
    from gateway.app_business_authority import protected_bytes,protected
    # Select the exact installed app-work version from protected owner binding,
    # never from task/model input. V1 remains a separate pinned source contract.
    task,board=sys.argv[1:3]
    protected(ROOT/'private/phone-assistant/actions.sqlite',ROOT)
    protected(board_database(ROOT,board),ROOT)
    with contextlib.closing(sqlite3.connect((ROOT/'private/phone-assistant/actions.sqlite').as_uri()+'?mode=ro',uri=True)) as actions:
        with contextlib.closing(sqlite3.connect(board_database(ROOT,board).as_uri()+'?mode=ro',uri=True)) as native:
            if not needs_private_proof(actions,native,task,board):
                print('null');return
        row=actions.execute('SELECT authority_realm FROM phone_task_links WHERE task_id=? AND board=?',(task,board)).fetchall()
        if not row:
            with contextlib.closing(sqlite3.connect((board_database(ROOT,board)).as_uri()+'?mode=ro',uri=True)) as native:
                sources=native.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='readiness_preparation_for'",(task,)).fetchall()
                if len(sources)!=1:raise PermissionError('Private source owner missing')
                original=json.loads(sources[0][0])['task_id']
                row=actions.execute('SELECT authority_realm FROM phone_task_links WHERE task_id=? AND board=?',(original,board)).fetchall()
        if len(row)!=1 or not str(row[0][0]).startswith('owner:'):raise PermissionError('Unique private owner required')
        owner=row[0][0][6:]
    binding_path=ROOT/'private/phone-assistant/app-private-work-bindings.json'
    binding_raw=protected_bytes(binding_path,True,ROOT)
    bindings=json.loads(binding_raw)
    rows=[r for r in bindings['bindings'] if r.get('owner')==owner and r.get('enabled') is True]
    if len(rows)!=1:raise PermissionError('Private work binding unavailable')
    helper=rows[0]['helperSha256']
    if helper=='bdce287144d7e235d13da663efe8cc3f2d302ca13bbf32d9c698a715fef0142e':bundle,bundle_sha,version=BUNDLE,BUNDLE_SHA,2
    elif helper=='5fd860e765af21e4806ce7adabb862669af9279f6b39557114b72a9828ea4c91':bundle,bundle_sha,version=Path('/Users/Chris/.hermes/context-runtimes/app-work-20260920-v1'),'5b32f2a4719d542f33c9d41aaed282a2f723ccc300fc67d52f6264f80d3323c5',1
    else:raise PermissionError('Unsupported private source version')
    raw=protected_bytes(bundle/'BUNDLE.json',anchor=ROOT)
    if hashlib.sha256(raw).hexdigest()!=bundle_sha:raise PermissionError('Private bundle changed')
    manifest=json.loads(raw)
    for name,digest in manifest['files'].items():
        if not name.startswith('processors/') or len(Path(name).parts)!=2 or not name.endswith('.py'):raise PermissionError('Invalid private bundle')
        if hashlib.sha256(protected_bytes(bundle/name,anchor=ROOT)).hexdigest()!=digest:raise PermissionError('Private source changed')
    if {str(p.relative_to(bundle)) for p in (bundle/'processors').glob('*.py')}!=set(manifest['files']):raise PermissionError('Unexpected private module')
    sys.path.insert(0,str(bundle/'processors'))
    import phone_app_work
    phone_app_work.PRIVATE_DOCUMENT_BINDING_VERSION=version
    phone_app_work.PRIVATE_WORK_BINDING=rows[0]
    from hermes_cli import kanban_db
    args=sys.argv[1:];setup=len(args)==4 and args[-1]=='--review-setup'
    if len(args)!=(4 if setup else 3):raise PermissionError('Invalid private admission command')
    value=resolve(*args[:3],phone_app_work,kanban_db,review_setup=setup)
    if protected_bytes(binding_path,True,ROOT)!=binding_raw:raise PermissionError('Private installed binding changed')
    print(json.dumps(value))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit(1)


def reconcile_reviews(conn,*,board,only_task=None):
    """Existing dispatcher only: register review before first private maker run."""
    from hermes_cli import kanban_db as kb,owner_cell as oc
    policy_path=ROOT/'private/phone-assistant/private-review-policy.json'
    if not policy_path.exists() and not policy_path.is_symlink():return 0
    changed=0
    eligible="t.current_run_id IS NULL AND t.status IN ('todo','ready','blocked') AND g.router_mode='single' AND g.security_realm LIKE 'owner:%' AND NOT EXISTS(SELECT 1 FROM task_runs r WHERE r.task_id=t.id)"
    # Persisted least-recently-attempted order survives restart. Generic native
    # event GC keeps these watermarks while the tasks remain nonterminal.
    sql="SELECT t.id,t.assignee FROM tasks t JOIN owner_cell_goals g ON g.task_id=t.id WHERE "+eligible
    params=()
    if only_task is not None:sql+=' AND t.id=?';params=(only_task,)
    sql+=" ORDER BY COALESCE((SELECT MAX(e.id) FROM task_events e WHERE e.task_id=t.id AND e.kind='private_review_readiness_checked'),0),t.created_at,t.id LIMIT 25"
    rows=conn.execute(sql,params).fetchall()
    for row in rows:
        with kb.write_txn(conn):
            fresh=conn.execute("SELECT t.assignee FROM tasks t JOIN owner_cell_goals g ON g.task_id=t.id WHERE t.id=? AND "+eligible,(row['id'],)).fetchone()
            if not fresh or fresh['assignee']!=row['assignee']:continue
            kb._append_event(conn,row['id'],'private_review_readiness_checked')
        try:proof=current(row['id'],board,row['assignee'],review_setup=True)
        except PermissionError:continue # normal admission reports this task's blocker
        if not proof or not proof.get('review_policy'):continue
        with kb.write_txn(conn):
            task=conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
            goal=conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?',(row['id'],)).fetchone()
            if not task or task['current_run_id'] is not None or task['status'] not in {'todo','ready','blocked'} or task['assignee']!=row['assignee'] or not goal or goal['router_mode']!='single' or conn.execute('SELECT 1 FROM task_runs WHERE task_id=?',(row['id'],)).fetchone():raise PermissionError('Private task changed before review registration')
            if current(row['id'],board,row['assignee'],review_setup=True)!=proof:raise PermissionError('Private review authority changed')
            old=json.loads(goal['contract_json']);updated=dict(old)
            updated.update(router_mode='reviewed',required_reviewer_profile=proof['review_policy']['reviewer'],private_review_policy=proof['review_policy'],worker_budget={**old['worker_budget'],'reviewers':1})
            oc.validate_goal_contract(updated)
            version=oc.update_state(conn,row['id'],expected_version=goal['current_state_version'],facts=['Independent private artifact review registered under current owner authority.'],created_by='private-review-policy')
            conn.execute("UPDATE owner_cell_goals SET router_mode='reviewed',required_reviewer_profile=?,worker_budget_json=?,contract_json=? WHERE task_id=?",(updated['required_reviewer_profile'],oc._canonical_json(updated['worker_budget']),oc._canonical_json(updated),row['id']))
            conn.execute("UPDATE tasks SET router_mode='reviewed',current_state_version=? WHERE id=?",(version,row['id']))
            kb.add_comment(conn,row['id'],author='private-review-policy',body='Registered independent private artifact review; original action, route, realm and callback retained.')
            changed+=1
    return changed


def delivery_instructions(native, policy, task_id, *, root=ROOT, verify=None, actions_path=None):
    """Read-only worker projection; no delivery, permission or tool is created.

    Called only behind authenticated_private_policy. Dependency overrides are
    internal protected-fixture seams, never model arguments. Preparation and
    phone routes are deliberately unsupported by this bounded app projection.
    """
    from gateway.app_business_authority import protected
    root=Path(root)
    if (not isinstance(policy,dict) or policy.get('version')!=VERSION
            or task_id!=policy.get('task') or policy.get('preparation_for')):
        return None
    action=policy.get('action','')
    # Only app_ actions have an app-request delivery projection. Owner-delegated
    # work (owner_work:) is admitted by gateway.owner_delegation and returns via
    # its own custody binding; re-verifying it here against phone_task_links
    # raised 'Original private action link missing' inside kanban_show
    # (t_3a3a6969). Authority is still re-checked by authenticated_private_policy.
    if not isinstance(action,str) or not action.startswith('app_'):return None
    verify=verify or (lambda:current(task_id,policy['board'],policy['worker']))
    if verify()!=policy:raise PermissionError('Private delivery authority changed')
    path=Path(actions_path) if actions_path is not None else root/'private/phone-assistant/actions.sqlite'
    before=protected(path,root)
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as actions:
        actions.row_factory=sqlite3.Row
        def source():
            rows=actions.execute('SELECT * FROM phone_task_links WHERE action_id=? AND task_id=? AND board=?',(action,task_id,policy['board'])).fetchall()
            records=actions.execute('SELECT * FROM app_requests WHERE action_id=?',(action,)).fetchall()
            if len(rows)!=1 or len(records)!=1:raise PermissionError('Unique private app source required')
            row,record=dict(rows[0]),dict(records[0]);route=json.loads(row['route_contract_json'])
            if (row['create_state']!='bound' or record['source_kind']!='app'
                    or record['route_json']!=row['route_contract_json']
                    or row['return_surface']!='kanban:'+policy['board']
                    or route.get('return_surface')!=row['return_surface']
                    or route.get('source_surface')!=record['source_ref']):
                return None
            return (link_authority(row),record_authority(record))
        bound=source()
        if bound is None:return None
        def native_routes():
            goal=native.execute('SELECT contract_json FROM owner_cell_goals WHERE task_id=?',(task_id,)).fetchone()
            if not goal:return None
            callback=json.loads(goal[0]).get('callback')
            if not isinstance(callback,dict) or callback.get('return_surface')!='kanban:'+policy['board'] or callback.get('source_surface')!=bound[1]['source_ref']:return None
            # Presence of ANY additional route prevents an app-only claim,
            # including inactive or uncertain records. Do not inspect targets.
            try:
                intents=native.execute('SELECT count(*) FROM task_return_intents WHERE task_id=?',(task_id,)).fetchone()[0]
                subscriptions=native.execute('SELECT count(*) FROM kanban_notify_subs WHERE task_id=?',(task_id,)).fetchone()[0]
            except sqlite3.OperationalError:return None
            return (goal[0],intents,subscriptions)
        routes=native_routes()
        if routes is None:return None
        if verify()!=policy or source()!=bound or native_routes()!=routes:
            raise PermissionError('Private delivery binding changed')
    after=protected(path,root)
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise PermissionError('Private delivery source replaced')
    return {'version':'private-app-worker-delivery/v1','verified_source':'authenticated_private_app',
        'registered_return':'private_app_board','return_surface':'kanban:'+policy['board'],'app_only_at_read':routes[1:]==(0,0),
        'additional_delivery_routes_present':routes[1: ]!=(0,0),
        'artifact_workflow':'Create the requested artifact in your admitted workspace, attach it to this task, and request the required independent review. Native infrastructure handles the registered return after reviewed completion; do not create a separate send or inspect private configuration.',
        'delivery_status':'not_yet_verified',
        'scope_notice':'This describes current task routing, not a global prohibition on external sends or a new permission.'}

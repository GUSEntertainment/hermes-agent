"""Personal controls keep native storage/authentication; all state is temporary."""
import hashlib
import json
import time
from pathlib import Path

import pytest
import yaml

from gateway import app_private_board_authority as private, josh_board_authority as auth
from gateway import member_board_tools as member, board_control_transport as transport
from hermes_cli import board_owner_manage as control, kanban_db as kb


@pytest.fixture
def personal(tmp_path, monkeypatch):
    root = tmp_path / 'hermes'
    base = root / 'private' / 'phone-assistant'
    home = root / 'profiles' / 'app-owner-chris-margot'
    home.mkdir(parents=True, mode=0o700)
    base.mkdir(parents=True, mode=0o700)
    profile = home.name
    def save(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data) if path.suffix == '.yaml' else json.dumps(data))
        path.chmod(0o600)
    phone = '+15555550100'
    subject = 'registry:chris_owner:' + hashlib.sha256(phone.encode()).hexdigest()
    config = dict(toolsets=['owner_context', private.TOOLSET],
        platform_toolsets={'api_server':['owner_context', private.TOOLSET]},
        gateway={'multiplex_profiles':False},
        platforms={'api_server':{'enabled':True,'extra':{'host':'127.0.0.1','port':56711,'cockpit_principal':'chris'}}},
        mcp_servers={'owner_context':{'args':['-I','fixture_reader.py',str(base/'owner-context-chris.json')]}},
        app_private_board_control={'enabled':True,'principal':'chris','board':'chris-home','scope_contract':private.CONTRACT})
    backend = dict(owner='chris',domain='owner_private',agent='margot',profile=profile,enabled=True,
        url='http://127.0.0.1:56711',storePath=str(root/'private/app-continuity/chris/conversations.sqlite'),
        grantRef='private-read',grantVersion=1,keyEnv='HERMES_PRIVATE_CHRIS_KEY',verificationReceipt='fixture')
    work = dict(owner='chris',domain='owner_private',agent='margot',profile=profile,enabled=True,
        privateGrantVersion=1,workGrantRef='private-work',workGrantVersion=1)
    grant = dict(owner='chris',profile_key='chris',profile=profile,domain='owner_private',agent='margot',audience='margot-app',
        version=1,private_grant_ref='private-read',private_grant_version=1,realm='owner:chris',board='chris-home',
        enabled=True,expires_at=time.time()+3600,subject_key=subject,account=None)
    save(home/'config.yaml',config)
    save(base/'app-private-backends.json',dict(version=1,bindings=[backend]))
    save(base/'app-private-work-bindings.json',dict(version=1,bindings=[work]))
    save(base/'app-work-grants.json',{'private-work':grant})
    save(base/'owner-context-chris.json',dict(version=1,enabled=True,grant_ref='private-read',consumer=dict(
        owner='chris',domain='owner_private',agent='margot',profile=profile,audience='margot-app',grantVersion=1)))
    save(base/'caller-profiles.json',dict(profiles=[dict(profile_key='chris',phone_e164=phone,subject_key=subject,
        allowed_realms=['business','owner:chris'],allowed_accounts=[],board='chris-home',enabled=True)]))
    save(root/'private/phone_acl/caller_registry.private.json',dict(people=[dict(phone_e164=phone,role='chris_owner',enabled=True)]))
    for name in ('HERMES_KANBAN_TASK','HERMES_SESSION_SOURCE','HERMES_KANBAN_DB','HERMES_KANBAN_HOME','HERMES_SESSION_PROFILE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_PROFILE',profile)
    monkeypatch.setattr(private,'ROOT',root)
    monkeypatch.setattr(control,'ROOT',root)
    monkeypatch.setattr(transport,'ROOT',root)
    own=root/'kanban/boards/chris-home/kanban.db'
    other=root/'kanban/boards/other-home/kanban.db'
    own.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    with kb.connect_closing(own) as conn: task=kb.create_task(conn,title='Personal fixture',assignee='maker',triage=True)
    with kb.connect_closing(other) as conn: foreign=kb.create_task(conn,title='OTHER_PRIVATE_SENTINEL',assignee='maker',triage=True)
    return dict(root=root,home=home,base=base,profile=profile,save=save,config=config,grant=grant,backend=backend,task=task,foreign=foreign)


def test_authenticated_personal_scope_lists_shows_and_comments_only_own_board(personal):
    p=personal
    with auth.execution_scope(None,'chris'):
        grant=control.require_owner()
        assert grant['principal']=='chris' and grant['profile']==p['profile'] and grant['board']=='chris-home'
        assert auth.app_binding()['scope_contract']==private.CONTRACT
        assert [x['id'] for x in json.loads(member.list_work({}))['items']]==[p['task']]
        shown=json.loads(member.show({'task_id':p['task']}))
        assert shown['board_administration']['native']['tool']=='own_work_manage'
        assert 'OTHER_PRIVATE_SENTINEL' not in member.show({'task_id':p['foreign']})
        assert json.loads(member.comment({'task_id':p['task'],'body':'The user asked to archive this obsolete fixture.'}))['ok']
        with pytest.raises(PermissionError):control.member_board(grant,'other-home')


@pytest.mark.parametrize('principal',[None,'connor','joshgarcia'])
def test_other_or_missing_authenticated_principal_cannot_borrow_personal(personal,principal):
    with pytest.raises(PermissionError):
        with auth.execution_scope(None,principal):pass


def test_profile_environment_alone_never_grants_controls(personal):
    with pytest.raises(PermissionError):control.require_owner()


@pytest.mark.parametrize('field,value',[('enabled',False),('expires_at',0),('realm','business'),('board','other-home')])
def test_changed_or_expired_private_work_grant_denies_controls(personal,field,value):
    p=personal
    with auth.execution_scope(None,'chris'):
        p['grant'][field]=value
        p['save'](p['base']/'app-work-grants.json',{'private-work':p['grant']})
        with pytest.raises(PermissionError):control.require_owner()


@pytest.mark.parametrize('source',['kanban','cron'])
def test_workers_and_cron_cannot_inherit_personal_grant(personal,monkeypatch,source):
    with auth.execution_scope(None,'chris'):
        monkeypatch.setenv('HERMES_SESSION_SOURCE',source)
        with pytest.raises(PermissionError):control.require_owner()


def test_private_catalog_exposes_only_the_four_board_aliases(personal):
    import model_tools
    with auth.execution_scope(None,'chris'):
        schemas=model_tools.get_tool_definitions([private.TOOLSET],quiet_mode=True,skip_tool_search_assembly=True)
        assert {s['function']['name'] for s in schemas}==member.TOOLS
        assert not auth.app_tool_allowed('terminal')
        assert not auth.app_tool_allowed('kanban_manage')
        assert not auth.app_tool_allowed('own_work_create')


def test_capability_requires_current_authentication_and_actual_registry(personal,monkeypatch):
    import model_tools
    assert private.capabilities(None,'chris')=={'tools':sorted(member.TOOLS)}
    assert private.capabilities(None,'connor') is None
    assert private.capabilities(None,None) is None
    monkeypatch.setattr(model_tools,'get_tool_definitions',lambda *a,**kw:[])
    assert private.capabilities(None,'chris') is None


def test_personal_signed_controller_roundtrip_and_revocation(personal,monkeypatch):
    from gateway import session_context
    p=personal
    transport.key_path().parent.mkdir(mode=0o700)
    transport.key_path().write_bytes(b'p'*32);transport.key_path().chmod(0o600)
    transport.initialize_ledger(transport.ledger_path())
    monkeypatch.setattr(session_context,'get_bound_session_env',lambda key,*a:'private-session' if key=='HERMES_SESSION_ID' else '')
    with auth.execution_scope(None,'chris'):
        grant=control.require_owner()
        shown=json.loads(member.show({'task_id':p['task']}))
        payload=dict(action='archive',task_id=p['task'],board=grant['board'],reason='Obsolete fixture, owner asked for cleanup',
                     expected_state=shown['board_administration']['native']['expected_state'])
        envelope=transport.issue(payload,grant,'private-session')
    # Mirror the isolated controller's sanitized runtime, with no API context.
    monkeypatch.setenv('HERMES_HOME',str(p['root']));monkeypatch.setenv('HERMES_PROFILE','default')
    operation,verified=transport.verify(envelope)
    token=control._member_context.set(verified)
    try:
        assert control.execute(operation)['ok']
    finally:control._member_context.reset(token)
    with kb.connect_closing(board='chris-home') as conn:assert kb.get_task(conn,p['task']).status=='archived'
    with pytest.raises(PermissionError,match='already consumed'):transport.verify(envelope)
    p['grant']['enabled']=False;p['save'](p['base']/'app-work-grants.json',{'private-work':p['grant']})
    with pytest.raises(PermissionError):control.refresh_member_grant(verified)


def test_existing_native_member_refresh_is_unchanged(personal,monkeypatch):
    from gateway import app_business_authority as business
    monkeypatch.setattr(business,'canonical_enrollment',lambda principal,root:dict(owner=principal,profile=principal,board='connor-home',fingerprint='a'*64))
    value=control.refresh_member_grant(dict(principal='connor',profile='connor',board='',fingerprint=''))
    assert value==dict(principal='connor',profile='connor',board='connor-home',fingerprint='a'*64)

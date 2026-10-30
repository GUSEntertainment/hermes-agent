import json
import sqlite3
from pathlib import Path
import pytest
from gateway import gus_worker_authority as authority


@pytest.fixture
def enrolled(tmp_path):
    root=tmp_path/'hermes';home=root/'profiles'/'claire';home.mkdir(parents=True)
    (home/'config.yaml').write_text('toolsets: [kanban, file]\n')
    registry=tmp_path/'registry.yaml'
    # t_9ac68588: member profiles are enrolled by their own board owner, never in
    # Chris's roster; no global exclusion list strips them back out.
    registry.write_text(json.dumps({'profile_roster':{'canonical':['claire','julia'],
        'department_owners':['claire','julia'],'department_makers':[],
        'review_and_bounded_support':[]}}))
    board=root/'kanban'/'boards'/'chris-home';board.mkdir(parents=True);db=board/'kanban.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT, assignee TEXT, runtime_profile TEXT)')
        conn.execute('INSERT INTO tasks VALUES (?,?,?)',('t_fixture','claire','claire'))
    env={'HERMES_PROFILE':'claire','HERMES_SESSION_SOURCE':'kanban','HERMES_KANBAN_BOARD':'chris-home',
         'HERMES_KANBAN_TASK':'t_fixture','HERMES_KANBAN_DB':str(db)}
    return dict(home=home,root=root,registry=registry,env=env)


def test_native_team_worker_is_bound_to_installed_assignment(enrolled):
    assert authority.current(**enrolled)=={'profile':'claire','board':'chris-home','task':'t_fixture'}


@pytest.mark.parametrize('change',[{'HERMES_PROFILE':'joshgarcia'},{'HERMES_KANBAN_TASK':'t_other'},
    {'HERMES_KANBAN_BOARD':'josh-home'},{'HERMES_KANBAN_DB':'/tmp/other.db'},{'HERMES_KANBAN_HOME':'/tmp'}])
def test_wrong_assignment_cannot_be_exempted(enrolled,change):
    enrolled['env'].update(change)
    with pytest.raises(PermissionError):authority.current(**enrolled)


def test_api_request_does_not_inherit_native_worker_exemption(enrolled):
    enrolled['env']['HERMES_SESSION_SOURCE']='api_server'
    assert authority.current(**enrolled) is None


@pytest.mark.parametrize('profile',['family-eliana','joshgarcia','unknown'])
def test_family_members_and_unknown_profiles_not_team_workers(enrolled,profile):
    # Not in Chris's roster -> no GUS/Chris worker authority (scope wall), with no
    # hardcoded name list involved.
    home=enrolled['root']/'profiles'/profile;home.mkdir(parents=True,exist_ok=True)
    (home/'config.yaml').write_text('toolsets: [kanban]\n')
    enrolled['home']=home;enrolled['env']['HERMES_PROFILE']=profile
    assert authority.current(**enrolled) is None
    assert not hasattr(authority,'EXCLUDED')


def test_registry_and_task_assignment_both_required(enrolled):
    with sqlite3.connect(enrolled['env']['HERMES_KANBAN_DB']) as conn:
        conn.execute('UPDATE tasks SET assignee="joshgarcia"')
    with pytest.raises(PermissionError):authority.current(**enrolled)


def test_untrusted_registry_rejected(enrolled):
    enrolled['registry'].chmod(0o666)
    with pytest.raises(PermissionError):authority.current(**enrolled)


def test_writable_parent_rejected(enrolled):
    enrolled['home'].chmod(0o777)
    with pytest.raises(PermissionError):authority.current(**enrolled)


def test_hardlinked_registry_rejected(enrolled):
    import os
    os.link(enrolled['registry'],enrolled['registry'].with_suffix('.link'))
    with pytest.raises(PermissionError):authority.current(**enrolled)


def test_leaf_cannot_control_board(monkeypatch):
    from gateway import josh_board_authority as board
    from agent import delegation_context
    monkeypatch.setattr(authority,'current',lambda:{'profile':'claire','board':'chris-home','task':'t_fixture'})
    monkeypatch.setattr(delegation_context,'is_delegated_child_context',lambda:True)
    with pytest.raises(PermissionError):board.admitted_board('chris-home')


def test_internal_board_reader_cannot_escape_assigned_board(monkeypatch):
    from gateway import josh_board_authority as board
    monkeypatch.setattr(authority,'current',lambda:{'profile':'claire','board':'chris-home','task':'t_fixture'})
    assert board.admitted_board(None)=='chris-home'
    assert board.admitted_board('chris-home')=='chris-home'
    with pytest.raises(PermissionError):board.admitted_board('josh-home')


def test_api_principal_binding_takes_precedence_over_process_worker(monkeypatch):
    from gateway import josh_board_authority as board
    monkeypatch.setattr(authority,'current',lambda:{'profile':'claire','board':'chris-home','task':'t_fixture'})
    token=board._execution.set(('joshgarcia','joshgarcia'))
    try:
        assert board.restricted()
        with pytest.raises(PermissionError):board.admitted_board('chris-home')
    finally:board._execution.reset(token)


def test_julia_hermes_profile_is_a_worker(enrolled):
    """t_96d87a50: ~/.hermes/profiles/julia is a real worker lane (not OpenClaw Julia)."""
    home=enrolled['root']/'profiles'/'julia';home.mkdir(parents=True)
    (home/'config.yaml').write_text('toolsets: [kanban, file]\n')
    with sqlite3.connect(enrolled['env']['HERMES_KANBAN_DB']) as conn:
        conn.execute('UPDATE tasks SET assignee="julia", runtime_profile=NULL')
    enrolled['home']=home;enrolled['env']['HERMES_PROFILE']='julia'
    assert authority.current(**enrolled)=={'profile':'julia','board':'chris-home','task':'t_fixture'}

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.gateway.test_app_private_board_authority import personal
from gateway import josh_board_authority as auth, member_board_tools as member
from gateway import board_control_transport as transport, session_context
from hermes_cli import board_owner_manage as control, kanban_db as kb


@pytest.fixture
def administrative(personal, monkeypatch):
    p = personal
    catalog = json.loads((p['base']/'caller-profiles.json').read_text())
    phone = '+15555550202'
    catalog['profiles'].append(dict(profile_key='connor', phone_e164=phone,
        subject_key='registry:connor_owner:'+hashlib.sha256(phone.encode()).hexdigest(),
        allowed_realms=['business'], board='other-home', enabled=True))
    p['save'](p['base']/'caller-profiles.json', catalog)
    registry = json.loads((p['root']/'private/phone_acl/caller_registry.private.json').read_text())
    registry['people'].append(dict(phone_e164=phone, role='admin_internal', enabled=True))
    p['save'](p['root']/'private/phone_acl/caller_registry.private.json', registry)
    folder = p['root']/'private/board-owner-control';folder.mkdir(mode=0o700)
    transport.key_path().write_bytes(b'k'*32);transport.key_path().chmod(0o600)
    transport.initialize_ledger(transport.ledger_path())
    monkeypatch.setattr(session_context, 'get_bound_session_env',
        lambda name,*args: 'admin-session' if name == 'HERMES_SESSION_ID' else '')
    return p


def test_authenticated_admin_reads_explicit_foreign_board(administrative):
    p=administrative
    with auth.execution_scope(None, 'chris'):
        assert control.administrator_board(control.require_owner(), 'other-home') == 'other-home'
        result=json.loads(member.list_work({'board':'other-home'}))
        assert [row['id'] for row in result['items']] == [p['foreign']]
        shown=json.loads(member.show({'task_id':p['foreign'], 'board':'other-home'}))
        assert shown['task']['id'] == p['foreign']
        assert shown['board_administration']['native']['expected_state']


def test_signed_admin_operation_in_actual_controller_process_preserves_owner(administrative):
    p=administrative
    foreign=p['root']/'kanban/boards/other-home/kanban.db'
    with kb.connect_closing(foreign) as conn:
        expected=control.describe(conn,p['foreign'])['expected_state']
        original=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(p['foreign'],)).fetchone())
    args=dict(action='archive',task_id=p['foreign'],board='other-home',
        expected_state=expected,reason='Chris administrator: retire obsolete fixture, preserve its owner.')
    with auth.execution_scope(None, 'chris'):
        envelope=transport.issue(args,control.require_owner(),'admin-session')
    script="""import sys
from pathlib import Path
from hermes_cli import board_owner_manage as c
from gateway import board_control_transport as t, app_private_board_authority as p
c.ROOT=t.ROOT=p.ROOT=Path(sys.argv[1])
raise SystemExit(c.main())
"""
    env={'PATH':os.environ['PATH'],'HOME':str(p['root'].parent),'HERMES_HOME':str(p['root']),
         'PYTHONPATH':str(Path(__file__).parents[2]),'PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([sys.executable,'-P','-c',script,str(p['root'])],
        input=json.dumps(envelope),text=True,capture_output=True,timeout=30,env=env,cwd=p['root'])
    assert result.returncode == 0,result.stdout+result.stderr
    receipt=json.loads(result.stdout);assert receipt['administrator']=='chris'
    assert receipt['status']=='archived' and receipt['original_owner_preserved']
    with kb.connect_closing(foreign) as conn:
        current=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(p['foreign'],)).fetchone())
        for key in ('tenant','security_realm','created_by','owner_profile','runtime_profile','workspace_path'):
            assert current[key] == original[key]
        event=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrator_board_action'",(p['foreign'],)).fetchone()
        assert json.loads(event[0])['actor']=='chris'
    replay=subprocess.run([sys.executable,'-P','-c',script,str(p['root'])],
        input=json.dumps(envelope),text=True,capture_output=True,timeout=30,env=env,cwd=p['root'])
    assert replay.returncode != 0 and 'already consumed' in replay.stdout


def test_revoked_admin_role_is_not_semantic_authority(administrative):
    p=administrative
    with auth.execution_scope(None, 'chris'):
        grant=control.require_owner()
        path=p['root']/'private/phone_acl/caller_registry.private.json'
        data=json.loads(path.read_text());data['people'][0]['role']='member';p['save'](path,data)
        with pytest.raises(PermissionError):control.administrator_board(grant,'other-home')


def test_other_member_or_model_claim_cannot_become_admin(administrative):
    p=administrative
    with auth.execution_scope(None, 'chris'):
        grant=control.require_owner()
        with pytest.raises(PermissionError):
            control.administrator_board(dict(grant,principal='connor'),'other-home')
        with pytest.raises(ValueError):
            member.manage({'task_id':p['foreign'],'action':'work','reason':'Do it',
                'board':'other-home','administrator':True})


def test_unknown_unenrolled_board_is_not_admin_target(administrative):
    with auth.execution_scope(None, 'chris'):
        with pytest.raises(PermissionError):
            control.administrator_board(control.require_owner(),'unregistered')


def test_member_tool_calls_signed_controller_and_never_rebinds_admin_inbox(administrative,monkeypatch):
    import shlex
    from gateway import owner_attention_binding
    p=administrative
    root=str(Path(__file__).parents[2])
    code=("import sys;from pathlib import Path;sys.path.insert(0,"+repr(root)+");"
          "from hermes_cli import board_owner_manage as c;"
          "from gateway import board_control_transport as t,app_private_board_authority as p;"
          "c.ROOT=t.ROOT=p.ROOT=Path(sys.argv[1]);raise SystemExit(c.main())")
    wrapper=p['root']/'scripts/hermes-kanban-guarded';wrapper.parent.mkdir(mode=0o700)
    wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -P -c '+shlex.quote(code)+' '+shlex.quote(str(p['root']))+'\n')
    wrapper.chmod(0o700)
    monkeypatch.setattr(owner_attention_binding,'bind_current',
        lambda *args,**kwargs:pytest.fail('Admin maintenance must preserve the original return and never bind the administrator inbox'))
    foreign=p['root']/'kanban/boards/other-home/kanban.db'
    with kb.connect_closing(foreign) as conn:expected=control.describe(conn,p['foreign'])['expected_state']
    with auth.execution_scope(None,'chris'):
        receipt=json.loads(member.manage(dict(action='archive',task_id=p['foreign'],
            board='other-home',expected_state=expected,reason='Admin retires obsolete fixture.')))
    assert receipt.get('ok'),receipt
    assert receipt['administrator']=='chris' and receipt['status']=='archived'
    assert receipt['follow_up_bound'] is False and receipt['original_owner_preserved']

"""Exercise real admission_requirements opening a separate disposable held board."""
import dataclasses
import hashlib
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, owner_cell as oc, owner_workflow as workflow
from tests.hermes_cli.test_owner_cell_qa_regressions import contract


@pytest.fixture
def held(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('HERMES_KANBAN_DB','HERMES_KANBAN_HOME','HERMES_KANBAN_BOARD',
                 'HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_SESSION_SOURCE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oc, '_installed_profile', lambda value:value)
    board = 'held-review'
    kb.init_db(board=board)
    conn = kb.connect(board=board)
    tid = kb.create_task(conn,title='Synthetic held review',body='Produce a local artifact.',
        assignee='claire',owner_cell_contract=contract(owner_profile='claire',runtime_profile='claire'))
    artifact = tmp_path / 'artifact.txt'
    artifact.write_text('Synthetic immutable review bytes.')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert kb.request_review(conn,tid,summary='Artifact bound',reviewer='grace',
        metadata={'owner_cell_artifact':{'ref':str(artifact),'digest':digest}})
    conn.execute("UPDATE tasks SET status='triage',block_kind='capability',block_recurrences=2 WHERE id=?",(tid,))
    conn.commit()
    yield conn,tid,board,artifact
    conn.close()


def call(held, projected=None, profile='grace', board=None):
    conn,tid,own_board,_ = held
    if projected is None: projected=dataclasses.replace(kb.get_task(conn,tid),status='review')
    return workflow.admission_requirements(projected, profile,
        {'toolsets':['terminal'],'commands':['maker-only-command']},board=board or own_board)


@pytest.mark.parametrize('status',['triage','blocked'])
def test_real_requirements_accept_exact_idle_held_review_without_mutating(held,status):
    conn,tid,board,artifact=held
    conn.execute('UPDATE tasks SET status=? WHERE id=?',(status,tid));conn.commit()
    before=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    previous_runs=[dict(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]
    needed,extra=call(held)
    assert needed['toolsets']==['file','kanban'] and needed['read_paths']==[str(artifact)]
    assert not extra and not needed['commands']
    assert dict(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())==before
    assert [dict(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=?',(tid,))]==previous_runs


@pytest.mark.parametrize('changed',['unprojected','body','wrong_reviewer','maker','claim','active_run','lease','artifact','version','contract_reviewer','foreign_board'])
def test_real_requirements_refuse_nonexact_or_active_held_projection(held,changed):
    conn,tid,board,artifact=held
    projected=dataclasses.replace(kb.get_task(conn,tid),status='review')
    profile='grace'
    requested_board=None
    if changed=='unprojected': projected=kb.get_task(conn,tid)
    elif changed=='body': projected=dataclasses.replace(projected,body='Unbound changed work')
    elif changed in ('wrong_reviewer','maker'): profile='scarlett' if changed=='wrong_reviewer' else 'claire'
    elif changed=='claim': conn.execute("UPDATE tasks SET claim_lock='active' WHERE id=?",(tid,))
    elif changed=='active_run': conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES(?,'grace','running',1)",(tid,))
    elif changed=='lease':
        conn.execute("INSERT INTO task_targets(task_id,target_key,access_mode,expected_version) VALUES(?,?,'mutate','v1')",(tid,'synthetic'))
        oc.acquire_mutator_leases(conn,tid,run_id=1,expected_state_version=1)
    elif changed=='artifact': artifact.write_text('Changed bytes')
    elif changed=='version': conn.execute('UPDATE owner_cell_goals SET current_state_version=2 WHERE task_id=?',(tid,))
    elif changed=='contract_reviewer': conn.execute("UPDATE owner_cell_goals SET required_reviewer_profile='other' WHERE task_id=?",(tid,))
    else:
        # A different board with the same task ID but changed work must not
        # validate a projection obtained from the first board.
        kb.init_db(board='other')
        with kb.connect_closing(board='other') as other:
            other.execute("ATTACH DATABASE ? AS source",(str(kb.kanban_db_path(board=board)),))
            other.execute('INSERT INTO tasks SELECT * FROM source.tasks WHERE id=?',(tid,))
            other.execute('INSERT INTO owner_cell_goals SELECT * FROM source.owner_cell_goals WHERE task_id=?',(tid,))
            other.execute("UPDATE tasks SET body='Other private work' WHERE id=?",(tid,));other.commit()
        requested_board='other'
    conn.commit()
    with pytest.raises((oc.OwnerCellGuardError,ValueError)):
        call(held,projected,profile,requested_board)
    assert kb.get_task(conn,tid).status=='triage'


def test_existing_review_lane_still_allows_inspection_of_stale_artifact(held):
    conn,tid,_,artifact=held
    conn.execute("UPDATE tasks SET status='review' WHERE id=?",(tid,));conn.commit()
    artifact.write_text('Changed bytes requiring authenticated reviewer evidence rebind')
    needed,extra=call(held)
    assert needed['read_paths']==[str(artifact)] and not extra

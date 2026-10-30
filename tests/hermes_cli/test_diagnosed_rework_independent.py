"""Independent QA; disposable synthetic database only, outside submitted patch."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_legacy_review_resume import legacy, sha
from tests.hermes_cli.test_diagnosed_rework import diagnosis_args

@pytest.mark.parametrize('legacy', [6], indirect=True)
def test_missing_capability_must_not_authorize(legacy, monkeypatch):
    conn, tid, art, claim, _ = legacy
    assert kb.unblock_task(conn, tid)
    t = claim(True)
    # Pre-capability legacy review has no recorded capability and no pipe.
    conn.execute('UPDATE tasks SET review_decision_digest=NULL WHERE id=?', (tid,))
    conn.execute('UPDATE task_runs SET review_decision_digest=NULL WHERE id=?', (t.current_run_id,))
    from agent import review_authority as ra
    ra._reset_for_tests()
    monkeypatch.delenv('HERMES_REVIEW_DECISION_FD', raising=False)
    result = json.loads(kt._handle_owner_handoff(diagnosis_args(tid, art)))
    assert result.get('error'), result

@pytest.mark.parametrize('legacy', [6], indirect=True)
def test_handoff_redacts_durable_free_text(legacy):
    conn, tid, art, claim, _ = legacy
    assert kb.unblock_task(conn, tid)
    claim(True)
    args = diagnosis_args(tid, art)
    synthetic = 'sk-' + 'A' * 48
    assert synthetic not in kt.redact_sensitive_text(synthetic, force=True)
    args['summary'] += ' Synthetic credential marker: ' + synthetic
    args['acceptance_criteria'][0] += ' ' + synthetic
    result = json.loads(kt._handle_owner_handoff(args))
    assert result.get('status') == 'diagnosed_rework_authorized', result
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='diagnosed_rework_authorized'", (tid,)).fetchone()[0]
    assert synthetic not in payload, 'Unredacted synthetic credential persisted in event payload'

@pytest.mark.parametrize('legacy', [6], indirect=True)
@pytest.mark.parametrize('bad', ['json', 'array', 'oversized', 'hash', 'outside', 'missing_key', 'short', 'stale_state', 'wrong_owner'])
def test_reject_bad_diagnosis_atomically(legacy, bad):
    conn, tid, art, claim, _ = legacy
    assert kb.unblock_task(conn, tid)
    claim(True)
    args = diagnosis_args(tid, art)
    path = Path(args['brief_ref'])
    if bad == 'json': path.write_text('{broken')
    elif bad == 'array': path.write_text('[]')
    elif bad == 'oversized': path.write_text(' ' * 131073)
    elif bad == 'outside':
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(art.parent/'narrower'), tid))
    elif bad == 'missing_key':
        value=json.loads(path.read_text()); del value['changed_method']; path.write_text(json.dumps(value))
    elif bad == 'short': args['acceptance_criteria']=['short']
    elif bad == 'stale_state': conn.execute('UPDATE owner_cell_goals SET current_state_version=99 WHERE task_id=?', (tid,))
    elif bad == 'wrong_owner': conn.execute("UPDATE owner_cell_goals SET reviewer_profile='grace' WHERE task_id=?", (tid,))
    args['brief_digest']=sha(path) if bad != 'hash' else '0'*64
    before=tuple(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone())
    result=json.loads(kt._handle_owner_handoff(args))
    assert result.get('error'), result
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='diagnosed_rework_authorized'", (tid,)).fetchone()
    assert before == tuple(conn.execute('SELECT * FROM owner_cell_goals WHERE task_id=?', (tid,)).fetchone())

@pytest.mark.parametrize('legacy', [6], indirect=True)
@pytest.mark.parametrize('mutation', ['state', 'artifact', 'contract', 'repeat', 'rollback'])
def test_grant_binding_and_atomic_consumption(legacy, mutation, monkeypatch):
    conn, tid, art, claim, _ = legacy
    assert kb.unblock_task(conn, tid)
    t=claim(True)
    args=diagnosis_args(tid, art)
    assert json.loads(kt._handle_owner_handoff(args)).get('status') == 'diagnosed_rework_authorized'
    if mutation == 'repeat':
        assert json.loads(kt._handle_owner_handoff(args)).get('error')
        return
    if mutation == 'state':
        conn.execute('UPDATE owner_cell_goals SET current_state_version=2,review_state_version=2 WHERE task_id=?',(tid,))
    elif mutation == 'artifact':
        conn.execute("UPDATE owner_cell_goals SET review_artifact_digest='different' WHERE task_id=?",(tid,))
    elif mutation == 'contract':
        g=oc.goal_projection(conn,tid); c=json.loads(g['contract_json']); c['objective']+=' revised'
        conn.execute('UPDATE owner_cell_goals SET contract_json=? WHERE task_id=?',(json.dumps(c),tid))
    elif mutation == 'rollback':
        original=kb._end_run
        def fail(*a, **kw):
            original(*a, **kw)
            raise ValueError('synthetic late failure')
        monkeypatch.setattr(kb,'_end_run',fail)
    before_verdicts=[tuple(r) for r in conn.execute('SELECT * FROM review_verdicts WHERE task_id=?',(tid,))]
    with pytest.raises((ValueError, oc.OwnerCellGuardError)):
        kb.request_changes(conn,tid,reason='Independent correction test',expected_run_id=t.current_run_id,review_decision_capability=t.review_decision_capability)
    assert kb.get_task(conn,tid).status=='running'
    assert oc.goal_projection(conn,tid)['review_loop_count']==6
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='diagnosed_rework_consumed'",(tid,)).fetchone()
    assert before_verdicts==[tuple(r) for r in conn.execute('SELECT * FROM review_verdicts WHERE task_id=?',(tid,))]

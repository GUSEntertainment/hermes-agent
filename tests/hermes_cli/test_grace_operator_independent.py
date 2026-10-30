"""Grace independent probes: synthetic isolated boards only; not candidate patch."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_legacy_review_resume import legacy, sha
from tests.hermes_cli.test_diagnosed_rework import diagnosis_args

pytestmark = pytest.mark.parametrize('legacy', [6], indirect=True)


def setup(legacy):
    conn, tid, art, claim, review = legacy
    assert kb.unblock_task(conn, tid)
    task = claim(True)
    return conn, tid, art, task, diagnosis_args(tid, art)


def authorize(conn, tid, args, monkeypatch, **overrides):
    values = dict(authorization_ref='synthetic:user:1', authorization_text='Complete this existing task with one further correction.',
                  brief_ref=args['brief_ref'], brief_digest=args['brief_digest'])
    values.update(overrides)
    with monkeypatch.context() as mp:
        mp.delenv('HERMES_KANBAN_TASK', raising=False)
        return oc.authorize_operator_rework(conn, tid, **values)


def snapshot(conn):
    return {table: [tuple(r) for r in conn.execute('SELECT * FROM ' + table)]
            for table in ['tasks', 'task_runs', 'task_events', 'owner_cell_goals', 'review_verdicts']}


@pytest.mark.parametrize('bad', ['empty_ref', 'space_ref', 'null_ref', 'numeric_ref', 'empty_text', 'null_text',
    'bad_digest', 'absent_brief', 'outside', 'oversized', 'stale_state', 'nonexhausted', 'wrong_reviewer'])
def test_authorization_inputs_fail_without_mutation(legacy, monkeypatch, bad):
    conn, tid, art, task, args = setup(legacy)
    overrides = {}
    if bad == 'empty_ref': overrides['authorization_ref'] = ''
    elif bad == 'space_ref': overrides['authorization_ref'] = ' \n\t '
    elif bad == 'null_ref': overrides['authorization_ref'] = None
    elif bad == 'numeric_ref': overrides['authorization_ref'] = 4
    elif bad == 'empty_text': overrides['authorization_text'] = ' \n '
    elif bad == 'null_text': overrides['authorization_text'] = None
    elif bad == 'bad_digest': overrides['brief_digest'] = 'bad'
    elif bad == 'absent_brief': overrides['brief_ref'] = str(art.parent / 'absent')
    elif bad == 'outside': conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(art.parent / 'narrow'), tid))
    elif bad == 'oversized':
        Path(args['brief_ref']).write_text('a' * 131073)
        overrides['brief_digest'] = sha(Path(args['brief_ref']))
    elif bad == 'stale_state': conn.execute('UPDATE owner_cell_goals SET current_state_version=99 WHERE task_id=?', (tid,))
    elif bad == 'nonexhausted': conn.execute('UPDATE owner_cell_goals SET review_loop_count=5 WHERE task_id=?', (tid,))
    else: conn.execute("UPDATE owner_cell_goals SET required_reviewer_profile='grace' WHERE task_id=?", (tid,))
    before = snapshot(conn)
    with pytest.raises(oc.OwnerCellGuardError):
        authorize(conn, tid, args, monkeypatch, **overrides)
    assert snapshot(conn) == before


def test_pending_duplicate_normalization_and_redaction(legacy, monkeypatch):
    conn, tid, art, task, args = setup(legacy)
    synthetic_secret = 'sk-' + 'A' * 48
    authorize(conn, tid, args, monkeypatch, authorization_ref='  synthetic:user:1 \n', authorization_text='Continue ' + synthetic_secret)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='operator_rework_authorized'").fetchone()
    assert synthetic_secret not in row[0]
    assert json.loads(row[0])['authorization_ref'] == 'synthetic:user:1'
    before = snapshot(conn)
    for ref in ['synthetic:user:1', ' \t synthetic:user:1\n', 'synthetic:user:2']:
        with pytest.raises(oc.OwnerCellGuardError):
            authorize(conn, tid, args, monkeypatch, authorization_ref=ref)
        assert snapshot(conn) == before


@pytest.mark.parametrize('mutation', ['state', 'counter', 'workspace', 'brief_deleted', 'brief_symlink', 'wrong_token', 'expired_claim', 'late_failure'])
def test_consumption_rejects_stale_or_unauthorized_atomically(legacy, monkeypatch, mutation):
    conn, tid, art, task, args = setup(legacy)
    authorize(conn, tid, args, monkeypatch)
    token = task.review_decision_capability
    if mutation == 'state': conn.execute('UPDATE owner_cell_goals SET current_state_version=2,review_state_version=2 WHERE task_id=?', (tid,))
    elif mutation == 'counter': conn.execute('UPDATE owner_cell_goals SET review_loop_count=7 WHERE task_id=?', (tid,))
    elif mutation == 'workspace': conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(art.parent/'narrow'),tid))
    elif mutation == 'brief_deleted': Path(args['brief_ref']).unlink()
    elif mutation == 'brief_symlink':
        brief = Path(args['brief_ref'])
        replacement = art.parent/'other-brief'
        replacement.write_bytes(brief.read_bytes())
        brief.unlink(); brief.symlink_to(replacement)
        conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(art.parent/'narrow'),tid))
    elif mutation == 'wrong_token': token = 'wrong'
    elif mutation == 'expired_claim': conn.execute('UPDATE tasks SET claim_expires=1 WHERE id=?', (tid,))
    else:
        original = kb._end_run
        def fail(*a, **kw):
            original(*a, **kw)
            raise ValueError('synthetic post-consumption failure')
        monkeypatch.setattr(kb, '_end_run', fail)
    before = snapshot(conn)
    with pytest.raises((ValueError, oc.OwnerCellGuardError)):
        kb.request_changes(conn, tid, reason='Independent synthetic correction', expected_run_id=task.current_run_id, review_decision_capability=token)
    assert snapshot(conn) == before


def test_consumed_grant_needs_new_user_ref_and_preserves_maker(legacy, monkeypatch):
    conn, tid, art, task, args = setup(legacy)
    contract = oc.goal_projection(conn, tid)['contract_json']
    for index in range(3):
        authorize(conn, tid, args, monkeypatch, authorization_ref=f'synthetic:user:{index}')
        assert kb.request_changes(conn, tid, reason='Independent correction', expected_run_id=task.current_run_id,
                                  review_decision_capability=task.review_decision_capability)[0]
        assert kb.get_task(conn, tid).assignee == 'legal-drafter'
        row = oc.goal_projection(conn, tid)
        assert row['review_loop_count'] == 7 + index
        assert row['contract_json'] == contract
        assert row['required_reviewer_profile'] == 'steven'
        legacy[3](); task = legacy[4](7 + index)
        before = snapshot(conn)
        with pytest.raises(oc.OwnerCellGuardError):
            kb.request_changes(conn, tid, reason='No automatic reuse', expected_run_id=task.current_run_id,
                               review_decision_capability=task.review_decision_capability)
        with pytest.raises(oc.OwnerCellGuardError):
            authorize(conn, tid, args, monkeypatch, authorization_ref=f' synthetic:user:{index} ')
        assert snapshot(conn) == before
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('surface', ['native', 'tool'])
def test_operator_grant_requires_authenticated_native_reviewer_even_legacy(legacy, monkeypatch, surface):
    conn, tid, art, task, args = setup(legacy)
    assert json.loads(kt._handle_owner_handoff(args))['status'] == 'diagnosed_rework_authorized'
    assert kb.request_changes(conn, tid, reason='Spend diagnosed reserve', expected_run_id=task.current_run_id,
                              review_decision_capability=task.review_decision_capability)[0]
    legacy[3](); task = legacy[4](7)
    assert conn.execute("SELECT 1 FROM task_events WHERE kind='diagnosed_rework_consumed'").fetchone()
    # Synthetic representation of an old review run with no capability binding.
    conn.execute('UPDATE tasks SET review_decision_digest=NULL WHERE id=?', (tid,))
    conn.execute('UPDATE task_runs SET review_decision_digest=NULL WHERE id=?', (task.current_run_id,))
    from agent import review_authority as ra
    ra._reset_for_tests()
    monkeypatch.delenv('HERMES_REVIEW_DECISION_FD', raising=False)
    authorize(conn, tid, args, monkeypatch)
    before = snapshot(conn)
    try:
        if surface == 'native':
            result = kb.request_changes(conn, tid, reason='Legacy unauthenticated correction',
                                        expected_run_id=task.current_run_id, review_decision_capability=None)
        else:
            response = json.loads(kt._handle_request_changes({'task_id': tid, 'reason': 'Legacy unauthenticated correction'}))
            result = (response.get('ok', False), response.get('implementer'))
    except (ValueError, oc.OwnerCellGuardError):
        result = (False, 'guard rejected')
    assert not result[0], f'Unauthenticated correction accepted: {result}; loop={oc.goal_projection(conn, tid)["review_loop_count"]}'
    assert snapshot(conn) == before

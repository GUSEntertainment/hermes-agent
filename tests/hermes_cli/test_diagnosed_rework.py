"""One reserved owner-diagnosed correction; actual native review authority."""
import json
import pytest
from hermes_cli import kanban_db as kb, owner_cell as oc
from tools import kanban_tools as kt
from tests.hermes_cli.test_legacy_review_resume import legacy,sha


def diagnosis_args(tid,art):
    brief=art.parent/'diagnosis.json'
    brief.write_text(json.dumps({
        'root_cause':'Dataset position was incorrectly used as official question identity.',
        'source_evidence':'Official template captions and binding paths independently expose the mismatch.',
        'changed_method':'Derive the inventory from official controls before joining source-backed answers.',
        'verification':'Check every binding against independent official inventory and read saved values back.'}))
    return {'task_id':tid,'action':'diagnosed_rework','brief_ref':str(brief),'brief_digest':sha(brief),
            'summary':'Source-backed diagnosis changes the failed matching method, preserving original review.',
            'acceptance_criteria':['Every control maps to the exact official question.',
                                   'Saved values are checked against that independent inventory.']}


@pytest.mark.parametrize('legacy',[6],indirect=True)
def test_diagnosed_correction_returns_original_maker_then_independent_acceptance(legacy):
    conn,tid,art,claim,review=legacy
    assert kb.unblock_task(conn,tid)
    t=claim(True)
    prior=dict(oc.goal_projection(conn,tid))
    with pytest.raises(oc.OwnerCellGuardError,match='budget exhausted'):
        kb.request_changes(conn,tid,reason='Verified mapping defect',expected_run_id=t.current_run_id,
            review_decision_capability=t.review_decision_capability)
    args=diagnosis_args(tid,art)
    result=json.loads(kt._handle_owner_handoff(args));assert result.get('status')=='diagnosed_rework_authorized',result
    assert dict(oc.goal_projection(conn,tid))==prior
    assert kb.request_changes(conn,tid,reason='Use the source-derived inventory and independent checks.',expected_run_id=t.current_run_id,
            review_decision_capability=t.review_decision_capability)[0]
    g=dict(oc.goal_projection(conn,tid));assert g['review_loop_count']==7
    assert kb.get_task(conn,tid).assignee=='legal-drafter'
    assert g['contract_json']==prior['contract_json']
    claim();t=review(7)
    # The reserve cannot be replenished by another brief, state or run.
    result=json.loads(kt._handle_owner_handoff(args));assert result.get('error'),result
    with pytest.raises(oc.OwnerCellGuardError,match='budget exhausted'):
        kb.request_changes(conn,tid,reason='Another defect',expected_run_id=t.current_run_id,
            review_decision_capability=t.review_decision_capability)
    result=json.loads(kt._handle_complete({'task_id':tid,'summary':'Synthetic exact artifact verified',
        'metadata':{'owner_cell_verdict':{'state_version':1,'artifact_ref':str(art),
            'artifact_digest':sha(art),'verdict':'PASS','defects':[]}}}))
    assert result.get('ok') is True,result
    assert kb.get_task(conn,tid).status=='done'


@pytest.mark.parametrize('legacy',[6],indirect=True)
@pytest.mark.parametrize('failure',['wrong_capability','changed_brief','missing_evidence'])
def test_diagnosed_correction_fails_closed(legacy,failure):
    conn,tid,art,claim,review=legacy
    assert kb.unblock_task(conn,tid);t=claim(True);args=diagnosis_args(tid,art)
    if failure=='missing_evidence':
        from pathlib import Path
        p=Path(args['brief_ref']);p.write_text('{}');args['brief_digest']=sha(p)
        assert json.loads(kt._handle_owner_handoff(args)).get('error')
    elif failure=='wrong_capability':
        with pytest.raises((ValueError,oc.OwnerCellGuardError)):
            oc.authorize_diagnosed_rework(conn,tid,expected_run_id=t.current_run_id,review_decision_capability='untrusted',
                **{k:v for k,v in args.items() if k not in ('task_id','action')})
    else:
        assert json.loads(kt._handle_owner_handoff(args)).get('status')=='diagnosed_rework_authorized'
        from pathlib import Path
        Path(args['brief_ref']).write_text('Changed after authorization')
        with pytest.raises(oc.OwnerCellGuardError,match='diagnosis changed'):
            kb.request_changes(conn,tid,reason='Attempt changed diagnosis',expected_run_id=t.current_run_id,
                review_decision_capability=t.review_decision_capability)
    assert oc.goal_projection(conn,tid)['review_loop_count']==6
    assert kb.get_task(conn,tid).status=='running'

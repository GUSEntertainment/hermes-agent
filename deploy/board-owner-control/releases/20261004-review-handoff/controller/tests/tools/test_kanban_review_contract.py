"""The exposed review tool must explain and recover from malformed bindings."""
import hashlib
import json

import pytest
from jsonschema import validate

from agent.kanban_stop import build_kanban_stop_nudge
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt
from tests.hermes_cli.test_worker_readiness import board, contract


def reviewed_contract():
    value = contract()
    value.update(router_mode='reviewed', required_reviewer_profile='owner',
                 worker_budget={'producers': 1, 'reviewers': 1, 'extras': 0})
    value['delegation_roles']['reviewer'] = 'owner'
    # Independent reviewer must not also be the accountable owner.
    value['owner_profile'] = 'coordinator'
    return value


@pytest.mark.parametrize('bad', [
    {},
    {'reviewer': 'owner'},
    {'reviewer': 'wrong', 'metadata': {'owner_cell_artifact': {'ref': 'file', 'digest': 'hash'}}},
    {'reviewer': 'owner', 'metadata': {'artifact_ref': 'file', 'artifact_digest': 'hash'}},
    {'reviewer': 'owner', 'metadata': {'owner_cell_artifact': {'ref': '', 'digest': 'hash'}}},
])
def test_rejected_payload_is_actionable_and_corrected_native_handoff_ends_run(board, monkeypatch, bad):
    conn, home = board
    def forbidden_judge(*args, **kwargs):
        raise AssertionError('Review handoff must use native validation, not an auxiliary judge')
    monkeypatch.setattr(kt, '_goal_mode_handoff_rejection', forbidden_judge)
    tid = kb.create_task(conn, title='Produce a verified text artifact', assignee='worker',
                         body='Complete requested work and obtain independent reviewer PASS.',
                         goal_mode=True, owner_cell_contract=reviewed_contract())
    kb.claim_task(conn, tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_PROFILE', 'worker')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(kb.latest_run(conn, tid).id))
    before = kb.get_task(conn, tid)
    state = kb.owner_cell_read_state(conn, tid)
    failed = json.loads(kt._handle_request_review({'task_id': tid, 'summary': 'Verified', **bad}))
    assert failed['code'] == 'review_handoff_input_invalid'
    assert failed['recoverable'] is True
    assert failed['required_reviewer_profile'] == 'owner'
    assert 'metadata.owner_cell_artifact.ref' in failed['required_fields']
    assert kb.get_task(conn, tid).status == before.status
    assert kb.get_task(conn, tid).assignee == before.assignee
    assert kb.owner_cell_read_state(conn, tid) == state
    artifact = home / 'result.txt'
    artifact.write_text('The actual requested artifact.\n')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload = {'task_id': tid, 'summary': 'Artifact created and verified.',
               'reviewer': failed['required_reviewer_profile'],
               'metadata': {'owner_cell_artifact': {'ref': str(artifact), 'digest': digest}}}
    validate(payload, kt.KANBAN_REQUEST_REVIEW_SCHEMA['parameters'])
    result = kt._handle_request_review(payload)
    assert json.loads(result).get('status') == 'review', result
    assert kb.get_task(conn, tid).status == 'review'
    assert kb.get_task(conn, tid).assignee == 'owner'
    assert kb.latest_run(conn, tid).outcome == 'review_requested'
    landed = kb.owner_cell_read_state(conn, tid)
    assert landed['phase'] == 'review'
    assert landed['review_artifact_ref'] == str(artifact)
    assert landed['review_artifact_digest'] == digest
    assert build_kanban_stop_nudge(messages=[{'role': 'tool', 'name': 'kanban_request_review', 'content': result}]) is None


def test_ordinary_task_does_not_acquire_owner_cell_requirements(board, monkeypatch):
    conn, _ = board
    tid = kb.create_task(conn, title='Ordinary review', assignee='worker')
    kb.claim_task(conn, tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(kb.latest_run(conn, tid).id))
    result = json.loads(kt._handle_request_review({'task_id': tid, 'summary': 'Verified'}))
    assert result.get('status') == 'review', result

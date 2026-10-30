"""Shared native/own-work action catalog; no service or model calls."""
import json
from hermes_cli import board_owner_manage as owner

def test_main_and_own_work_share_schema_and_guarded_handler(monkeypatch):
    from tools import kanban_tools as kt
    from gateway import member_board_tools as member
    props = kt.KANBAN_MANAGE_SCHEMA['parameters']['properties']
    assert 'repair_review_reference' in props['action']['enum']
    assert {'expected_artifact_ref','expected_artifact_digest','artifact_ref'} <= set(props['evidence']['properties'])
    grant = dict(principal='member', profile='app-private-member', board='member-home', fingerprint='f' * 64)
    monkeypatch.setattr(member, '_grant', lambda: grant)
    monkeypatch.setattr(owner, 'member_board', lambda grant, board: grant['board'])
    seen = []
    monkeypatch.setattr(kt, '_handle_manage', lambda args: seen.append(args) or json.dumps({'ok': True}))
    result = member.manage({'action': 'repair_review_reference', 'task_id': 't_test'})
    assert json.loads(result)['ok']
    assert seen == [{'action':'repair_review_reference', 'task_id':'t_test', 'board':'member-home'}]

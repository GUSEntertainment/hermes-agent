"""Native creation/review/return regression: no provider, no live writes."""
import json
import os
import sqlite3
import pytest
import yaml
from test_member_owner_delegation import enrolled, request
from gateway import owner_delegation as work, owner_app_returns as replies, mirror
from hermes_cli import kanban_db as kb

@pytest.fixture
def own_agents(enrolled):
    root, catalog, env = enrolled
    folder = root / 'profiles/joshgarcia'
    folder.mkdir(mode=0o700)
    (folder / 'config.yaml').write_text('{}\n')
    identifiers = ['oa_' + ch * 32 for ch in 'ab']
    with sqlite3.connect(root / 'agent-os/owner-agents.sqlite') as conn:
        for identifier, name in zip(identifiers, ['Amanda', 'Morgan']):
            conn.execute('INSERT INTO owner_agents VALUES(?,?,?,?,NULL,2,?)',
                         (identifier, 'joshgarcia', name, 'Document drafting', identifier))
            folder = root / 'profiles' / work.agent_profile_name('joshgarcia', identifier)
            folder.mkdir(mode=0o700)
            (folder / 'config.yaml').write_text(yaml.safe_dump({'owner_agent': {
                'owner': 'joshgarcia', 'id': identifier, 'board': 'josh-home'}}))
    return identifiers

@pytest.mark.parametrize('index', [0, 1])
def test_named_agent_produces_own_margot_reviews_and_same_chat_receives(enrolled, own_agents, index, monkeypatch):
    identifier = own_agents[index]
    runtime = work.agent_profile_name('joshgarcia', identifier)
    mirrored = []
    monkeypatch.setattr(mirror, 'mirror_proactive_message', lambda *a, **k: mirrored.append((a, k)))
    with request('joshgarcia'):
        assert {r['id'] for r in json.loads(work.list_agents({}))['agents']} == set(own_agents)
        for staff in ['grace', 'scarlett', 'claire', 'margot']:
            with pytest.raises(PermissionError):
                work.agent(staff)
        created = json.loads(work.create({'agent': identifier, 'title': '[QA TEST] own agent', 'request': 'Draft a document.'}))
    tid = created['task_id']
    with work._connect(work.database('josh-home'), write=True) as conn:
        record, task, goal, binding = work.verify_task(conn, tid)
        assert task['assignee'] == task['owner_profile'] == runtime
        assert goal['required_reviewer_profile'] == 'joshgarcia'
        assert record['reviewer']['owner'] == 'joshgarcia'
        assert work.worker_policy(tid, 'josh-home', runtime)['principal'] == 'joshgarcia'
        maker = kb.claim_task(conn, tid, claimer=runtime + ':fixture')
        assert kb.request_review(conn, tid, summary='Named agent drafted the document.', reviewer='joshgarcia',
            expected_run_id=maker.current_run_id,
            metadata={'owner_cell_artifact': {'ref': 'fixture-document', 'digest': 'fixture-digest'}})
        reviewer = kb.claim_review_task(conn, tid, claimer='joshgarcia:fixture')
        assert reviewer is not None
        kb._set_worker_pid(conn, tid, os.getpid())
        assert work.worker_policy(tid, 'josh-home', 'joshgarcia')['principal'] == 'joshgarcia'
        with pytest.raises(PermissionError):
            work.worker_policy(tid, 'josh-home', 'grace')
        assert kb.complete_task(conn, tid, summary='Own Margot verified this document.',
            expected_run_id=reviewer.current_run_id, review_decision_capability=reviewer.review_decision_capability,
            metadata={'owner_cell_verdict': {'state_version': 1, 'artifact_ref': 'fixture-document',
                'artifact_digest': 'fixture-digest', 'verdict': 'PASS', 'defects': []}}, fire_lifecycle_hook=False)
        assert replies.pump(conn) == 1
        assert replies.pump(conn) == 0
        result = replies.read(conn, owner='joshgarcia', session_id='fixture-session', run_id='fixture-run')
        assert len(result) == 1 and result[0]['task_id'] == tid
        assert [r[0] for r in conn.execute('SELECT profile FROM task_runs WHERE task_id=? ORDER BY id', (tid,))] == [runtime, 'joshgarcia']
    assert mirrored[0][1]['session_id'] == 'fixture-session'

def test_missing_own_reviewer_never_falls_back(enrolled, own_agents):
    root, _, _ = enrolled
    (root / 'profiles/joshgarcia/config.yaml').unlink()
    with request('joshgarcia'), pytest.raises(FileNotFoundError):
        work.create({'agent': own_agents[0], 'title': '[QA TEST] no reviewer', 'request': 'Draft.'})
    with work._connect(work.database('josh-home')) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0

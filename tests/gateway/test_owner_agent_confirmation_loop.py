"""Josh iMessage yes -> ask-again loop (t_2a5d2aa5).

Real handlers, real sqlite stores, real native task creation. Only enrollment
is a fixture (same as test_member_owner_delegation). No model, no network.
Each turn is a fresh photon message in the same DM, history=0, exactly like
the production photon ingress.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3

import pytest

from gateway import owner_delegation as work
from gateway import owner_agent_creation as creation
from gateway import josh_board_authority as scope
from tests.gateway.test_member_owner_delegation import enrolled  # noqa: F401  (fixture)

SENDER = '+15550000001'
MORGAN = dict(name='Morgan',
              purpose='Deal Strategist for offer, objections, case studies and sales approach, separate from Amanda',
              title='Morgan deal strategy', request='Prepare the offer and objection handling plan.')


@contextmanager
def imessage(principal, n, text, sender=SENDER):
    run = f'run-{n}'
    ident = dict(platform='photon', chat_type='dm', sender=sender, conversation='any;-;' + sender,
                 message=f'spc-msg-{n}', session=run)
    claim = dict(run=run, identity=ident, digest=hashlib.sha256(text.encode()).hexdigest())
    with scope.execution_scope(principal, principal):
        with work.bind(principal, principal, run, run, text, claim=claim):
            yield


def agents(root, owner='joshgarcia'):
    with sqlite3.connect(root / 'agent-os/owner-agents.sqlite') as c:
        return [r[0] for r in c.execute('SELECT name FROM owner_agents WHERE owner=? ORDER BY name', (owner,))]


def tasks(board):
    with work._connect(work.database(board)) as c:
        return c.execute('SELECT count(*) FROM tasks').fetchone()[0]


@pytest.fixture(autouse=True)
def _transport_enrollment(enrolled, monkeypatch):  # noqa: F811
    # Only the phone->owner enrollment lookup is a fixture (as the base
    # fixture already does for app enrollment). Each sender maps to its owner.
    from plugins.platforms.photon.secure_intake import canonical_enrollment as ce
    owners = {SENDER: 'joshgarcia', '+15550000002': 'joshgarcia', '+15550000009': 'connor'}
    monkeypatch.setattr(ce, 'canonical_transport_enrollment', lambda identity: {'owner': owners[identity['sender']]})


def _seed_amanda(root):
    with sqlite3.connect(root / 'agent-os/owner-agents.sqlite') as c:
        c.execute("INSERT INTO owner_agents VALUES('oa_amanda','joshgarcia','Amanda','Existing researcher',NULL,1,'fixture')")


def test_reproduces_and_fixes_yes_then_ask_again(enrolled):
    root, _cat, _env = enrolled
    _seed_amanda(root)
    with imessage('joshgarcia', 1, 'Can we get started on the sales plan?'):
        asked = json.loads(creation.propose(MORGAN))
    assert asked['created'] is False
    # Turn 2: fresh turn, no history. Before the fix the model only saw
    # own_agents_list (no pending question) and re-proposed -> asked again.
    with imessage('joshgarcia', 2, 'Yes'):
        listed = json.loads(work.list_agents({}))
        assert listed['pending_proposal']['proposal_id'] == asked['proposal_id']
        assert listed['pending_proposal']['name'] == 'Morgan' and listed['pending_proposal']['reply_is_yes']
        # Even if the model re-proposes (the observed failure), the server
        # resolves it to the existing question and executes it.
        result = json.loads(creation.propose(MORGAN))
    assert result['created'] is True and result['accepted'] is True and result['agent_name'] == 'Morgan'
    assert agents(root) == ['Amanda', 'Grace', 'Morgan', 'Scarlett']
    with work._connect(work.database('josh-home')) as conn:
        record, task, _goal, binding = work.verify_task(conn, result['task_id'])
        assert task['created_by'] == binding['principal'] == 'joshgarcia'
        assert task['assignee'] == result['profile'] and record['selected']['name'] == 'Morgan'
    with sqlite3.connect(root / 'agent-os/owner-agents.sqlite') as c:
        assert c.execute("SELECT name,purpose FROM owner_agents WHERE id='oa_amanda'").fetchone() == ('Amanda', 'Existing researcher')
        assert c.execute("SELECT count(*) FROM owner_agents WHERE owner='chris' AND name='Morgan'").fetchone()[0] == 0


def test_repeated_yes_is_idempotent_and_truthful(enrolled):
    root, *_ = enrolled
    with imessage('joshgarcia', 1, 'start'):
        pid = json.loads(creation.propose(MORGAN))['proposal_id']
    with imessage('joshgarcia', 2, 'Yes'):
        first = json.loads(creation.confirm({'proposal_id': pid}))
    with imessage('joshgarcia', 3, 'Yes'):
        assert 'pending_proposal' not in json.loads(work.list_agents({}))
        again = json.loads(creation.confirm({'proposal_id': pid}))
    assert again['already_created'] and again['task_id'] == first['task_id'] and again['agent_id'] == first['agent_id']
    assert agents(root).count('Morgan') == 1 and tasks('josh-home') == 1


def test_wrong_name_other_owner_and_negation_rejected(enrolled):
    root, *_ = enrolled
    with imessage('joshgarcia', 1, 'start'):
        pid = json.loads(creation.propose(MORGAN))['proposal_id']
    with imessage('joshgarcia', 2, 'yes Marco'):
        assert json.loads(work.list_agents({}))['pending_proposal']['reply_is_yes'] is False
        with pytest.raises(creation.ConfirmationDenied):
            creation.confirm({'proposal_id': pid})
    with imessage('joshgarcia', 3, 'no wait'):
        with pytest.raises(creation.ConfirmationDenied):
            creation.confirm({'proposal_id': pid})
    with imessage('connor', 4, 'Yes', sender='+15550000009'):
        with pytest.raises(creation.ConfirmationDenied, match='No agent proposal'):
            creation.confirm({'proposal_id': pid})
    # A different DM of the same owner cannot confirm either (channel scope).
    with imessage('joshgarcia', 5, 'Yes', sender='+15550000002'):
        with pytest.raises(creation.ConfirmationDenied, match='No agent proposal'):
            creation.confirm({'proposal_id': pid})
    assert 'Morgan' not in agents(root) and 'Morgan' not in agents(root, 'connor')
    # A different agent proposed with "yes" does not consume the Morgan consent.
    with imessage('joshgarcia', 6, 'Yes'):
        other = json.loads(creation.propose(dict(MORGAN, name='Sloan')))
    assert other['created'] is False and 'Sloan' not in agents(root)


def test_routing_failure_does_not_reprompt_consent(enrolled, monkeypatch):
    root, *_ = enrolled
    with imessage('joshgarcia', 1, 'start'):
        pid = json.loads(creation.propose(MORGAN))['proposal_id']
    real = work.create
    monkeypatch.setattr(work, 'create', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('board down')))
    with imessage('joshgarcia', 2, 'Yes'):
        failed = json.loads(creation.confirm({'proposal_id': pid}))
    assert failed['accepted'] is False and failed['routing_error'] == 'RuntimeError'
    assert 'do not ask for approval again' in failed['message'] and agents(root).count('Morgan') == 1
    monkeypatch.setattr(work, 'create', real)
    with imessage('joshgarcia', 3, 'Yes'):
        assert 'pending_proposal' not in json.loads(work.list_agents({}))
        retried = json.loads(creation.confirm({'proposal_id': pid}))
    assert retried['accepted'] is True and retried['task_id'] and agents(root).count('Morgan') == 1


def test_explicit_named_directive_creates_and_assigns_without_another_yes(enrolled):
    root, *_ = enrolled
    with imessage('joshgarcia', 41, 'Yes set up Morgan as the deal strategist'):
        result=json.loads(creation.propose(MORGAN))
    assert result['created'] and result['accepted']
    with work._connect(work.database('josh-home')) as conn:
        record, task, goal, binding=work.verify_task(conn,result['task_id'])
        assert task['assignee']==result['profile']
        assert binding['principal']=='joshgarcia'
        assert record['selected']['name']=='Morgan'
    assert agents(root).count('Morgan')==1


def test_directives_keep_questions_negation_and_other_names_out():
    for request in ['Yes set up Morgan as the deal strategist','Please create Morgan','Can you create Morgan for sales?']:
        assert creation.creation_directive(request,'Morgan')
    for request in ['Should we create Morgan?','Do not create Morgan','Maybe create Morgan','Create Amanda','Yes']:
        assert not creation.creation_directive(request,'Morgan')


def test_existing_agent_is_reused_without_pending_approval(enrolled):
    root, *_ = enrolled
    with imessage('joshgarcia', 41, 'Please create Morgan'):
        first=json.loads(creation.propose(MORGAN))
    with imessage('joshgarcia', 42, 'Please create Morgan'):
        again=json.loads(creation.propose(MORGAN))
        assert 'pending_proposal' not in json.loads(work.list_agents({}))
    assert again['already_created'] and again['agent_id']==first['agent_id']
    assert agents(root).count('Morgan')==1 and tasks('josh-home')==1

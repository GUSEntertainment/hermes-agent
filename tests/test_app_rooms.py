import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT if (ROOT / 'gateway/hosted_rooms.py').exists() else Path('/Users/Chris/.hermes/releases/hermes-agent-20260925-private-delivery-proof-v57')
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location('app_rooms_under_test', ROOT / 'gateway/platforms/api_server_app_rooms.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FakeService:
    def __init__(self, db):
        self.db_path = db
        self.sent = []
        self.stopped = False

    def create_room(self, **kw):
        return app.hosted_rooms.create_room(self.db_path, room_id=kw['room_id'], name=kw['name'], members=kw['members'], authority_gateway_id=app.hosted_rooms.local_authority_gateway_id())

    def _events(self, room_id):
        return app.hosted_rooms.read_events(self.db_path, room_id=room_id)['events']

    def status(self, room_id):
        return {'working': False, 'blocked': False}

    def send(self, **kw):
        room = app.hosted_rooms.room_state(self.db_path, room_id=kw['room_id'])
        self.sent.append(kw)
        return app.hosted_rooms.append_event(self.db_path, room_id=kw['room_id'], event_id=kw['event_id'], kind='message.user', actor={'kind': 'user', 'id': 'desktop'}, payload=kw['payload'], authority_gateway_id=room['authority_gateway_id'], authority_epoch=room['authority_epoch'])

    def stop_room(self, *args, **kw):
        self.stopped = True
        return 0

    def wakeup(self):
        pass


class Request(dict):
    def __init__(self, principal, body=None, room_id=None):
        super().__init__()
        self.principal = principal
        self.body = body
        self.match_info = {'id': room_id}
        self.headers = {'X-Username': 'victim'}


class Server:
    def _expected_api_key(self):
        return 'test-key-at-least-sixteen-chars'

    config = types.SimpleNamespace(extra={'app_rooms': {'enabled': True, 'requesters': {
        'alice': {'agents': {'ada': {'profile': 'default', 'name': 'Ada'}, 'bob': {'profile': 'bob', 'name': 'Bob'}}},
        'eve': {'agents': {'eve': {'profile': 'eve', 'name': 'Eve'}}},
    }}})

    def _check_auth(self, request):
        if request.principal is not None:
            request['hermes.authenticated_principal'] = request.principal
        return None

    async def _read_json_body(self, request):
        return request.body, None


def call(server, action, principal, body=None, room_id=None):
    response = asyncio.run(app._handle(server, Request(principal, body, room_id), action))
    return response.status, json.loads(response.text)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_approval_ready', lambda: True)
    db = tmp_path / 'rooms.db'
    monkeypatch.setattr(app, '_db', lambda: db)
    service = FakeService(db)
    monkeypatch.setattr(app, '_service', lambda self, path: service)
    monkeypatch.setattr(app.hosted_rooms, 'local_authority_gateway_id', lambda: 'test-gateway')
    return Server(), service, db


def test_create_reload_exchange_interject_stop_and_scope(fixture):
    server, service, db = fixture
    room_id = str(uuid.uuid4())
    status, result = call(server, 'create', 'alice', {'id': room_id, 'name': 'Team', 'agents': ['ada', 'bob']})
    assert status == 201, result
    assert result['room']['agents'] == [{'id': 'ada', 'name': 'Ada'}, {'id': 'bob', 'name': 'Bob'}]
    assert call(server, 'list', 'alice')[1]['rooms'][0]['id'] == room_id
    assert call(server, 'get', 'alice', room_id=room_id)[1]['room']['id'] == room_id
    assert call(server, 'get', 'eve', room_id=room_id)[0] == 404
    assert call(server, 'message', 'eve', {'id': str(uuid.uuid4()), 'text': 'steal'}, room_id)[0] == 404
    assert call(server, 'create', 'eve', {'id': room_id, 'name': 'Team', 'agents': ['eve', 'ada']})[0] in (400, 404)
    assert call(server, 'create', 'alice', {'id': str(uuid.uuid4()), 'name': 'bad', 'agents': ['ada', 'eve']})[0] == 400
    assert call(server, 'create', 'alice', {'id': str(uuid.uuid4()), 'name': 'bad', 'agents': ['ada', 'bob'], 'profile': 'eve'})[0] == 400
    assert call(server, 'agents', None)[0] == 403
    original_key = server._expected_api_key
    server._expected_api_key = lambda: ''
    assert call(server, 'agents', 'alice')[0] == 403
    server._expected_api_key = original_key
    assert call(server, 'create', None, {'id': str(uuid.uuid4()), 'name': 'bad', 'agents': ['ada', 'bob']})[0] == 403
    for text in ('start', 'interjection'):
        assert call(server, 'message', 'alice', {'id': str(uuid.uuid4()), 'text': text}, room_id)[0] == 200
    room = app.hosted_rooms.room_state(db, room_id=room_id)
    for slug in ('ada', 'bob'):
        app.hosted_rooms.append_event(db, room_id=room_id, event_id=f'member-{slug}', kind='message.member', actor={'kind': 'member', 'id': slug}, payload={'text': slug, 'member_id': slug}, authority_gateway_id=room['authority_gateway_id'], authority_epoch=room['authority_epoch'])
    detail = call(server, 'get', 'alice', room_id=room_id)[1]
    assert [m['agent'] for m in detail['messages']] == [None, None, 'ada', 'bob']
    assert call(server, 'stop', 'alice', {}, room_id)[1]['status'] == 'stopped'
    assert call(server, 'message', 'alice', {'id': str(uuid.uuid4()), 'text': 'no'}, room_id)[1]['status'] == 'stopped'
    assert call(server, 'resume', 'eve', {}, room_id)[0] == 404
    assert call(server, 'resume', 'alice', {}, room_id)[1]['status'] == 'idle'


def test_turn_reservation_cap_does_not_reset_on_interjection(fixture):
    server, service, db = fixture
    room_id = str(uuid.uuid4())
    call(server, 'create', 'alice', {'id': room_id, 'name': 'Team', 'agents': ['ada', 'bob']})
    with app._connect(db) as conn:
        conn.execute('UPDATE app_room_ownership SET reserved_turns=12,paused=1 WHERE room_id=?', (room_id,))
    assert call(server, 'message', 'alice', {'id': str(uuid.uuid4()), 'text': 'interject'}, room_id)[1]['status'] == 'paused'
    assert call(server, 'get', 'alice', room_id=room_id)[1]['status'] == 'paused'
    assert call(server, 'resume', 'alice', {}, room_id)[0] == 200
    with app._connect(db) as conn:
        assert conn.execute('SELECT reserved_turns,turn_limit FROM app_room_ownership WHERE room_id=?', (room_id,)).fetchone()[:] == (12, 24)
        conn.execute('UPDATE app_room_ownership SET reserved_turns=48,paused=1 WHERE room_id=?', (room_id,))
    assert call(server, 'resume', 'alice', {}, room_id)[0] == 400


def test_unsafe_approval_connection_is_refused(fixture, monkeypatch):
    server, _, _ = fixture
    monkeypatch.setattr(app, '_approval_ready', lambda: False)
    assert call(server, 'agents', 'alice')[0] == 503
    assert call(server, 'list', 'alice')[0] == 503
    assert call(server, 'create', 'alice', {'id': str(uuid.uuid4()), 'name': 'No bypass', 'agents': ['ada', 'bob']})[0] == 503

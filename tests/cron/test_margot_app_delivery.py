import sqlite3

import pytest

from cron import margot_app_delivery as mad
from cron.scheduler_delivery import _resolve_delivery_targets

T1 = "11111111-2222-4333-8444-555555555555"
T2 = "aaaaaaaa-2222-4333-8444-555555555555"


@pytest.fixture
def store(tmp_path, monkeypatch):
    p = tmp_path / "c.sqlite"
    db = sqlite3.connect(p)
    db.executescript("""
    CREATE TABLE conversations(id TEXT PRIMARY KEY, owner TEXT, domain TEXT, title TEXT, binding TEXT,
      sessionId TEXT UNIQUE, createdAt INT, updatedAt INT, agent TEXT DEFAULT 'margot', archivedAt INT, space TEXT);
    CREATE TABLE turns(id TEXT PRIMARY KEY, conversationId TEXT, message TEXT, source TEXT, state TEXT, response TEXT,
      error TEXT, model TEXT, runId TEXT, payload TEXT, fingerprint TEXT, createdAt INT, updatedAt INT,
      approval TEXT, replyTo TEXT, threading TEXT);
    CREATE TABLE conversation_notices(id TEXT PRIMARY KEY, owner TEXT, conversationId TEXT, turnId TEXT, kind TEXT,
      summary TEXT, createdAt INT, seenAt INT, spokenAt INT, voiceClaim TEXT, voiceClaimUntil INT DEFAULT 0, UNIQUE(kind,turnId));
    """)
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,0,0,'margot',NULL,NULL)",
               (T1, "chris", "gus", "Test", "b", "app_gus_" + T1.replace("-", "")))
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,0,0,'margot',NULL,NULL)",
               (T2, "connor", "gus", "Other", "b", "app_gus_" + T2.replace("-", "")))
    db.commit(); db.close()
    monkeypatch.setenv("HERMES_MARGOT_APP_STORE", str(p))
    return p


def _job(**kw):
    j = {"id": "j1", "origin": mad.origin_for_session("app_gus_" + T1.replace("-", ""))}
    j.update(kw)
    return j


def counts(p):
    db = sqlite3.connect(p)
    r = (db.execute("SELECT count(*) FROM turns").fetchone()[0],
         db.execute("SELECT count(*) FROM conversation_notices WHERE kind='work'").fetchone()[0])
    db.close()
    return r


def test_origin_resolves_to_app_thread(store):
    job = _job(deliver="origin")
    assert job["origin"]["platform"] == "margot-app" and job["origin"]["thread_id"] == T1
    t = _resolve_delivery_targets(job)
    assert t[0]["platform"] == "margot-app" and t[0]["chat_id"] == "chris" and t[0]["thread_id"] == T1


def test_posts_once_and_dedupes(store):
    job = _job(execution_id="e1")
    assert mad.deliver(job, "Britt says hi", "chris", T1) is None
    assert mad.deliver(job, "Britt says hi", "chris", T1) is None
    assert counts(store) == (1, 1)


def test_owner_bound(store):
    job = _job(execution_id="e2")
    assert "not the job's creator" in mad.deliver(job, "x", "connor", T2)
    assert "not owned" in mad.deliver(job, "x", "chris", T2)
    assert "requires" in mad.deliver({"id": "j", "origin": None}, "x", "chris", T1)
    assert counts(store) == (0, 0)


def test_bad_token():
    with pytest.raises(ValueError):
        mad.parse_token("margot-app:chris")
    assert mad.parse_token("telegram:1") is None

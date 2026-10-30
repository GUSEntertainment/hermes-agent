"""Real bundled startup synchronization occurs before native admission signing."""
import os
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home=tmp_path/'home'; profile=home/'profiles'/'worker'
    profile.mkdir(parents=True)
    (home/'config.yaml').write_text('{}\n')
    (profile/'config.yaml').write_text('model:\n  provider: anthropic\n  default: claude-test\nplatform_toolsets:\n  cli: [file]\n')
    bundle=tmp_path/'bundle'; source=bundle/'review-fixture'/'SKILL.md'
    source.parent.mkdir(parents=True)
    source.write_text('---\nname: review-fixture\ndescription: Review fixture\n---\nOld supported procedure.\n')
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(home/'kanban.db'))
    monkeypatch.setenv('HERMES_BUNDLED_SKILLS',str(bundle))
    monkeypatch.setenv('HERMES_OPTIONAL_SKILLS',str(tmp_path/'no-optional-skills'))
    for key in ('HERMES_PROFILE','HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_SESSION_SOURCE','HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key,raising=False)
    env=dict(os.environ,HERMES_HOME=str(profile),HERMES_PROFILE='worker')
    kb._prepend_active_source_to_pythonpath(env,honor_explicit_hermes_bin=False)
    def startup():
        subprocess.run([sys.executable,*kb._python_safe_path_flag(),'-c',kb._WORKER_SKILL_SYNC_CODE],
                       env=env,capture_output=True,text=True,check=True,timeout=30)
    startup()
    # Simulate the release update that Grace first encountered after claim.
    source.write_text(source.read_text().replace('Old supported','New supported'))
    # This fixture is an unconfined native company task, not a protected
    # member/private route. Never query installed real owner ledgers from tests.
    from gateway import member_worker_policy
    monkeypatch.setattr(member_worker_policy,'select',lambda *a,**kw:None)
    # Catalog discovery has separate protected-owner-ledger dependencies.
    # Keep only that existing seam synthetic; skill synchronization, profile
    # file I/O, native admission hashing and revocation remain real.
    probe={'provider':'anthropic','model':'claude-test','fallbacks':[],
           'toolsets':['file'],'schema_tools':['read_file'],'loaded':[],
           'missing':[],'unavailable':[],'privacy':{},'workspace_confinement':False,
           'workspace_read_only_roots':[],'available_providers':['anthropic'],'readiness':None}
    monkeypatch.setattr(kb,'_WORKER_ADMISSION_CODE','print('+repr(json.dumps(probe))+')')
    monkeypatch.setattr(kb,'_fire_kanban_lifecycle_hook',lambda *a,**kw:None)
    with kb.connect_closing() as conn:
        tid=kb.create_task(conn,title='Independent review',body='Review the saved local deliverable.',assignee='worker')
        yield conn,kb.get_task(conn,tid),home,profile,source,startup


def test_native_admission_seals_synced_profile_and_startup_is_idempotent(board):
    conn,task,home,profile,source,startup=board
    before=kb._profile_config_digest(str(profile))
    admission=kb._build_worker_admission(task,'worker')
    assert admission.profile_config_digest!=before
    assert (profile/'skills/review-fixture/SKILL.md').read_bytes()==source.read_bytes()
    assert not (home/'skills/review-fixture/SKILL.md').exists()
    assert kb._admission_still_current(task,admission)
    startup()
    assert kb._profile_config_digest(str(profile))==admission.profile_config_digest
    assert kb._admission_still_current(task,admission)
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0


@pytest.mark.parametrize('relative',['skills/review-fixture/SKILL.md','config.yaml','.env'])
def test_real_policy_changes_after_signing_still_revoke_admission(board,relative):
    _,task,_,profile,_,_=board
    admission=kb._build_worker_admission(task,'worker')
    path=profile/relative
    prior=path.read_text() if path.exists() else ''
    path.write_text(prior+'\n# Real policy change\n')
    assert not kb._admission_still_current(task,admission)


def test_sync_failure_stops_before_claim_or_discovery(board,monkeypatch):
    conn,task,_,_,_,_=board
    calls=[]
    def failed_sync(argv,**kw):
        calls.append(argv)
        assert argv[-1]==kb._WORKER_SKILL_SYNC_CODE
        return SimpleNamespace(returncode=1,stdout='',stderr='fixture synchronization failure')
    monkeypatch.setattr(kb.subprocess,'run',failed_sync)
    with pytest.raises(kb.WorkerAdmissionError):
        kb._build_worker_admission(task,'worker')
    assert len(calls)==1
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0
    assert kb.get_task(conn,task.id).current_run_id is None

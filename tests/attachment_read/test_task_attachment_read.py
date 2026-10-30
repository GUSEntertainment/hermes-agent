import hashlib,json,os,sqlite3,tempfile,shutil
from pathlib import Path
from dataclasses import dataclass,replace
import pytest
from tools import task_attachment_read as ar

@dataclass(frozen=True)
class Attachment:
 id:int;task_id:str;filename:str;stored_path:str;content_type:str;size:int
@pytest.fixture
def fixture(tmp_path,monkeypatch):
 root=Path(tempfile.mkdtemp(prefix='.attachment-test-',dir=Path.home())).resolve();db=root/'kanban.db';db.touch();task='t_fixture';folder=root/'attachments'/task;folder.mkdir(parents=True);file=folder/'result.txt';file.write_bytes(b'WORKSPACE_OK\n')
 attachment=Attachment(1,task,file.name,str(file),'text/plain',file.stat().st_size)
 class KB:
  def get_attachment(self,conn,id):return attachment if id==1 else None
 kb=KB();monkeypatch.setattr(ar,'_authority',lambda conn,tid:({'task':task},db))
 yield kb,task,file,attachment
 shutil.rmtree(root)

def test_text_bytes_hash(fixture):
 kb,task,file,a=fixture;r=ar.read_attachment(kb,None,task,1)
 assert r['text']=='WORKSPACE_OK\n' and r['sha256']==hashlib.sha256(file.read_bytes()).hexdigest()
 assert 'stored_path' not in r

def test_binary_hash_no_text(fixture,monkeypatch):
 kb,task,file,a=fixture;file.write_bytes(b'\xff\0binary');a=replace(a,size=file.stat().st_size,content_type='application/octet-stream');monkeypatch.setattr(kb,'get_attachment',lambda *args:a)
 r=ar.read_attachment(kb,None,task,1);assert r['text_omitted']=='binary' and 'text' not in r

def test_text_limit_returns_hash(fixture,monkeypatch):
 kb,task,file,a=fixture;file.write_bytes(b'x'*(ar.MAX_TEXT_BYTES+1));monkeypatch.setattr(kb,'get_attachment',lambda *args:replace(a,size=file.stat().st_size))
 assert ar.read_attachment(kb,None,task,1)['text_omitted']=='text_size_limit'

def test_file_cap_rejected(fixture,monkeypatch):
 kb,task,file,a=fixture;file.write_bytes(b'x'*(ar.MAX_BYTES+1));monkeypatch.setattr(kb,'get_attachment',lambda *args:replace(a,size=file.stat().st_size))
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,1)

@pytest.mark.parametrize('kind',['symlink','hardlink','directory_symlink'])
def test_link_rejected(fixture,kind):
 kb,task,file,a=fixture;outside=file.parents[2]/'outside';outside.write_bytes(file.read_bytes());file.unlink()
 if kind=='symlink':file.symlink_to(outside)
 elif kind=='hardlink':os.link(outside,file)
 else:
  directory=file.parent;directory.rmdir();directory.symlink_to(outside.parent,target_is_directory=True)
 with pytest.raises((PermissionError,OSError)):ar.read_attachment(kb,None,task,1)

def test_foreign_task_and_missing_id_rejected(fixture,monkeypatch):
 kb,task,file,a=fixture
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,'t_other',1)
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,2)
 for id in [True,0,-1,'1']:
  with pytest.raises(ValueError):ar.read_attachment(kb,None,task,id)

def test_row_path_and_size_cannot_grant_access(fixture,monkeypatch):
 kb,task,file,a=fixture;outside=file.parents[2]/'outside';outside.write_bytes(b'controlled')
 monkeypatch.setattr(kb,'get_attachment',lambda *args:replace(a,stored_path=str(outside)))
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,1)
 monkeypatch.setattr(kb,'get_attachment',lambda *args:replace(a,size=999))
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,1)

def test_revocation_after_read_never_returns_bytes(fixture,monkeypatch):
 kb,task,file,a=fixture;before=ar._authority;n=[0]
 def authority(*args):
  n[0]+=1
  if n[0]>1:raise PermissionError('revoked')
  return before(*args)
 monkeypatch.setattr(ar,'_authority',authority)
 with pytest.raises(PermissionError,match='revoked'):ar.read_attachment(kb,None,task,1)

def test_worker_requires_actual_live_admission(tmp_path,monkeypatch):
 import importlib.util
 from agent import private_worker_mode as pwm
 from hermes_cli import kanban_db as kb
 source=Path(__file__).parents[1]/'agent/test_private_worker_mode.py';spec=importlib.util.spec_from_file_location('private_fixture',source);f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
 pwm._reset_private_worker_contract_for_tests();conn,db,c,digest,run=f._live_claim(tmp_path,monkeypatch)
 kb._set_worker_pid(conn,c['task'],os.getpid())
 try:
  pwm.install_authenticated_worker_contract(c,durable_validator=pwm._default_durable_validator)
  assert ar._authority(conn,c['task'])[1]==db
  with pytest.raises(PermissionError):ar._authority(conn,'t_other')
  other=kb.connect(tmp_path/'other.db')
  try:
   with pytest.raises(PermissionError):ar._authority(other,c['task'])
  finally:other.close()
  from agent.delegation_context import delegated_child_context
  with delegated_child_context('fixture-child'):
   with pytest.raises(PermissionError):ar._authority(conn,c['task'])
 finally:conn.close();pwm._reset_private_worker_contract_for_tests()

def test_legacy_metadata_response_unchanged(monkeypatch):
 from tools import kanban_tools as kt
 class A:
  id=1;filename='file.txt';content_type='text/plain';size=1;uploaded_by='chris';stored_path='/existing/file.txt';created_at=1
 class KB:
  def get_task(self,*args):return object()
  def list_attachments(self,*args):return [A()]
 class Conn:
  def close(self):pass
 monkeypatch.setattr(kt,'_connect',lambda **kw:(KB(),Conn()))
 result=json.loads(kt._handle_attachments({'task_id':'t_chris'}));assert result['ok'] and result['attachments'][0]['stored_path']=='/existing/file.txt'


def test_fifo_rejected_without_blocking(fixture):
 kb,task,file,a=fixture;file.unlink();os.mkfifo(file)
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,1)

def test_world_writable_file_rejected(fixture):
 kb,task,file,a=fixture;file.chmod(0o666)
 with pytest.raises(PermissionError):ar.read_attachment(kb,None,task,1)

import json,types
from unittest.mock import patch
from gateway import password_recovery_tools as t

def test_rejects_model_selected_owner():
 with patch.object(t,'identity') as i:
  assert json.loads(t.invoke({'username':'someone-else'}))['state']=='unavailable'
  i.assert_not_called()
def test_missing_authority_never_issues():
 with patch.object(t,'identity',side_effect=PermissionError()),patch.object(t.subprocess,'run') as run:
  assert json.loads(t.invoke({}))['state']=='unavailable';run.assert_not_called()
def test_changed_authority_withholds_link(tmp_path):
 p=tmp_path/'script';p.write_text('')
 with patch.object(t,'SCRIPT',p),patch.object(t,'identity',side_effect=[({'owner':'josh'},{}),({'owner':'connor'},{})]),patch.object(t.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='{"url":"https://margot.gusentertainment.com/login/reset#token"}')):
  assert json.loads(t.invoke({}))['state']=='unavailable'
def test_verified_source_only_passed_to_issuer(tmp_path):
 p=tmp_path/'script';p.write_text('');identity=({'owner':'josh'},{'generation':1})
 with patch.object(t,'SCRIPT',p),patch.object(t,'identity',return_value=identity),patch.object(t.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='{"url":"https://margot.gusentertainment.com/login/reset#token","username":"joshgarcia"}')) as run:
  assert json.loads(t.invoke({}))['username']=='joshgarcia'
  assert json.loads(run.call_args.kwargs['input'])=={'identity':identity[0]}

def test_native_and_ordinary_registration_do_not_overwrite_each_other():
 from tools.registry import registry
 t.register();t.register('josh_status_pilot')
 assert registry.get_toolset_for_tool('margot_password_reset')=='kanban'
 assert registry.get_toolset_for_tool('owner_password_reset')=='josh_status_pilot'

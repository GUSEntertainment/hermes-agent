import ast
from pathlib import Path
from unittest.mock import patch
import types
import logging
import pytest

def actual_definition(available,permitted,native=False,requested=None,authorized=True):
 tree=ast.parse((Path(__file__).parents[2]/'model_tools.py').read_text())
 node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_tool_definitions')
 code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
 ns={'os':types.SimpleNamespace(environ={'HERMES_SESSION_SOURCE':'kanban'} if native else {}),'_compute_tool_definitions':lambda *a,**kw:[{'function':{'name':n}} for n in available]}
 ns['logger']=logging.getLogger(__name__)
 pilot=types.ModuleType('gateway.josh_pilot_tools');pilot.pilot_required=lambda:True;pilot.TOOLSET='josh_status_pilot';pilot.schema_names=lambda:permitted
 intake=types.ModuleType('gateway.owner_api_intake');intake.TOOL='owner_secure_intake'
 native_module=types.ModuleType('gateway.native_owner_board')
 def current():
  if not authorized:raise PermissionError('Unbound worker')
  return {'profile':'fixture','board':'fixture-home'}
 native_module.current=current
 with patch.dict('sys.modules',{'gateway.josh_pilot_tools':pilot,'gateway.owner_api_intake':intake,'gateway.native_owner_board':native_module}):
  exec(compile(ast.fix_missing_locations(code),'model_tools.py','exec'),ns)
  return ns['get_tool_definitions'](requested if requested is not None else ['josh_status_pilot'])

@pytest.mark.parametrize('active',[False,True,False])
def test_reset_registration_does_not_leak_or_block_next_run(active):
 base={'josh_task_status','own_work_list','owner_computer'};secure={'owner_secure_intake','owner_password_reset'}
 result=actual_definition(base|secure,base|secure if active else base)
 assert {x['function']['name'] for x in result}==(base|secure if active else base)

def test_unknown_tools_filtered_and_missing_tools_fail_closed():
 assert actual_definition({'own_work_list','unapproved'},{'own_work_list'}) == [{'function':{'name':'own_work_list'}}]
 with pytest.raises(PermissionError):actual_definition(set(),{'own_work_list'})


def test_native_worker_requires_own_proof_and_only_lifecycle_tools():
 names={'kanban_show','kanban_complete','kanban_block','kanban_heartbeat'}
 result=actual_definition(names|{'kanban_create','terminal'},set(),native=True,requested=['kanban'])
 assert {x['function']['name'] for x in result}==names
 with pytest.raises(PermissionError):actual_definition(names,set(),native=True,requested=['kanban'],authorized=False)
 with pytest.raises(PermissionError):actual_definition(names,set(),native=True,requested=['terminal'])
 with pytest.raises(PermissionError):actual_definition({'kanban_complete'},set(),native=True,requested=['kanban'])

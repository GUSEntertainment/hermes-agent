"""Activation preparation and rollback use disposable files and no services."""
import importlib.util
import json
from pathlib import Path
import plistlib

import pytest
import yaml

spec=importlib.util.spec_from_file_location('activate_personal_board',Path(__file__).parents[1]/'operational/activate_personal_board.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def staged(tmp_path,monkeypatch):
    home=tmp_path/'operator';root=home/'.hermes';packages=tmp_path/'packages'
    monkeypatch.setattr(installer,'HOME',home);monkeypatch.setattr(installer,'ROOT',root)
    monkeypatch.setattr(installer,'PACKAGE_ROOT',packages)
    monkeypatch.setattr(installer,'BASE_CONTROLLER',root/'releases/old-controller')
    package=packages/'reviewed';package.mkdir(parents=True)
    config=dict(toolsets=['owner_context'],platform_toolsets={'api_server':['owner_context']},
        platforms={'api_server':{'enabled':True,'extra':{'host':'127.0.0.1','port':56711}}},
        mcp_servers={'owner_context':{'command':'unchanged-reader','args':['private-consumer']}},
        model={'default':'unchanged-model'},memory={'preserve':True})
    soul='Existing policy. Read-only without verified work. Preserve private history.\n'
    launch=dict(Label=installer.LABEL,ProgramArguments=['/usr/bin/env','-i','HERMES_HOME='+str(root/'profiles/app-owner-chris-margot'),
        'PYTHONPATH='+str(root/'releases/old-gateway'),str(root/'releases/old-gateway/.venv/bin/python'),'-P','-m','hermes_cli.main','gateway','run'],
        WorkingDirectory=str(root/'releases/old-gateway'),KeepAlive=True)
    initial={'configuration':yaml.safe_dump(config).encode(),'soul':soul.encode(),
        'launchagent':plistlib.dumps(launch),'wrapper':('REL='+str(installer.BASE_CONTROLLER)+'\nexec fixture\n').encode()}
    for kind,path in installer.targets().items():
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(initial[kind]);path.chmod(0o700 if kind=='wrapper' else 0o600)
    monkeypatch.setattr(installer,'WRAPPER_SHA',installer.digest(initial['wrapper']))
    report={kind:dict(path=str(path),expected_before_sha256=installer.digest(initial[kind]))
            for kind,path in installer.targets().items() if kind!='wrapper'}
    report['soul'].update(old_sentence='Read-only without verified work.',new_sentence='Board controls when verified.',append='Only four owner tools.')
    manifest={'lanes':{}}
    for lane in ('gateway','controller'):
        candidate=root/'releases'/('new-'+lane);candidate.mkdir(parents=True)
        (candidate/'fixture.txt').write_text('reviewed candidate fixture\n')
        report[lane]=str(candidate)
        manifest['lanes'][lane]=dict(candidate=str(candidate),files=[dict(path='fixture.txt',after_sha256=installer.digest((candidate/'fixture.txt').read_bytes()))])
    runtime=root/'releases/runtime/.venv/bin/python';runtime.parent.mkdir(parents=True);runtime.write_text('fixture')
    report['runtime_python']=str(runtime)
    (package/'verification.json').write_text(json.dumps(report));(package/'manifest.json').write_text(json.dumps(manifest))
    return package,initial,config,report


def test_preparation_changes_exact_fields_and_preserves_private_runtime(staged):
    package,initial,original,report=staged
    _,selected,_,prepared=installer.prepare(package)
    current=yaml.safe_load(prepared['configuration'])
    assert current['mcp_servers']==original['mcp_servers']
    assert current['memory']==original['memory'] and current['model']==original['model']
    assert current['toolsets']==['owner_context','owner_board_control']
    assert current['platforms']['api_server']['extra']==dict(host='127.0.0.1',port=56711,cockpit_principal='chris')
    launcher=plistlib.loads(prepared['launchagent'])
    assert launcher['Label']==installer.LABEL and launcher['WorkingDirectory']==report['gateway']
    assert [a for a in launcher['ProgramArguments'] if a.startswith('HERMES_HOME=')]==[a for a in plistlib.loads(initial['launchagent'])['ProgramArguments'] if a.startswith('HERMES_HOME=')]
    assert all(path.read_bytes()==initial[kind] for kind,path in selected.items())


def test_changed_input_and_changed_candidate_are_rejected(staged):
    package,initial,_,report=staged
    soul=installer.targets()['soul'];soul.write_text('new operator instruction')
    with pytest.raises(ValueError,match='reviewed input'):installer.prepare(package)
    soul.write_bytes(initial['soul'])
    (Path(report['controller'])/'fixture.txt').write_text('unreviewed')
    with pytest.raises(ValueError,match='reviewed manifest'):installer.prepare(package)


def test_failed_start_restores_exact_files_without_touching_main(staged,monkeypatch):
    package,initial,_,_=staged
    monkeypatch.setattr(installer,'credential',lambda:'fixture-key')
    monkeypatch.setattr(installer,'stopped',lambda deadline:None)
    starts=[]
    def restart(path,key,*,expect_controls,deadline=None):
        assert path==installer.targets()['launchagent']
        starts.append(expect_controls)
        if expect_controls:raise TimeoutError('fixture startup failure')
    monkeypatch.setattr(installer,'restart',restart)
    with pytest.raises(RuntimeError,match='prior files restored'):installer.apply(package)
    assert starts==[True,False]
    assert all(path.read_bytes()==initial[kind] for kind,path in installer.targets().items())
    receipt=next((installer.ROOT/'private/board-owner-control/personal-activations').glob('*/receipt.json'))
    assert json.loads(receipt.read_text())['status']=='rolled_back'
    assert receipt.stat().st_mode&0o077==0


def test_success_then_explicit_rollback_is_reversible(staged,monkeypatch):
    package,initial,_,_=staged
    monkeypatch.setattr(installer,'credential',lambda:'fixture-key')
    monkeypatch.setattr(installer,'stopped',lambda deadline:None)
    monkeypatch.setattr(installer,'restart',lambda *a,**kw:None)
    installer.apply(package)
    receipt=next((installer.ROOT/'private/board-owner-control/personal-activations').glob('*/receipt.json'))
    assert json.loads(receipt.read_text())['status']=='active'
    installer.restore(receipt)
    assert all(path.read_bytes()==initial[kind] for kind,path in installer.targets().items())

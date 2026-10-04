#!/usr/bin/env python3
"""Switch only Personal's code path, retaining config, credentials and identity."""
import argparse, hashlib, json, plistlib, time
from pathlib import Path
from activate_personal_board import read,atomic,restart,credential,activation_lock,HOME,ROOT,LABEL

PLIST=HOME/'Library/LaunchAgents'/f'{LABEL}.plist'

def digest(value):return hashlib.sha256(value).hexdigest()

def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--apply',action='store_true');p.add_argument('--rollback',action='store_true');a=p.parse_args()
 m=json.loads(read(a.manifest)[0]);lane=m['lanes']['gateway']
 BASE=Path(lane['baseline']);TARGET=Path(lane['candidate'])
 for runtime in (BASE,TARGET):
  if runtime.parent!=ROOT/'releases' or runtime.resolve()!=runtime or not runtime.is_dir() or not runtime.name.startswith('hermes-agent-'):
   raise ValueError('Canonical installed runtime pair required')
 if BASE==TARGET:raise ValueError('Distinct runtime pair required')
 RECEIPT=ROOT/'private/board-owner-control'/(TARGET.name.removeprefix('hermes-agent-')+'-personal')/'receipt.json'
 with activation_lock():
  if a.rollback:
   r=json.loads(read(RECEIPT)[0]);old=read(RECEIPT.parent/'launchagent.before')[0];now,mode=read(PLIST)
   if digest(old)!=r['before_sha256'] or digest(now) not in (r['before_sha256'],r['after_sha256']):raise ValueError('Rollback input changed')
   atomic(PLIST,old,mode);restart(PLIST,credential(),expect_controls=True);r['status']='rolled_back';atomic(RECEIPT,json.dumps(r,indent=2).encode()+b'\n',0o600);print(json.dumps({'status':'rolled_back'}));return
  for entry in lane['files']:
   file=TARGET/entry['path']
   if not file.resolve().is_relative_to(TARGET) or digest(read(file)[0])!=entry['after_sha256']:raise ValueError('Candidate drift')
  old,mode=read(PLIST);value=plistlib.loads(old);assert value['Label']==LABEL
  args=value['ProgramArguments'];matches=[i for i,v in enumerate(args) if v=='PYTHONPATH='+str(BASE)]
  if len(matches)!=1 or value['WorkingDirectory']!=str(BASE):raise ValueError('Unexpected prior Personal runtime')
  args[matches[0]]='PYTHONPATH='+str(TARGET);value['WorkingDirectory']=str(TARGET);new=plistlib.dumps(value)
  check=plistlib.loads(new);check['ProgramArguments'][matches[0]]='PYTHONPATH='+str(BASE);check['WorkingDirectory']=str(BASE);assert check==plistlib.loads(old)
  r={'before_sha256':digest(old),'after_sha256':digest(new),'service':LABEL,'baseline':str(BASE),'target':str(TARGET),'status':'prepared','manifest':str(a.manifest),'changed':['ProgramArguments.PYTHONPATH','WorkingDirectory']}
  if not a.apply:print(json.dumps(r));return
  if RECEIPT.exists():raise ValueError('Activation already attempted; inspect receipt')
  RECEIPT.parent.mkdir(mode=0o700,parents=True,exist_ok=True);atomic(RECEIPT.parent/'launchagent.before',old,0o600);atomic(RECEIPT,json.dumps(r,indent=2).encode()+b'\n',0o600)
  assert read(PLIST)[0]==old
  try:
   atomic(PLIST,new,mode);print(json.dumps({'status':'reloading_personal'}),flush=True);restart(PLIST,credential(),expect_controls=True)
  except Exception:
   if digest(read(PLIST)[0])!=r['after_sha256']:raise RuntimeError('Personal changed during activation; manual review required')
   atomic(PLIST,old,mode);restart(PLIST,credential(),expect_controls=True);r['status']='rolled_back';atomic(RECEIPT,json.dumps(r,indent=2).encode()+b'\n',0o600);raise
  r['status']='active';r['completed_at']=time.time();atomic(RECEIPT,json.dumps(r,indent=2).encode()+b'\n',0o600);print(json.dumps({'status':'active','service':LABEL,'receipt':str(RECEIPT)}))
if __name__=='__main__':main()

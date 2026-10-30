"""Prove a native worker's identity from its existing admitted app task."""
import os,json,subprocess,sys,re
from pathlib import Path
PROCESSORS='/Users/Chris/.hermes/context-runtimes/app-work-20260924-v2/processors'
CHECK=r'''
import sys,json,sqlite3,hashlib,stat,os
from pathlib import Path
root=Path(sys.argv[1]).parent
raw=(root/'BUNDLE.json').read_bytes()
if hashlib.sha256(raw).hexdigest()!='cc12054bd9a63c2edef2b9caa1556a70804d342ebd58cf26506eacd8e5556afe':raise ValueError('Bundle changed')
for name,digest in json.loads(raw)['files'].items():
 p=root/name;st=p.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o022 or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Bundle changed')
sys.path.insert(0,sys.argv[1])
from phone_app_work import readonly,validate_app_link
from phone_work import DEFAULT_PHONE_DB
profile,board,task,db=sys.argv[2:]
with readonly(DEFAULT_PHONE_DB) as c:
 rows=c.execute('SELECT * FROM phone_task_links WHERE task_id=? AND board=?',(task,board)).fetchall()
 if len(rows)!=1:raise ValueError('Bound task required')
 row=dict(rows[0]);validate_app_link(row)
 if row['runtime_profile']!=profile or row['owner_profile']!=profile or row['authority_realm']!='owner:'+profile:raise ValueError('Worker scope mismatch')
with readonly(db) as c:
 t=c.execute('SELECT assignee,runtime_profile,owner_profile,security_realm,idempotency_key FROM tasks WHERE id=?',(task,)).fetchone()
 if not t or t['assignee']!=profile or t['runtime_profile']!=profile or t['owner_profile']!=profile or t['security_realm']!='owner:'+profile or t['idempotency_key']!=row['idempotency_key']:raise ValueError('Native task mismatch')
print('verified')
'''
def current():
 from hermes_constants import get_hermes_home
 from gateway.owner_work_enrollment import current as enrolled
 home=Path(get_hermes_home());profile=home.name
 if home.parent.name!='profiles' or os.environ.get('HERMES_PROFILE')!=profile or os.environ.get('HERMES_SESSION_SOURCE')!='kanban':raise PermissionError('Native owner scope missing')
 binding=enrolled(profile);board=binding['board'];task=os.environ.get('HERMES_KANBAN_TASK','')
 expected=Path('/Users/Chris/.hermes/kanban/boards')/board/'kanban.db'
 if not re.fullmatch(r't_[a-zA-Z0-9_]+',task) or os.environ.get('HERMES_KANBAN_DB')!=str(expected) or expected.resolve(strict=True)!=expected:raise PermissionError('Native owner task missing')
 r=subprocess.run([sys.executable,'-I','-c',CHECK,PROCESSORS,profile,board,task,str(expected)],capture_output=True,text=True,timeout=10,env={})
 if r.returncode or r.stdout.strip()!='verified':raise PermissionError('Native owner task not authorized')
 return binding

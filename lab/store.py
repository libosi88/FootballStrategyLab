"""Local SQLite registry. One worker, transactional status, immutable run configs."""
import sqlite3,time,uuid,os
from contextlib import contextmanager
from pathlib import Path
from .common import *

def resumability(job,current_engine=None):
 current=current_engine or source_fingerprint()
 if job['config'].get('engine_hash')!=current:return {'resumable':False,'resume_block_reason':'SOURCE_REVISION_MISMATCH'}
 allowed=job['status'] in ('PAUSED','ERROR','INTERRUPTED')
 return {'resumable':allowed,'resume_block_reason':None if allowed else 'JOB_NOT_IN_RESUMABLE_STATE'}

def process_birth(pid):
 """PID plus OS creation time distinguishes a worker from a recycled PID."""
 if os.name=='nt':
  import ctypes
  from ctypes import wintypes
  k=ctypes.WinDLL('kernel32',use_last_error=True)
  k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);k.OpenProcess.restype=wintypes.HANDLE
  k.CloseHandle.argtypes=(wintypes.HANDLE,)
  k.GetProcessTimes.argtypes=(wintypes.HANDLE,)+(ctypes.POINTER(wintypes.FILETIME),)*4
  handle=k.OpenProcess(0x1000,False,pid)
  if not handle:
   if ctypes.get_last_error()==5:return None
   raise OSError('worker absent')
  try:
   values=[wintypes.FILETIME() for _ in range(4)]
   if not k.GetProcessTimes(handle,*(ctypes.byref(v) for v in values)):return None
   return str((values[0].dwHighDateTime<<32)|values[0].dwLowDateTime)
  finally:k.CloseHandle(handle)
 path=Path(f'/proc/{pid}/stat')
 return path.read_text().split(') ')[-1].split()[19] if path.exists() else None

class Store:
 def __init__(self,root):
  self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'registry.sqlite3'
  with self.conn() as c:
   c.execute('PRAGMA journal_mode=WAL')
   c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL, league TEXT, status TEXT, pause INTEGER, config TEXT, manifest TEXT, progress TEXT, error TEXT, pid INTEGER)')
   c.execute('CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, body TEXT, job TEXT)')
   c.execute('CREATE TABLE IF NOT EXISTS worker_identity(job TEXT PRIMARY KEY,pid INTEGER,birth TEXT)')
 @contextmanager
 def conn(self):
  c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
  try:
   with c:yield c
  finally:c.close()
 def create(self,league,config,manifest,start_paused=False,request_id=None):
  all_files=manifest['files']
  # Use only files relevant to this league. Multi-league source files remain whole and are filtered by row.
  manifest={**manifest,'files':[r for r in manifest['files'] if
   (r['kind']=='quotes' and (not r.get('targets') or [league,config.get('company','皇冠')] in r['targets'])) or
   (r['kind']=='index' and (not r.get('leagues') or league in r['leagues'])) or r['kind']=='quality']}
  # A job keeps only its own inputs and catalog row; the whole scan is not copied into every job.
  excluded=[{'path':r['path'],'sha256':r.get('sha256')} for r in all_files if r not in manifest['files']]
  manifest['excluded_files']={'count':len(excluded),'sha256':digest(excluded),'reason':'不属于所选联赛与公司，未读取研究内容'}
  if isinstance(manifest.get('leagues'),list):manifest['leagues']=[r for r in manifest['leagues'] if r.get('league')==league and r.get('company')==config.get('company','皇冠')]
  if isinstance(manifest.get('scan'),dict):manifest['scan']={k:v for k,v in manifest['scan'].items() if k!='ignored'}
  for row in manifest.get('leagues') or []:
   if row.get('source_conflict'):
    conflict=row['source_conflict']
    raise ValueError(f"{league}（{config.get('company','皇冠')}）在多个数据目录中有重复比赛（{conflict['duplicate_matches']} 场），例如："+'；'.join(conflict['directories'][:3])+'。请只选择其中一个数据目录后重新检查数据')
  config=check_config(config);jid=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
  config['engine_hash']=source_fingerprint();config['created_version']=VERSION
  body=digest([league,config,manifest,start_paused])
  with self.conn() as c:
   c.execute('BEGIN IMMEDIATE')
   if request_id:
    old=c.execute('SELECT body,job FROM requests WHERE key=?',(request_id,)).fetchone()
    if old:
     if old['body']!=body:raise ValueError('同一请求标识不能更改数据或参数')
     return old['job']
    active=c.execute("SELECT id FROM jobs WHERE league=? AND config=? AND manifest=? AND status IN ('QUEUED','RUNNING','PAUSING','PAUSED')",(league,canonical(config),canonical(manifest))).fetchone()
    if active:
     c.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,body,active['id']));return active['id']
   # Files must exist before the scheduler can observe a committed queue entry.
   p=self.root/jid;p.mkdir();atomic_json(p/'config.json',config);atomic_json(p/'input_manifest.json',manifest)
   c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(jid,time.time(),league,'PAUSED' if start_paused else 'QUEUED',int(start_paused),canonical(config),canonical(manifest),'{}','',0))
   if request_id:c.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,body,jid))
  return jid
 def parallel_limit(self):
  """How many research jobs this workspace lets compute at once (1-8). Each job has its own directory;
  memory and disk grow with the number, so the default stays 1."""
  settings=read_json(self.root/'scheduler_settings.json',{})
  try:value=int((settings or {}).get('max_parallel_jobs',1))
  except (TypeError,ValueError):value=1
  return max(1,min(8,value))
 def set_parallel_limit(self,value):
  if type(value) is not int or not 1<=value<=8:raise ValueError('同时计算的任务数须为1—8的整数')
  atomic_json(self.root/'scheduler_settings.json',{'max_parallel_jobs':value});return value
 def claim(self,jid,pid):
  limit=self.parallel_limit()
  with self.conn() as c:
   c.execute('BEGIN IMMEDIATE')
   r=c.execute('SELECT status,pause FROM jobs WHERE id=?',(jid,)).fetchone()
   if r is None:raise ValueError('任务不存在')
   if r['status']!='QUEUED' or r['pause']:return False
   running=c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','PAUSING') AND id<>?",(jid,)).fetchone()[0]
   if running>=limit:return False
   c.execute("UPDATE jobs SET status='RUNNING',pid=?,error='' WHERE id=? AND status='QUEUED'",(pid,jid))
   c.execute('INSERT OR REPLACE INTO worker_identity VALUES(?,?,?)',(jid,pid,process_birth(pid)))
   return True
 def jobs(self,*,include_manifest=False,active_only=False):
  # Listing and scheduling never decode the (possibly large) input manifests.
  columns='*' if include_manifest else 'id,created,league,status,pause,config,progress,error,pid'
  where=" WHERE status IN ('RUNNING','PAUSING')" if active_only else ''
  with self.conn() as c:rr=c.execute(f'SELECT {columns} FROM jobs{where} ORDER BY created DESC').fetchall()
  return [self.decode(r) for r in rr]
 def get(self,jid):
  with self.conn() as c:r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
  if r is None:raise ValueError('任务不存在')
  return self.decode(r)
 def decode(self,r):
  d=dict(r)
  for k in ('config','manifest','progress'):
   if k in d:d[k]=json.loads(d[k])
  return d
 def update(self,jid,**kw):
  allowed={'status','pause','progress','error','pid'}
  if not set(kw)<=allowed:raise ValueError('非法状态字段')
  if 'progress' in kw:kw['progress']=canonical(kw['progress'])
  with self.conn() as c:c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',(*kw.values(),jid))
 def control(self,jid,action):
  with self.conn() as c:
   c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
   if row is None:raise ValueError('任务不存在')
   j=self.decode(row)
   if action=='pause':
    if j['status'] in ('QUEUED','RUNNING','PAUSING'):
     c.execute('UPDATE jobs SET pause=1,status=? WHERE id=?',('PAUSED' if j['status']=='QUEUED' else 'PAUSING',jid))
   elif action=='resume':
    if j['status'] in ('PAUSED','ERROR','INTERRUPTED'):
     if j['config'].get('engine_hash')!=source_fingerprint():raise ValueError('[SOURCE_REVISION_MISMATCH] 任务冻结代码与当前版本不同，不能直接续跑；请保留旧任务并新建研究')
     c.execute("UPDATE jobs SET pause=0,status='QUEUED',error='' WHERE id=?",(jid,))
   else:raise ValueError('未知操作')
 def resume_all(self):
  """Queue every paused or interrupted job of the current engine; old-engine jobs and explicit errors stay put."""
  current=source_fingerprint();resumed=[]
  for j in self.jobs():
   if j['status'] in ('PAUSED','INTERRUPTED') and j['config'].get('engine_hash')==current:
    self.control(j['id'],'resume');resumed.append(j['id'])
  return resumed
 def recover(self):
  for j in self.jobs(active_only=True):
   if j['status'] in ('RUNNING','PAUSING'):
    try:
     if j['pid']<=0:raise OSError('no worker')
     with self.conn() as c:identity=c.execute('SELECT pid,birth FROM worker_identity WHERE job=?',(j['id'],)).fetchone()
     if identity and identity['birth']:
      birth=process_birth(j['pid'])
      if identity['pid']!=j['pid'] or birth is not None and birth!=identity['birth']:raise OSError('worker PID was reused')
     if os.name=='nt':
      import ctypes
      from ctypes import wintypes
      kernel=ctypes.WinDLL('kernel32',use_last_error=True)
      kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
      kernel.GetExitCodeProcess.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD));kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
      h=kernel.OpenProcess(0x1000,False,j['pid'])
      if not h:
       if ctypes.get_last_error()==5:
        birth=process_birth(j['pid']) if identity and identity['birth'] else None
        if birth is not None and birth==identity['birth']:continue
        raise OSError('worker uninspectable')
       raise OSError('no worker')
      code=wintypes.DWORD()
      try:
       if not kernel.GetExitCodeProcess(h,ctypes.byref(code)) or code.value!=259:raise OSError('worker exited')
      finally:kernel.CloseHandle(h)
     else:
      os.kill(j['pid'],0)
      stat=Path(f"/proc/{j['pid']}/stat")
      if stat.exists() and stat.read_text().split(') ')[-1].startswith('Z'):raise OSError('zombie worker')
    except (OSError,ValueError):
     with self.conn() as c:
      c.execute("UPDATE jobs SET status='INTERRUPTED',pid=0,error=? WHERE id=? AND status=? AND pid=?",('上次进程退出；可从已提交分片继续',j['id'],j['status'],j['pid']))

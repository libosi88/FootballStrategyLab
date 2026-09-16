"""Loopback-only UI. All mutation routes require per-launch token and Origin checks."""
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote,quote
import secrets,threading,subprocess,os,sys,time,json,webbrowser,csv,shutil
import errno,socket
from .common import *
from .data import inspect
from .store import Store,resumability
from .catalog import InspectionCancelled,CATALOG_VERSION,CONFLICT_CHECK
from .release import release_fingerprint,workspace_identity
from .locking import WorkspaceLock,WorkspaceBusy

def launch_next(store,stop):
 """Serialize cooperative launchers until claim; keep each attempt's log separate."""
 if stop.is_set():return None
 try:
  with WorkspaceLock(store.root/'scheduler_guard'):
   if stop.is_set():return None
   store.recover();jobs=store.jobs()
   if sum(j['status'] in ('RUNNING','PAUSING') for j in jobs)>=store.parallel_limit():return None
   queued=sorted((j for j in jobs if j['status']=='QUEUED'),key=lambda j:j['created']);current=source_fingerprint()
   for stale in [j for j in queued if j['config'].get('engine_hash')!=current]:
    # A job frozen under older engine code would only fail at start: keep it and its checkpoints, out of the queue.
    store.update(stale['id'],status='PAUSED',pause=1,pid=0,error='[SOURCE_REVISION_MISMATCH] 代码版本已更新，此任务冻结于旧引擎，不会自动开始；原任务与断点保留，可用 rebuild-stale 或 rebuild-job 重建后再排队')
   queued=[j for j in queued if j['config'].get('engine_hash')==current]
   if not queued:return None
   job=queued[0];folder=store.root/job['id'];logpath=folder/('worker_'+secrets.token_hex(8)+'.log')
   free=shutil.disk_usage(store.root).free;need=max(2*1024**3,4*int(job['config'].get('min_free_disk_mb',256))*1024**2)
   if free<need:
    # Starting a job that would pause at once for disk space would drain the whole queue into PAUSED.
    message=f'磁盘剩余 {free/1024**3:.1f} GB，低于启动所需 {need/1024**3:.1f} GB；任务保持排队，释放空间后自动开始'
    if job.get('error')!=message:store.update(job['id'],error=message)
    return None
   if not all((folder/name).is_file() for name in ('config.json','input_manifest.json')):
    store.update(job['id'],status='ERROR',pid=0,error='任务文件不完整，已移出队列');return None
   try:
    if stop.is_set():return None
    with logpath.open('xb',buffering=0) as log:
     kw={'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS}
     proc=subprocess.Popen([sys.executable,'-B','-m','lab.cli','worker','--workspace',str(store.root),'--job',job['id']],cwd=ROOT,stdout=log,stderr=log,**kw)
    while not stop.is_set() and proc.poll() is None and store.get(job['id'])['status']=='QUEUED':stop.wait(.1)
    code=proc.poll()
    # 75: slot/parallel wait. 0 + still queued: never claimed (CLI/walk-forward/old worker).
    # Only a crashed unclaimed process is an error; a healthy wait must stay in line.
    if code in (0,75) and store.get(job['id'])['status']=='QUEUED':return None
    if code is not None and store.get(job['id'])['status']=='QUEUED':store.update(job['id'],status='ERROR',pid=0,error='计算进程在领取任务前退出；见 '+logpath.name)
    return job['id']
   except Exception as error:
    if store.get(job['id'])['status']=='QUEUED':store.update(job['id'],status='ERROR',pid=0,error='启动失败: '+str(error))
    raise
 except WorkspaceBusy:return None

def job_view(store,job,current_hash=None):
 """Expose workflow completion separately from process termination, including old jobs."""
 result={k:job[k] for k in ('id','created','league','status','progress','error','pid')}
 result['company']=job['config']['company'];result['profile']=job['config']['profile']
 result['research_objective']=job['config'].get('research_objective','validation')
 result['created_version']=job['config'].get('created_version','未知')
 result['previous_version']=job['config'].get('engine_hash')!=(current_hash or source_fingerprint())
 result.update(resumability(job,current_hash))
 result['budgets']={k:job['config'].get(k,0) for k in ('standard_node_budget','max_run_minutes','max_output_mb')}
 result['worker_logs']=[p.name for p in sorted((store.root/job['id']).glob('worker*.log'),key=lambda p:p.stat().st_mtime,reverse=True)[:3]]
 result['stages']=cached_json(store.root/job['id']/'workflow_stages.json',{}).get('stages',{})
 summary=cached_json(store.root/job['id']/'summary.json')
 result['summary']=None
 if summary:
  coverage=summary.get('coverage') if isinstance(summary.get('coverage'),dict) else {}
  result['summary']={'selected':summary.get('selected'),'backup_selected':summary.get('backup_selected',0),
   'state':summary.get('state'),'research_objective':summary.get('research_objective',job['config'].get('research_objective','validation')),'selection_status':summary.get('selection_status'),
   'coverage':{k:coverage.get(k) for k in ('candidates','planned_expressions','evaluated','equivalent_occurrences','proven_zero','proven_nonprofitable','remaining','local_search_complete','standard_search_complete')},
   'gates':summary.get('gates',{}),'packages':summary.get('packages',{}),'empty_roster_chain_verified':summary.get('empty_roster_chain_verified',False),
   'v3_status':summary.get('v3_status','未知'),'recommendation':summary.get('recommendation'),'holdout':summary.get('holdout'),
   'coverage_ratio':summary.get('coverage_ratio'),'packaging_skipped':summary.get('packaging_skipped',False)}
  if result['status']=='DONE' and summary.get('state')=='PARTIAL_RESULT':result['status']='PARTIAL_RESULT'
 return result

JSON_CACHE={}
JSON_CACHE_LOCK=threading.Lock()
TEXT_PREVIEW_BYTES=1024*1024

def cached_json(path,default=None,*,max_bytes=None):
 """Re-read a small job status file only when it was replaced or changed."""
 try:stat=path.stat()
 except FileNotFoundError:return default
 if max_bytes is not None and stat.st_size>max_bytes:return default
 key=str(path);stamp=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
 with JSON_CACHE_LOCK:
  hit=JSON_CACHE.get(key)
  if hit and hit[0]==stamp:return hit[1]
 if max_bytes is None:value=read_json(path,default)
 else:
  with path.open('rb') as f:raw=f.read(max_bytes+1)
  if len(raw)>max_bytes:return default
  value=json.loads(raw.decode('utf-8-sig'))
 with JSON_CACHE_LOCK:
  if len(JSON_CACHE)>=4096:JSON_CACHE.clear()
  JSON_CACHE[key]=(stamp,value)
 return value

def preview_text(path,limit=TEXT_PREVIEW_BYTES):
 """Bound actual reads, including a growing file, and never split a UTF-8 character."""
 import codecs
 with path.open('rb') as f:
  size=os.fstat(f.fileno()).st_size;raw=f.read(limit+1)
 truncated=len(raw)>limit
 text=codecs.getincrementaldecoder('utf-8-sig')().decode(raw[:limit],final=not truncated)
 return {'text':text,'text_truncated':truncated,'file_size_bytes':max(size,len(raw)),'preview_limit_bytes':limit}

def json_ui(value):
 """Preserve exact counters beyond JavaScript's safe integer range."""
 if type(value) is int and abs(value)>9007199254740991:return str(value)
 if isinstance(value,dict):return {k:json_ui(v) for k,v in value.items()}
 if isinstance(value,(tuple,list)):return [json_ui(v) for v in value]
 return value

PAGE_INDEX={}
PAGE_INDEX_LOCK=threading.Lock()
# Bounded lock stripes coalesce builds of the same file without holding the metadata lock during I/O.
PAGE_BUILD_LOCKS=tuple(threading.Lock() for _ in range(16))

def preview_record(f,csv_mode):
 line=f.readline()
 while csv_mode and line.count('"')%2:
  more=f.readline()
  if not more:raise ValueError('CSV引号未闭合，无法预览；请下载完整文件核查')
  line+=more
 return line

def preview_page(path,offset,page=200):
 """Rows [offset, offset+page] of a CSV or JSONL(.gz) file. Record start positions are indexed once per
 file version, so deep pages seek directly instead of re-reading every earlier row."""
 import gzip
 stat=path.stat();key=(str(path),stat.st_size,stat.st_mtime_ns,stat.st_ino)
 opener=(lambda:gzip.open(path,'rt',encoding='utf-8')) if path.suffix=='.gz' else (lambda:path.open(encoding='utf-8-sig',newline=''))
 with PAGE_INDEX_LOCK:entry=PAGE_INDEX.get(key)
 if entry is None:
  with PAGE_BUILD_LOCKS[hash(str(path))%len(PAGE_BUILD_LOCKS)]:
   with PAGE_INDEX_LOCK:entry=PAGE_INDEX.get(key)
   if entry is None:
    starts=[]
    with opener() as f:
     header=preview_record(f,True) if path.suffix!='.gz' else ''
     while True:
      at=f.tell();line=preview_record(f,path.suffix!='.gz')
      if not line:break
      if line.strip():starts.append(at)
    current=path.stat()
    if (current.st_size,current.st_mtime_ns,current.st_ino)!=key[1:]:raise ValueError('预览文件正在更新，请稍后重试')
    entry=(header,starts)
    with PAGE_INDEX_LOCK:
     for old in [k for k in PAGE_INDEX if k[0]==key[0]]:PAGE_INDEX.pop(old,None)
     if len(PAGE_INDEX)>=16:PAGE_INDEX.pop(next(iter(PAGE_INDEX)),None)
     PAGE_INDEX[key]=entry
 # Keep a local reference: another request may evict the entry while these rows are read.
 header,starts=entry;rows=[]
 if offset>=len(starts):return [],False
 with opener() as f:
  for index in range(offset,min(offset+page,len(starts))):
   f.seek(starts[index])
   line=preview_record(f,path.suffix!='.gz')
   if path.suffix=='.gz':rows.append(json.loads(line))
   else:rows.append(next(csv.DictReader([header,line])))
 return rows,offset+page<len(starts)

def preview_portfolio_row(path,variant,config):
 """Return the same priced scenario used to choose the displayed roster."""
 path=Path(path)
 if not path.exists():return None
 scenario='4' if historical_objective(config) else '0'
 target='默认' if variant=='main' else '较低风险';cap=str(config['match_cap'])
 policies=(f"整场最多{cap}单位",'每核心方向首单，无额外整场约束','每命名方向首单，无额外整场约束','每方向首单，无额外整场约束')
 with path.open(encoding='utf-8-sig',newline='') as f:
  for row in csv.DictReader(f):
   if row.get('名单')==target and row.get('情景')==scenario and row.get('整场上限')==cap and row.get('政策') in policies:return row
 return None

class LoopbackHTTPServer(ThreadingHTTPServer):
 # Windows SO_REUSEADDR permits multiple live listeners on one address. Require exclusive ownership.
 allow_reuse_address=False
 allow_reuse_port=False
 def server_bind(self):
  if os.name=='nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
   self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
  super().server_bind()

def bind_ui_server(port,handler):
 """An occupied default port must not prevent local startup or displace another app."""
 try:return LoopbackHTTPServer(('127.0.0.1',port),handler)
 except OSError as error:
  if port!=8765 or error.errno not in (errno.EADDRINUSE,10048):raise
  return LoopbackHTTPServer(('127.0.0.1',0),handler)

def serve(workspace,port=8765,open_browser=True):
 started_hash=source_fingerprint()
 started_release=release_fingerprint();tickets={};ticket_lock=threading.Lock()
 store=Store(workspace);store.recover();token=secrets.token_urlsafe(32);inspect_cache={};stop=threading.Event();lock=threading.Lock()
 scan_lock=threading.Lock();scans={};latest=read_json(store.root/'last_inspection.json')
 if latest:inspect_cache[latest['manifest_id']]={k:v for k,v in latest.items() if k!='manifest_id'}
 def remember(man,paths):
  key=digest(man);inspect_cache[key]=man
  atomic_json(store.root/'last_inspection.json',{'manifest_id':key,**man})
  atomic_json(store.root/'ui_preferences.json',{**read_json(store.root/'ui_preferences.json',{}),'input_paths':paths})
  return key
 def scan_worker(scan_id,paths):
  def update(**kw):
   with scan_lock:scans[scan_id]['progress'].update(kw)
  def cancelled():return stop.is_set() or scans[scan_id].get('cancel',False)
  try:
   man=inspect(paths,store.root/'import_cache',update,cancelled)
   key=remember(man,paths)
   with scan_lock:scans[scan_id].update(status='COMPLETE',manifest_id=key)
  except InspectionCancelled as e:
   with scan_lock:scans[scan_id].update(status='CANCELLED',error=str(e))
  except Exception as e:
   with scan_lock:scans[scan_id].update(status='ERROR',error=str(e))
 scheduler_lock=threading.Lock()
 scheduler_health={'state':'STARTING','alive':False,'heartbeat_at':None,'last_success_at':None,
                   'last_error':None,'last_error_at':None,'last_log_error':None,
                   'consecutive_failures':0,'recovered_at':None}
 def scheduler_snapshot():
  with scheduler_lock:return dict(scheduler_health)
 def scheduler():
  with scheduler_lock:scheduler_health.update(state='HEALTHY',alive=True,heartbeat_at=time.time())
  try:
   while not stop.wait(1):
    try:launch_next(store,stop)
    except Exception as error:
     log_error=None
     try:(store.root/'scheduler_error.log').write_text(str(error),encoding='utf-8')
     except Exception as logging_error:log_error=str(logging_error)
     with scheduler_lock:
      scheduler_health.update(state='DEGRADED',heartbeat_at=time.time(),last_error=str(error),
                              last_error_at=time.time(),last_log_error=log_error,
                              consecutive_failures=scheduler_health['consecutive_failures']+1)
    else:
     with scheduler_lock:
      if scheduler_health['consecutive_failures']:scheduler_health['recovered_at']=time.time()
      scheduler_health.update(state='HEALTHY',heartbeat_at=time.time(),last_success_at=time.time(),consecutive_failures=0)
  finally:
   with scheduler_lock:scheduler_health.update(state='STOPPED',alive=False,heartbeat_at=time.time())
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def safe(self,auth=True):
   if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
    self.respond({'error':'非法Host'},403);return False
   origin=self.headers.get('Origin')
   if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
    self.respond({'error':'跨站请求拒绝'},403);return False
   if auth and self.headers.get('X-Local-Token')!=token:
    self.respond({'error':'缺少本机会话令牌，请刷新界面'},403);return False
   return True
  def respond(self,x,code=200):
   data=json.dumps(json_ui(x),ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
  def do_GET(self):
   route=urlparse(self.path);path=unquote(route.path)
   if path=='/api/identity':
    if not self.safe(False):return
    return self.respond({'application':'FootballStrategyLab','version':VERSION,'workspace_id':workspace_identity(store.root),'engine_hash':started_hash,'release_hash':started_release})
   if path.startswith('/download/'):
    if not self.safe(False):return
    if self.headers.get('X-Local-Token')!=token:
     with ticket_lock:ticket=tickets.pop(parse_qs(route.query).get('ticket',[''])[0],None)
     if not ticket or ticket[0]!=path or ticket[1]<time.time():return self.respond({'error':'下载票据已使用、过期或不匹配，请重新点击下载'},403)
    parts=path.split('/',3)
    try:
     job=store.get(parts[2]);base=(store.root/job['id']).resolve();p=(base/parts[3]).resolve()
     if base not in p.parents or not p.is_file():raise ValueError('无此文件')
     self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(p.stat().st_size));self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
     with p.open('rb') as f:
      for b in iter(lambda:f.read(1024*1024),b''):self.wfile.write(b)
    except (ValueError,IndexError):self.respond({'error':'文件不存在'},404)
    return
   if path.startswith('/api/'):
    if not self.safe():return
    try:
     if path=='/api/state':
      current_hash=source_fingerprint();clean=[]
      for j in store.jobs():
       try:clean.append(job_view(store,j,current_hash))
       except Exception as error:
        clean.append({'id':j.get('id'),'created':j.get('created'),'league':j.get('league'),'status':j.get('status'),'progress':j.get('progress',{}),'error':'任务视图无法生成: '+str(error),'pid':j.get('pid',0),'company':(j.get('config') or {}).get('company'),'profile':(j.get('config') or {}).get('profile'),'created_version':(j.get('config') or {}).get('created_version','未知'),'previous_version':True,'resumable':False,'summary':None,'budgets':{},'worker_logs':[],'stages':{}})
      prefs=read_json(store.root/'ui_preferences.json',{})
      return self.respond({'version':VERSION,'catalog_version':CATALOG_VERSION,'conflict_check':CONFLICT_CHECK,'release_hash':started_release,'engine_hash':current_hash,'server_pid':os.getpid(),'workspace':str(store.root),'jobs':clean,'default_config':DEFAULT,'standard_thresholds':standard_thresholds(DEFAULT),'thresholds_by_objective':{'historical':standard_thresholds(DEFAULT),'validation':STANDARD_RESEARCH_THRESHOLDS},'defaults_by_objective':{'historical':DEFAULT,'validation':VALIDATION_DEFAULT},'scheduler':{'max_parallel_jobs':store.parallel_limit(),'cpu_count':os.cpu_count(),**scheduler_snapshot()},'directions':DIRECTIONS,'research_settings':prefs.get('research_settings',{}),'research_settings_version':prefs.get('settings_version'),'input_paths':prefs.get('input_paths',[]),'has_catalog':bool(inspect_cache),'scope':f'软件{VERSION}；标准范围与各任务实际完成状态分开；结果用于研究/模拟'})
     if path=='/api/catalog':return self.respond(read_json(store.root/'last_inspection.json',{}))
     if path=='/api/inspection':
      scan_id=parse_qs(route.query).get('id',[''])[0]
      with scan_lock:
       if scan_id not in scans:raise ValueError('扫描任务不存在，请重新检查数据')
       result={k:v for k,v in scans[scan_id].items() if k!='cancel'}
       result['progress']=dict(result['progress'])
      if result['status']=='COMPLETE':result['manifest']={'manifest_id':result['manifest_id'],**inspect_cache[result['manifest_id']]}
      return self.respond(result)
     if path=='/api/preview':
      qs=parse_qs(route.query);jid=qs.get('job',[''])[0];job=store.get(jid);name=qs.get('file',['当前模拟名单.csv'])[0];variant=qs.get('variant',['main'])[0]
      if variant not in ('main','lower_risk'):raise ValueError('未知名单')
      shared=('方向汇总.csv','组合政策对照.csv','全部候选审查.csv','全部候选家族审查.csv','最终移除贡献.csv','入围替代对照.csv','主备名单重叠与共同亏损.csv','高风险备选.csv','低样本观察.csv','直接邻域测试.csv','单条报价压力.csv','入围直接邻域测试.csv','入围单条报价压力.csv','最终决定.md','缺口说明.md','样本外逐条.csv','样本外评测.json')
      shared=(*shared,'主备实际风险比较.json','互补研究摘要.json','数据证据分层.json')
      if name not in (*shared,'组合分段稳定性.json','当前模拟名单.csv','完整触发卡.md','golden_signals.jsonl.gz','minute_close_orders.jsonl.gz','trigger_verification.json','rules.json','integration_manifest.json'):raise ValueError('预览文件不支持')
      relative='results/'+('lower_risk/' if variant=='lower_risk' and name not in shared else '')+name
      p=store.root/jid/relative;context=job_view(store,job)
      offset=max(0,int(qs.get('offset',['0'])[0]));page=200
      base={'job':context,'variant':variant,'relative_path':relative,'offset':offset,'shared':name in shared,'note':'历史研究结果；主备名单不可叠加。原价研究与分钟末模拟取价不同。'}
      comparison=store.root/jid/'results/组合政策对照.csv'
      base['portfolio_basis']=read_json(store.root/jid/'results'/('lower_risk' if variant=='lower_risk' else '')/'rules.json',{}).get('execution_policy',{}).get('quote_mapping','legacy_instant_research')
      base['portfolio']=preview_portfolio_row(comparison,variant,job['config'])
      report_path=store.root/jid/'results'/('lower_risk' if variant=='lower_risk' else '')/'trigger_verification.json'
      report=cached_json(report_path,None,max_bytes=TEXT_PREVIEW_BYTES)
      if report is None:
       report={}
       if report_path.exists():base['note']+=' 验证报告较大，指标卡暂不加载；完整内容可下载查看。'
      base['minute_close']=report.get('streaming_paper_execution',{})
      if not p.exists():return self.respond({**base,'rows':[],'note':'该任务尚未生成此项；旧任务可能不提供新验收证据。','available':False})
      if name.endswith(('.md','.json')):
       content=preview_text(p)
       if content['text_truncated']:base['note']+=' 文件较大，仅预览前1 MiB；内容已截断，完整文件请点击下载。'
       return self.respond({**base,**content,'rows':[],'available':True,'has_more':False})
      rows,has_more=preview_page(p,offset,page)
      return self.respond({**base,'rows':rows,'has_more':has_more,'available':True})
     return self.respond({'error':'未知API'},404)
    except Exception as e:return self.respond({'error':str(e)},400)
   if not self.safe(False):return
   name={'/':'index.html','/app.js':'app.js','/jobs.js':'jobs.js','/research_policy.js':'research_policy.js','/style.css':'style.css'}.get(path)
   if name is None:return self.respond({'error':'不存在'},404)
   p=ROOT/'web'/name;data=p.read_bytes()
   if name=='index.html':data=data.replace(b'__TOKEN__',token.encode())
   self.send_response(200);self.send_header('Content-Type',{'index.html':'text/html','app.js':'text/javascript','jobs.js':'text/javascript','research_policy.js':'text/javascript','style.css':'text/css'}[name]+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Frame-Options','DENY');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
  def do_POST(self):
   if not self.safe():return
   try:
    length=int(self.headers.get('Content-Length','0'))
    if not 0<length<1024*1024:raise ValueError('请求大小错误')
    if 'application/json' not in self.headers.get('Content-Type',''):raise ValueError('仅接受JSON')
    data=json.loads(self.rfile.read(length));path=urlparse(self.path).path
    if path=='/api/shutdown':
     stop.set()
     self.respond({'stopped':'interface_service','active_workers_continue':True})
     threading.Thread(target=self.server.shutdown,daemon=True).start();return
    if path=='/api/download-ticket':
     job=store.get(data['job']);base=(store.root/job['id']).resolve();target=(base/data['path']).resolve()
     if base not in target.parents or not target.is_file():raise ValueError('下载文件不存在或越界')
     relative=target.relative_to(base).as_posix();route='/download/'+job['id']+'/'+relative;expiry=time.time()+300
     with ticket_lock:
      for key in list(tickets):
       if tickets[key][1]<time.time():tickets.pop(key)
      if len(tickets)>=1024:raise ValueError('未使用的下载票据过多，请稍后重试')
      nonce=secrets.token_urlsafe(32);tickets[nonce]=(route,expiry)
     return self.respond({'url':quote(route,safe='/')+'?ticket='+nonce,'expires_at':expiry})
    if path=='/api/inspect':
     paths=data.get('paths',[])
     if not isinstance(paths,list) or not paths or any(not isinstance(p,str) or not p.strip() for p in paths):raise ValueError('请填写一个或多个本机路径')
     if data.get('background'):
      with scan_lock:
       if any(s['status']=='RUNNING' for s in scans.values()):raise ValueError('已有输入扫描正在进行，请等待或取消')
       scans.clear();scan_id=secrets.token_hex(12);scans[scan_id]={'status':'RUNNING','progress':{},'cancel':False}
      threading.Thread(target=scan_worker,args=(scan_id,paths),daemon=True).start()
      return self.respond({'scan_id':scan_id,'status':'RUNNING'})
     man=inspect(paths,store.root/'import_cache');key=remember(man,paths);return self.respond({'manifest_id':key,**man})
    if path=='/api/inspection/cancel':
     with scan_lock:
      if data.get('scan_id') not in scans:raise ValueError('扫描任务不存在')
      scans[data['scan_id']]['cancel']=True
     return self.respond({'ok':True})
    if path=='/api/create':
     if started_hash!=source_fingerprint() or started_release!=release_fingerprint():raise ValueError('服务启动后代码已更新，请重启工作台再创建任务')
     man=inspect_cache.get(data['manifest_id'])
     if not man:raise ValueError('请先检查输入')
     if man.get('catalog_version')!=CATALOG_VERSION or man.get('conflict_check')!=CONFLICT_CHECK:raise ValueError('扫描口径已更新，请重新检查输入')
     if 'paths' in data and [str(Path(p).expanduser().resolve()) for p in data['paths']]!=man.get('input_paths'):raise ValueError('目录已改变，请重新检查输入')
     config=check_config(data.get('config',{}));leagues=data.get('leagues',[])
     allowed={(r['league'],r['company']) for r in man['leagues']}
     if len(leagues)!=1:raise ValueError('每次请选择一个联赛')
     ids=[]
     for league in leagues:
      if (league,config['company']) not in allowed:raise ValueError('联赛与公司不存在于输入')
      ids.append(store.create(league,config,man,request_id=data.get('request_id')))
     # A demo run must not replace the saved research settings used for real leagues.
     if data.get('persist_settings',True):atomic_json(store.root/'ui_preferences.json',{**read_json(store.root/'ui_preferences.json',{}),'research_settings':config,'settings_version':VERSION})
     return self.respond({'jobs':ids})
    if path=='/api/control':store.control(data['job'],data['action']);return self.respond({'ok':True})
    if path=='/api/control-all':
     if data.get('action')!='resume':raise ValueError('批量操作只支持续跑')
     return self.respond({'resumed':store.resume_all()})
    if path=='/api/settings':
     current=read_json(store.root/'ui_preferences.json',{})
     # The parallel limit belongs to the workspace scheduler, never to a frozen research configuration.
     parallel=None
     if 'max_parallel_jobs' in data:
      raw=data['max_parallel_jobs']
      if type(raw) is int:parallel=raw
      elif isinstance(raw,str) and raw.strip().isdigit():parallel=int(raw.strip())
      if parallel is None or not 1<=parallel<=8:raise ValueError('同时计算的任务数须为1—8的整数')
     merged={k:v for k,v in {**current.get('research_settings',{}),**data}.items() if k in DEFAULT}
     # Standard thresholds are fixed; an older saved relaxed value must not block saving other settings.
     if merged.get('profile',DEFAULT['profile'])=='standard':merged.update(standard_thresholds({**DEFAULT,**merged}))
     settings=check_config(merged)
     if parallel is not None:store.set_parallel_limit(parallel)
     atomic_json(store.root/'ui_preferences.json',{**current,'research_settings':settings,'settings_version':VERSION})
     return self.respond({'saved':True})
    if path=='/api/demo':
     man=inspect([str(ROOT/'demo')],store.root/'import_cache');key=digest(man);inspect_cache[key]=man
     return self.respond({'manifest_id':key,**man})
    if path=='/api/browse':
     import tkinter as tk
     from tkinter import filedialog
     with lock:
      rt=tk.Tk();rt.withdraw();rt.attributes('-topmost',True);picked=filedialog.askdirectory(title='选择一个联赛或多个联赛的CSV目录');rt.destroy()
     return self.respond({'path':picked})
    return self.respond({'error':'未知接口'},404)
   except Exception as e:return self.respond({'error':str(e)},400)
 server=bind_ui_server(port,Handler)
 try:
  atomic_json(store.root/'ui_endpoint.json',{'port':server.server_port,'pid':os.getpid()})
  threading.Thread(target=scheduler,daemon=True).start()
  url=f'http://127.0.0.1:{server.server_port}'
  print(f'本地界面：{url}\n工作区：{store.root}\n无真实下注接口。Ctrl+C退出界面服务，正在运行的本机工作进程独立。',flush=True)
  if open_browser:
   try:webbrowser.open(url)
   except Exception as error:print('浏览器未能自动打开，请手动访问 '+url+'：'+str(error),file=sys.stderr)
  server.serve_forever()
 except KeyboardInterrupt:pass
 finally:stop.set();server.server_close()

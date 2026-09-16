"""Strict, opt-in cache import for ONE documented AtomCatalog cache_size patch.

Default: validate and write an audit report under validation/v041_india only.
--execute: import into an existing, pristine, paused NEW task via a private directory.
Never edits production source, the old task, input CSVs, or registry job contents.
No production lab modules are imported. The old worker must already have exited.
"""
from pathlib import Path
from contextlib import ExitStack,contextmanager
from datetime import datetime,timezone
import argparse,ctypes,hashlib,json,math,os,re,shutil,sqlite3,stat,subprocess,sys,tempfile,time,uuid

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
FROZEN=HERE/'frozen_engine_9e'
OLD_ENGINE='9e5011b5dc661aa47fb39c0826f81f17fa3594bb504001b605cc2978e6f20f78'
OLD_JOB='20260913_023227_7b3064'
OUTPUT_ROOT=Path('D:/FootballStrategyLab_workspace/v041_india')
DIRECTIONS=[f'{p}_{d}' for p in ('PRE','LIVE') for d in ('GIVE','RECEIVE','PK_HOME','PK_AWAY','OVER','UNDER','HOME','AWAY')]
PRE=[d for d in DIRECTIONS if d.startswith('PRE_')]
PREPARED={'events.jsonl.gz','labels.json','data_audit.json'}
PRAGMA=b"        self.conn.execute('PRAGMA cache_size=-65536')"

class Rejected(RuntimeError):pass
def require(test,message):
    if not test:raise Rejected(message)
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(x):return hashlib.sha256(canonical(x).encode('utf-8')).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def now():return datetime.now(timezone.utc).isoformat()
def atomic_replace(source,target):
    deadline=time.monotonic()+15;delay=.02
    while True:
        try:os.replace(source,target);return
        except OSError as exc:
            transient=os.name=='nt' and (isinstance(exc,PermissionError) or getattr(exc,'winerror',None) in (5,32,33))
            if not transient or time.monotonic()>=deadline:raise
            time.sleep(min(delay,max(0,deadline-time.monotonic())));delay=min(.5,delay*2)
def atomic_json(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
        atomic_replace(tmp,path)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()
def regular(path):
    p=Path(path)
    require(p.is_file(),f'Missing regular file: {p}')
    s=p.lstat()
    require(not p.is_symlink() and not (getattr(s,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',1024)),f'Reparse/symlink file refused: {p}')
    return p
def binding(version,engine,direction,config,prepared):
    return digest({'version':version,'engine':engine,'direction':direction,'config':config,'input':prepared})
def fingerprint(files):
    # The evidence manifest also contains tests; the production engine hash covers lab/*.py only.
    return digest(sorted((name,h) for name,h in files.items() if name.startswith('lab/') and name.count('/')==1 and name.endswith('.py')))

def validate_code():
    identity=read(regular(FROZEN/'frozen_engine_identity.json'))
    require(identity['engine_hash']==OLD_ENGINE,'Frozen identity is not the required 9e engine')
    old_files={name:h for name,h in identity['files'].items() if name.startswith('lab/') and name.count('/')==1 and name.endswith('.py')}
    require(fingerprint(old_files)==OLD_ENGINE,'Frozen file manifest does not reproduce 9e fingerprint')
    baseline=regular(FROZEN/'standard_atoms.py').read_bytes()
    require(hashlib.sha256(baseline).hexdigest()==old_files['lab/standard_atoms.py'],'Frozen AtomCatalog bytes changed')
    current={p.relative_to(ROOT).as_posix():sha(p) for p in sorted((ROOT/'lab').glob('*.py'))}
    require(set(current)==set(old_files),'Production lab file set changed')
    require([name for name in current if current[name]!=old_files[name]]==['lab/standard_atoms.py'],'Only standard_atoms.py may differ; unchanged source is not a new engine')
    anchor=b'        self.conn=sqlite3.connect(self.path);self.cache=OrderedDict()'
    eol=b'\r\n' if anchor+b'\r\n' in baseline else b'\n'
    require(baseline.count(anchor+eol)==1 and PRAGMA not in baseline,'Frozen insertion anchor is not unique')
    expected=baseline.replace(anchor+eol,anchor+eol+PRAGMA+eol,1)
    actual=(ROOT/'lab/standard_atoms.py').read_bytes()
    require(actual==expected,'New AtomCatalog must equal frozen bytes plus the one exact cache_size line after connect')
    engine=fingerprint(current);require(engine!=OLD_ENGINE,'New engine fingerprint equals old')
    return {'old_engine':OLD_ENGINE,'new_engine':engine,'version':identity['version'],'frozen_manifest_sha256':sha(FROZEN/'frozen_engine_identity.json'),'frozen_atoms_sha256':sha(FROZEN/'standard_atoms.py'),'new_atoms_sha256':sha(ROOT/'lab/standard_atoms.py'),'new_files':current,'only_change':PRAGMA.decode().strip(),'byte_exact_insertion':True}

def registry_connection(workspace,readonly=True):
    p=regular(Path(workspace)/'registry.sqlite3').resolve()
    conn=sqlite3.connect(p.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=10)
    conn.row_factory=sqlite3.Row
    return conn
def get_job(workspace,jid,connection=None):
    own=connection is None;c=connection or registry_connection(workspace)
    try:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone();require(row is not None,f'Registry job missing: {jid}')
        result=dict(row)
        for k in ('config','manifest','progress'):result[k]=json.loads(result[k])
        return result
    finally:
        if own:c.close()
def paused(job,pristine=False):
    require(job['status']=='PAUSED' and job['pause']==1 and job['pid']==0,f"Job must be PAUSED, pause=1, pid=0: {job['id']}")
    if pristine:require(job['progress']=={} and job['error']=='',f"Target has run/progress/error history: {job['id']}")
def process_exists(pid):
    require(type(pid) is int and pid>0,'Supply the positive PID recorded for the OLD worker before pausing')
    if os.name=='nt':
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            error=ctypes.get_last_error();require(error==87,f'Cannot prove old PID absent; OpenProcess error={error}')
            return False
        kernel.CloseHandle(handle);return True # Also reject PID reuse conservatively.
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:raise Rejected('Cannot prove old PID absent: access denied')
def assert_no_job_process(source_jid,target_jid):
    require(os.name=='nt','This narrow migration tool requires Windows process verification')
    # IDs are fixed-format data; no arbitrary strings are interpolated into shell code.
    require(all(re.fullmatch(r'\d{8}_\d{6}_[0-9a-f]{6}',s) for s in (source_jid,target_jid)),'Invalid task id')
    script=("$ErrorActionPreference='Stop'; $ids=@("+f"'{source_jid}','{target_jid}'"+"); "
            "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { "
            f"$_.ProcessId -ne {os.getpid()} -and $null -ne $_.CommandLine -and "
            f"-not ($_.ProcessId -eq {os.getppid()} -and $_.CommandLine.Contains('migrate_cache_only_change.py')) -and "
            "($_.CommandLine.Contains($ids[0]) -or $_.CommandLine.Contains($ids[1])) } | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress")
    r=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    require(r.returncode==0,'Windows process enumeration failed; absence is unproven')
    payload=r.stdout.strip();hits=[] if not payload else json.loads(payload)
    require(not hits,f'Python process still references source/target task: {hits}')

@contextmanager
def workspace_lock(workspace):
    # Only used with --execute. Registry contents are not changed.
    p=Path(workspace)/'.worker.lock';p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a+b') as f:
        f.seek(0,os.SEEK_END)
        if not f.tell():f.write(b'0');f.flush()
        f.seek(0)
        import msvcrt
        try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise Rejected(f'Workspace worker is still active: {workspace}')
        try:yield
        finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

def verify_dictionary(directory,old_binding,direction,record):
    marker_path=directory/'dictionary_complete.json';marker=read(record(marker_path))
    spec=read(record(directory/'search_spec.json'));extra=read(record(directory/'dictionary.json'))
    db=record(directory/'atoms.sqlite3',marker.get('database_sha256'))
    require(marker.get('database_sha256') and spec['dictionary_sha256']==marker['database_sha256']==extra['atoms_sha256'],'Dictionary database SHA chain mismatch')
    require(spec['binding']==old_binding and spec['direction']==extra['direction']==direction,'Dictionary direction/binding mismatch')
    require(all(extra.get(k)==v for k,v in marker.items()),'dictionary.json does not match complete marker')
    require(spec['spec_version']==marker['spec_version']=='FSL_STANDARD_V3_1' and spec['blocked']==marker['blocked'],'Dictionary scope/version mismatch')
    require(not any(Path(str(db)+s).exists() for s in ('-wal','-shm','-journal')),'SQLite sidecar present; refuse copying a possibly live database')
    with sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
        require(conn.execute('PRAGMA quick_check').fetchall()==[('ok',)],'SQLite quick_check failed')
        count,last=conn.execute('SELECT COUNT(*),MAX(id)+1 FROM atoms').fetchone();aliases=conn.execute('SELECT COUNT(*) FROM aliases').fetchone()[0]
        require(count==last==marker['atom_count'] and aliases==marker['raw_atom_templates'],'Atom IDs/alias counts differ from marker')
        groups={name:conn.execute('SELECT COUNT(*) FROM atoms WHERE groups & ? != 0',(flag,)).fetchone()[0] for name,flag in [('W',1),('C',2),('T',4),('M06',8),('M07',16)]}
        require(groups==marker['groups'],'Dictionary group counts differ')
        ctx=conn.execute("SELECT COUNT(*) FROM atoms WHERE groups & 2 != 0 AND feature NOT LIKE 'path%' AND feature NOT LIKE 'linepath%'").fetchone()[0]
        extra_w=conn.execute('SELECT COUNT(*) FROM atoms WHERE groups & 1 != 0 AND groups & 2 = 0').fetchone()[0]
    conn.close();c=groups['C'];choose=lambda n,k:math.comb(n,k) if n>=k else 0
    modules={'M00':1,'M01':groups['W'],'M02':choose(c,2),'M03':choose(c,3),'M04':groups['T']*(1+ctx+choose(ctx,2)),'M05':extra_w*(ctx+choose(ctx,2)),'M06':groups['M06']*(1+c+choose(c,2)),'M07':groups['M07']*(1+c+choose(c,2))}
    require(spec['modules']==modules and spec['total']==sum(modules.values()),'Recomputed M00-M07 counts differ from old frozen spec')
    record(db,marker['database_sha256'])
    return {'direction':direction,'database_sha256':marker['database_sha256'],'atoms':count,'aliases':aliases,'groups':groups,'modules':modules,'total':sum(modules.values()),'quick_check':'ok','old_binding':old_binding}

def validate(args,report):
    require(args.source_job==OLD_JOB,'Only the specified India 9e source task is supported')
    require(args.target_job!=args.source_job and re.fullmatch(r'\d{8}_\d{6}_[0-9a-f]{6}',args.target_job),'Target must be a distinct valid task id')
    sw=Path(args.source_workspace).resolve();tw=Path(args.target_workspace).resolve()
    require(sw==tw==(ROOT/'workspace').resolve(),'This narrow importer requires the same original C registry/workspace for both tasks')
    require((sw/args.source_job).is_junction() and (tw/args.target_job).is_junction(),'Both C task directories must be existing per-task junctions to D')
    source=(sw/args.source_job).resolve();target=(tw/args.target_job).resolve()
    require(source.is_dir() and target.is_dir(),'Both task directories must already exist')
    require(source.parent==target.parent==OUTPUT_ROOT.resolve() and source.name==args.source_job and target.name==args.target_job,'Resolved task directories must be direct, matching-ID children of the declared D output root')
    require(source!=target and source not in target.parents and target not in source.parents,'Source/target physical paths overlap')
    old=get_job(sw,args.source_job);new=get_job(tw,args.target_job);paused(old);paused(new,True)
    require(not process_exists(args.source_worker_pid),'Old worker PID still exists (or has been reused); do not migrate')
    assert_no_job_process(args.source_job,args.target_job)
    require({p.name for p in target.iterdir()}=={'config.json','input_manifest.json'},'Target is not pristine; only config.json and input_manifest.json are permitted')
    config_old=read(regular(source/'config.json'));config_new=read(regular(target/'config.json'))
    manifest_old=read(regular(source/'input_manifest.json'));manifest_new=read(regular(target/'input_manifest.json'))
    require(old['config']==config_old and new['config']==config_new and old['manifest']==manifest_old and new['manifest']==manifest_new,'Registry/file config or manifest disagreement')
    require(old['league']==new['league']=='印度超' and config_old['profile']=='standard' and config_old['company']=='皇冠','Unexpected league/profile/company')
    require(config_old['engine_hash']==OLD_ENGINE,'Source configuration is not frozen 9e')
    code=validate_code();report['source_code_proof']=code
    require(config_new['engine_hash']==code['new_engine'],'Target config does not name current new engine')
    require({k:v for k,v in config_old.items() if k!='engine_hash'}=={k:v for k,v in config_new.items() if k!='engine_hash'},'Configurations differ beyond engine_hash')
    require(config_old['created_version']==config_new['created_version']==code['version'],'Version/created_version changed')
    require(config_old['directions']==DIRECTIONS and manifest_new==manifest_old,'Direction order or complete input_manifest changed')
    require(old['progress'].get('stage')=='S1' and old['progress'].get('search_nodes',0)==0 and old['progress'].get('nodes',0)==0,'Source must still be S1 with zero visited search nodes')
    require(not list(source.rglob('search.sqlite3*')) and not list((source/'mining').glob('*/state.json')),'Source already has a search ledger or direction state; do not reset spent search budget')
    stages=read(regular(source/'workflow_stages.json'))
    require(stages['binding']==digest({'config':config_old,'manifest':manifest_old}),'Old workflow stage binding mismatch')
    require(stages['stages']['S0']['status']=='PASS' and stages['stages']['S1']['status']=='RUNNING','Old task is not paused within initial S1')
    require(all(stages['stages'][f'S{i}']['status']=='PENDING' and stages['stages'][f'S{i}']['started'] is None and stages['stages'][f'S{i}']['evidence']=={} for i in range(2,8)),'An old search or later workflow stage already started')
    tracked={};copies=[];generated={};directories=[]
    def record(path,expected=None):
        p=regular(path).resolve();before=p.stat();h=sha(p);after=p.stat()
        require((before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns),f'File changed during validation: {p}')
        if expected is not None:require(h==expected,f'SHA mismatch: {p}')
        if str(p) in tracked:require(tracked[str(p)]['sha256']==h,f'File changed across checks: {p}')
        tracked[str(p)]={'sha256':h,'size_bytes':after.st_size};return p
    for name in ('config.json','input_manifest.json'):record(source/name);record(target/name)
    record(source/'workflow_stages.json')
    for rec in manifest_old['files']:record(Path(rec['path']),rec['sha256'])
    prepared=read(record(source/'prepared_manifest.json'));require(set(prepared)==PREPARED,'Unexpected prepared manifest file set')
    audit=read(record(source/'data_audit.json',prepared['data_audit.json']))
    require(audit['league']=='印度超' and audit['company']=='皇冠' and audit['inputs']==manifest_old['files'],'Data audit input linkage mismatch')
    for name in sorted(PREPARED):record(source/name,prepared[name]);copies.append(name)
    copies.append('prepared_manifest.json')
    for direction in DIRECTIONS:
        directory=source/'mining'/direction;meta=read(record(directory/'standard_features.json'))
        old_binding=binding(code['version'],OLD_ENGINE,direction,config_old,prepared)
        new_binding=binding(code['version'],code['new_engine'],direction,config_new,prepared)
        require(meta['binding']==old_binding,'Old full array binding mismatch: '+direction)
        record(directory/'arrays.npz',meta['sha256']);copies.append(f'mining/{direction}/arrays.npz')
        generated[f'mining/{direction}/standard_features.json']={**meta,'binding':new_binding}
        evidence={'direction':direction,'array_sha256':meta['sha256'],'old_binding':old_binding,'new_binding':new_binding}
        if direction in PRE:
            evidence['dictionary']=verify_dictionary(directory,old_binding,direction,record)
            copies.extend(f'mining/{direction}/{name}' for name in ('atoms.sqlite3','dictionary_complete.json'))
        directories.append(evidence)
    report.update(source_job=args.source_job,target_job=args.target_job,source_workspace=str(sw),target_workspace=str(tw),source_directory=str(source),target_directory=str(target),old_worker_pid=args.source_worker_pid,
                  config_old=config_old,config_new=config_new,input_manifest_sha256=digest(manifest_old),prepared_manifest=prepared,directions=directories,source_files=tracked,
                  copy_whitelist=copies,generated_metadata_whitelist=sorted(generated),not_inherited=['old engine tests','workflow/stage completion','direction state','masks','search ledger','candidates','selection','packages','incomplete or LIVE dictionaries'],
                  arrays=16,complete_pre_dictionaries=8,previous_search_coverage_inherited=False)
    report['path_safety']={'declared_output_root':str(OUTPUT_ROOT.resolve()),'source_C_junction':str(sw/args.source_job),'target_C_junction':str(tw/args.target_job),'junctions_will_be_preserved':True,'both_physical_jobs_are_direct_children':True}
    return {'sw':sw,'tw':tw,'source':source,'target':target,'old':old,'new':new,'tracked':tracked,'copies':copies,'generated':generated,'code':code}

def recheck(ctx,args):
    for text,item in ctx['tracked'].items():require(sha(regular(text))==item['sha256'],f'Source/target/input changed since validation: {text}')
    require(validate_code()==ctx['code'],'Production source changed since validation')
    require(not process_exists(args.source_worker_pid),'Old PID appeared during migration')
    require(get_job(ctx['sw'],args.source_job)==ctx['old'] and get_job(ctx['tw'],args.target_job)==ctx['new'],'Registry state changed during migration')
    require((ctx['sw']/args.source_job).resolve()==ctx['source'] and (ctx['tw']/args.target_job).resolve()==ctx['target'],'C task junction target changed')
    require(not list(ctx['source'].rglob('search.sqlite3*')) and not list((ctx['source']/'mining').glob('*/state.json')),'Source acquired search state during validation')

def rename_job_directory(source,target,report,operation):
    source=Path(source).resolve();target=Path(target).resolve();boundary=OUTPUT_ROOT.resolve()
    require(source.parent==target.parent==boundary and source!=target,'Rename paths escaped declared D output root')
    require(source.is_dir() and not source.is_junction() and not target.exists(),'Rename requires a physical source directory and nonexistent target')
    evidence={'operation':operation,'resolved_source':str(source),'resolved_target':str(target),'checked_boundary':str(boundary),'status':'VALIDATED'}
    report.setdefault('directory_renames',[]).append(evidence)
    atomic_replace(source,target);evidence['status']='DONE'

def execute(ctx,args,report):
    target=ctx['target'];parent=target.parent.resolve()
    require(target.resolve().parent==parent and target.resolve()!=ctx['source'],'Resolved target parent changed')
    stage=Path(tempfile.mkdtemp(prefix=f'.{args.target_job}.cache_import.',dir=parent)).resolve()
    require(stage.parent==parent and stage!=target and ctx['source'] not in stage.parents,'Unsafe private staging path')
    report['private_staging_directory']=str(stage);report['copied_files']=[]
    # Existing empty target is preserved byte-for-byte inside private publication.
    for name in ('config.json','input_manifest.json'):
        original=target/name;h=sha(original);shutil.copy2(original,stage/name)
        require(sha(stage/name)==h==sha(original),'Pristine target file changed during copy')
    for relative in ctx['copies']:
        source=ctx['source']/relative;dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        require(dest.resolve().is_relative_to(stage),'Copy target escaped private directory')
        expected=ctx['tracked'][str(source.resolve())]['sha256'];require(sha(source)==expected,'Source changed before copy: '+relative)
        shutil.copy2(source,dest);require(sha(dest)==expected==sha(source),'Copy/source SHA mismatch: '+relative)
        report['copied_files'].append({'relative_path':relative,'source_sha256':expected,'target_sha256':expected})
    for relative,value in ctx['generated'].items():atomic_json(stage/relative,value)
    expected_files=set(ctx['copies'])|set(ctx['generated'])|{'config.json','input_manifest.json'}
    require({p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()}==expected_files,'Private directory contains non-whitelisted files')
    for relative in ctx['generated']:
        meta=read(stage/relative);require(sha((stage/relative).parent/'arrays.npz')==meta['sha256'],'Staged array SHA mismatch')
    recheck(ctx,args)
    require({p.name for p in target.iterdir()}=={'config.json','input_manifest.json'},'Target gained files before publication')
    backup=parent/f'.{args.target_job}.pristine_before_import.{uuid.uuid4().hex}'
    require(not backup.exists() and backup.resolve().parent==parent,'Unsafe backup path')
    report.update(status='VALIDATED_FOR_ATOMIC_PUBLICATION',pristine_target_backup=str(backup),generated_metadata=[{'relative_path':p,'sha256':sha(stage/p)} for p in ctx['generated']])
    atomic_json(stage/'cache_migration.json',report)
    # Hold short registry write locks without UPDATEs to prevent UI resume during rename.
    connections={};moved_old=False;published=False
    try:
        for workspace in sorted({ctx['sw'],ctx['tw']},key=str):
            conn=registry_connection(workspace,False);connections[workspace]=conn;conn.execute('BEGIN IMMEDIATE')
        require(get_job(ctx['sw'],args.source_job,connections[ctx['sw']])==ctx['old'] and get_job(ctx['tw'],args.target_job,connections[ctx['tw']])==ctx['new'],'Registry changed before publication')
        rename_job_directory(target,backup,report,'preserve_pristine_target');moved_old=True
        try:rename_job_directory(stage,target,report,'publish_verified_private_directory');published=True
        except BaseException:
            rename_job_directory(backup,target,report,'restore_pristine_after_publish_failure');moved_old=False;raise
        require({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}==expected_files|{'cache_migration.json'},'Published whitelist differs')
        for item in report['copied_files']:require(sha(target/item['relative_path'])==item['target_sha256'],'Published file SHA mismatch')
        require(validate_code()==ctx['code'],'Source code changed at publication')
        for text,item in ctx['tracked'].items():
            p=Path(text)
            # config/manifest bytes at target are retained; all old inputs/caches must also stay unchanged.
            require(sha(p)==item['sha256'],'Tracked file changed at publication: '+text)
        report.update(status='CACHE_MIGRATION_PUBLISHED_TARGET_STILL_PAUSED',finished_at=now(),new_tests_inherited=False,new_s1_spec_must_be_regenerated=True)
        atomic_json(target/'cache_migration.json',report)
        for conn in connections.values():conn.commit()
    except BaseException:
        if published:
            quarantine=parent/f'.{args.target_job}.failed_import.{uuid.uuid4().hex}'
            require(quarantine.resolve().parent==parent and not quarantine.exists(),'Unsafe rollback quarantine path')
            rename_job_directory(target,quarantine,report,'preserve_failed_import');rename_job_directory(backup,target,report,'restore_pristine_after_verification_failure')
            report['failed_import_preserved_at']=str(quarantine);moved_old=False
        elif moved_old:rename_job_directory(backup,target,report,'restore_pristine_after_other_failure')
        for conn in connections.values():conn.rollback()
        raise
    finally:
        for conn in connections.values():conn.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-workspace',default=str(ROOT/'workspace'))
    parser.add_argument('--source-job',default=OLD_JOB)
    parser.add_argument('--source-worker-pid',type=int,required=True,help='Actual 9e worker PID recorded BEFORE pause; must no longer exist')
    parser.add_argument('--target-workspace',default=str(ROOT/'workspace'))
    parser.add_argument('--target-job',required=True,help='Existing pristine PAUSED new-engine task; never the old task')
    parser.add_argument('--execute',action='store_true',help='Explicitly import validated whitelist; default only validates')
    args=parser.parse_args();report={'tool':'FSL_ONE_LINE_CACHE_SIZE_IMPORT_V1','started_at':now(),'mode':'EXECUTE' if args.execute else 'VALIDATE_ONLY','status':'CHECKING'}
    output=HERE/f'cache_migration_{"execute" if args.execute else "check"}_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{uuid.uuid4().hex[:6]}.json'
    try:
        require(Path(args.source_workspace).resolve()==Path(args.target_workspace).resolve()==(ROOT/'workspace').resolve(),'Only the original shared C workspace may be locked/used')
        with ExitStack() as stack:
            if args.execute:
                for workspace in sorted({Path(args.source_workspace).resolve(),Path(args.target_workspace).resolve()},key=str):stack.enter_context(workspace_lock(workspace))
            ctx=validate(args,report);recheck(ctx,args)
            if args.execute:execute(ctx,args,report)
            else:report.update(status='VALIDATED_ONLY_NO_TASK_FILES_CHANGED',finished_at=now())
        atomic_json(output,report)
        print(json.dumps({'status':report['status'],'report':str(output),'target_remains_paused':True,'execute':args.execute},ensure_ascii=False));return 0
    except Exception as exc:
        report.update(status='REJECTED_NO_SUCCESS_CLAIM',failure_type=type(exc).__name__,failure=str(exc),finished_at=now())
        atomic_json(output,report)
        print(json.dumps({'status':report['status'],'failure':str(exc),'report':str(output),'execute':args.execute},ensure_ascii=False),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())

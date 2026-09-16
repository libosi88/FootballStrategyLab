"""Import verified original inputs from B archives as a new PAUSED research job.

No historical absolute path is opened. Old search/certification state is never
rebound to different code, data locations, or an unverified host.
"""
from pathlib import Path,PureWindowsPath
import zipfile,tempfile,hashlib,json,re,shutil
from .common import ROOT,VERSION,DIRECTIONS,DEFAULT,DEPRECATED_CONFIG,check_config,standard_thresholds,sha,digest,read_json,atomic_json,atomic_replace,source_fingerprint
from .data import inspect
from .store import Store
from copy import deepcopy
from contextlib import closing
import sqlite3

REBUILD_BUDGETS=('standard_node_budget','standard_review_budget','max_pool_pairs','select_budget',
                 'selection_eval_budget','max_run_minutes','max_output_mb')
REBUILD_SETTINGS=('profile','company','research_objective','search_grammar','min_profit','min_matches',
                  'holdout_months','min_history_years','match_cap',*REBUILD_BUDGETS)
REBUILD_HINT='旧standard参数冲突时，可先用 --profile standard --dry-run 查看当前标准的调整；全历史研究须显式 --config config/rebuild_historical.json。'

def _read_rebuild_jobs(workspace,job_id=None):
    """Read a stable private DB/WAL snapshot; SQLite cannot create source SHM/WAL files."""
    path=Path(workspace).resolve()/'registry.sqlite3'
    if not path.is_file():raise ValueError('工作区注册表不存在，迁移不会创建空工作区')
    def stamp():
        return {p.name:sha(p) for p in (path,Path(str(path)+'-wal')) if p.is_file()}
    rows=None
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix='fsl_registry_read_') as scratch:
            snapshot=Path(scratch)/path.name
            try:
                before=stamp()
                for name in before:shutil.copyfile(path.parent/name,Path(scratch)/name)
                if path.name not in before or stamp()!=before or any(sha(Path(scratch)/n)!=h for n,h in before.items()):continue
            except OSError:continue
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as conn:
                conn.row_factory=sqlite3.Row
                rows=conn.execute('SELECT * FROM jobs'+(' WHERE id=?' if job_id is not None else ' ORDER BY created DESC'),
                                  (job_id,) if job_id is not None else ()).fetchall()
            break
    if rows is None:raise ValueError('注册表正在变化，未取得一致快照；没有修改或排队任务，请重试预览')
    result=[]
    for row in rows:
        value=dict(row)
        for name in ('config','manifest','progress'):value[name]=json.loads(value[name])
        result.append(value)
    if job_id is not None and not result:raise ValueError('任务不存在: '+str(job_id))
    return result

def _rebuild_config(old,profile,overrides):
    if not isinstance(old,dict):raise ValueError('旧任务配置必须为对象')
    if old.get('research_partition'):raise ValueError('分区训练任务须按原分区协议重建，不能直接重建全库任务')
    overrides={} if overrides is None else deepcopy(overrides)
    if not isinstance(overrides,dict):raise ValueError('迁移配置必须为JSON对象')
    if set(overrides)-set(DEFAULT):raise ValueError('迁移配置包含未知或身份字段，不能覆盖旧证据绑定: '+','.join(sorted(set(overrides)-set(DEFAULT))))
    if profile is not None and profile not in ('standard','smoke','routine','expanded'):raise ValueError('未知搜索规格')
    if profile is not None and 'profile' in overrides and overrides['profile']!=profile:raise ValueError('--profile 与迁移配置中的profile冲突')
    config={k:deepcopy(v) for k,v in old.items() if k not in ('engine_hash','created_version') and k not in DEPRECATED_CONFIG}
    # Absence in pre-0.6 jobs means validation, never the new historical default.
    config.setdefault('research_objective','validation')
    if profile is not None:
        config['profile']=profile
        if profile=='standard':
            config['directions']=list(DIRECTIONS)
            config.update(standard_thresholds({**config,**overrides}))
    # Explicit JSON overrides are validated last; no auto-correction hides a user's invalid value.
    config.update(overrides)
    checked=check_config(config)
    return config,checked

def _rebuild_plan(old,profile=None,config_overrides=None):
    from collections import Counter
    raw,config=_rebuild_config(old['config'],profile,config_overrides)
    engine=source_fingerprint();config.update(engine_hash=engine,created_version=VERSION)
    paths=[]
    for rec in old['manifest']['files']:
        p=Path(rec['path'])
        if not p.is_file() or sha(p)!=rec['sha256']:raise ValueError('原任务输入缺失或已改变；请重新检查数据，或使用import-audit导入原始输入')
        paths.append(str(p))
    if not paths:raise ValueError('原任务没有冻结输入，不能重建研究')
    # Both preview and application use an isolated inspection cache. Neither touches old workspaces.
    with tempfile.TemporaryDirectory(prefix='fsl_rebuild_inspect_') as scratch:
        manifest=inspect(paths,scratch)
    if Counter(r['sha256'] for r in manifest['files'])!=Counter(r['sha256'] for r in old['manifest']['files']):
        raise ValueError('质量清单或输入集合发生变化；请显式重新检查数据建立新任务')
    changes={k:{'before_present':k in old['config'],'before':old['config'].get(k),
                'after_present':k in config,'after':config.get(k)}
             for k in sorted(set(old['config'])|set(config))
             if k not in old['config'] or k not in config or old['config'][k]!=config[k]}
    warnings=[]
    if config['research_objective']=='validation':warnings.append('重建仍使用验证模式，留出'+str(config['holdout_months'])+'个月；不会自动切换全历史研究。')
    limits={k:config[k] for k in REBUILD_BUDGETS if config[k]}
    if limits:warnings.append('仍有冻结计算预算，可能暂停或得到PARTIAL；重建不等于完整搜索。')
    request_id='rebuild-'+old['id']+'-'+engine+'-'+digest(raw)
    plan={'source_job':old['id'],'source_status':old['status'],'league':old['league'],
          'source_engine_hash':old['config'].get('engine_hash'),'target_engine_hash':engine,
          'source_config_sha256':digest(old['config']),'source_manifest_sha256':digest(old['manifest']),
          'target_config_sha256':digest(config),'config':config,'config_changes':changes,
          'settings':{k:config[k] for k in REBUILD_SETTINGS},'budget_limits':limits,'warnings':warnings,
          'migration_options':{'profile':profile,'config_overrides':deepcopy(config_overrides or {})},
          'input_hashes':[r['sha256'] for r in manifest['files']],
          'automatic_research_started':False,'old_checkpoint_reused':False,'old_task_preserved':True}
    return plan,manifest,request_id

def rebuild_job(workspace,job_id,profile=None,*,config_overrides=None,dry_run=False):
    if type(dry_run) is not bool:raise ValueError('dry_run必须为布尔值')
    old=_read_rebuild_jobs(workspace,job_id)[0]
    plan,manifest,request_id=_rebuild_plan(old,profile,config_overrides)
    if dry_run:return {**plan,'status':'READY','dry_run':True,'queued':False}
    if source_fingerprint()!=plan['target_engine_hash']:raise ValueError('迁移期间引擎改变，请重新预览')
    store=Store(workspace);current=store.get(job_id)
    if current['config']!=old['config'] or current['manifest']!=old['manifest']:raise ValueError('迁移期间原任务配置或输入清单改变')
    with store.conn() as conn:previous=conn.execute('SELECT job FROM requests WHERE key=?',(request_id,)).fetchone()
    new=previous['job'] if previous else store.create(old['league'],plan['config'],manifest,start_paused=True,request_id=request_id)
    target=store.get(new)
    if target['config']!=plan['config']:raise ValueError('已有重建任务与预览配置不一致，拒绝重新绑定')
    # A deduplicated target can have several source jobs; never overwrite an earlier source's provenance.
    name='rebuild_provenance/'+digest([job_id,plan['source_config_sha256'],plan['source_manifest_sha256'],plan['target_config_sha256']])+'.json'
    proof={k:v for k,v in plan.items() if k!='source_status'}
    proof.update(schema='FSL_REBUILD_PROVENANCE_V2',job=new)
    saved=read_json(store.root/new/name)
    if saved is not None and saved!=proof:raise ValueError('已有迁移记录内容改变，拒绝覆盖')
    if saved is None:atomic_json(store.root/new/name,proof)
    primary=store.root/new/'rebuild_provenance.json'
    if not primary.exists():atomic_json(primary,proof)
    return {**plan,'job':new,'status':target['status'],'dry_run':False,'queued':False,
            'reused':previous is not None,'provenance_file':name}

def rebuild_stale_jobs(workspace,queue=False,profile=None,*,config_overrides=None,job_ids=None,dry_run=False):
    """Explicit, filterable migration. A preview has no registry/cache writes and never queues work."""
    if type(queue) is not bool or type(dry_run) is not bool:raise ValueError('queue/dry_run必须为布尔值')
    if job_ids is not None and (not isinstance(job_ids,(list,tuple)) or not job_ids or any(not isinstance(j,str) or not j for j in job_ids)):
        raise ValueError('任务筛选须为非空任务ID列表')
    jobs=_read_rebuild_jobs(workspace);current=source_fingerprint()
    wanted=set(job_ids) if job_ids is not None else None
    rebuilt=[];planned=[];failed=[];skipped=[]
    for missing in sorted((wanted or set())-{j['id'] for j in jobs}):
        failed.append({'source_job':missing,'error':'指定任务不存在','hint':'核对 --job，未匹配ID不会被当作成功。'})
    for job in jobs:
        reason=None
        if wanted is not None and job['id'] not in wanted:reason='NOT_SELECTED'
        elif job['config'].get('engine_hash')==current:reason='CURRENT_ENGINE'
        elif job['status'] not in ('QUEUED','PAUSED','INTERRUPTED') and not (job['status']=='ERROR' and 'SOURCE_REVISION_MISMATCH' in (job['error'] or '')):
            reason='STATE_NOT_ELIGIBLE'
        if reason:
            skipped.append({'source_job':job['id'],'source_status':job['status'],'reason':reason});continue
        result=None
        try:
            result=rebuild_job(workspace,job['id'],profile,config_overrides=config_overrides,dry_run=dry_run)
            result['would_queue']=queue
            if dry_run:planned.append(result);continue
            if queue:
                store=Store(workspace);new=store.get(result['job'])
                if new['status']=='PAUSED' and not new['progress']:
                    store.control(new['id'],'resume');result.update(status='QUEUED',queued=True)
                else:result['queue_note']='已有任务状态或进度不允许重新排队，保留现状。'
            rebuilt.append(result)
        except (ValueError,OSError,sqlite3.Error) as error:
            failed.append({'source_job':job['id'],'error_type':type(error).__name__,'error':str(error),
                           'hint':REBUILD_HINT,**({'job':result['job']} if result and 'job' in result else {})})
    successes=planned if dry_run else rebuilt
    status=('PARTIAL' if successes else 'FAIL') if failed else 'PASS' if successes else 'NO_ACTION'
    return {'status':status,'dry_run':dry_run,'queue_requested':queue,'rebuilt':rebuilt,'planned':planned,
            'failed':failed,'skipped':skipped,'counts':{'rebuilt':len(rebuilt),'planned':len(planned),'failed':len(failed),'skipped':len(skipped)},
            'note':'重建不复用旧断点、不清理旧目录；未显式覆盖的旧口径和预算继续保留。'}

def import_audit_bundle(archive,workspace=None,max_input_bytes=20*1024**3):
    archive=Path(archive).resolve();archive_hash=sha(archive);store=Store(workspace or ROOT/'workspace')
    base=(store.root/'imported_inputs').resolve();base.mkdir(exist_ok=True);target=base/archive_hash
    if target.exists() and target.resolve()!=target:raise ValueError('迁移输入目录不能是链接')
    with zipfile.ZipFile(archive) as z:
        names=[x.filename for x in z.infolist()]
        if len(names)!=len(set(names)):raise ValueError('审计ZIP存在重复条目')
        def metadata(name):
            info=z.getinfo(name)
            if info.file_size>16*1024**2:raise ValueError('审计元数据过大')
            return json.loads(z.read(name))
        manifest=metadata('input_manifest.json');config=metadata('config.json');audit=metadata('data_audit.json')
        if not all(isinstance(v,dict) for v in (manifest,config,audit)):raise ValueError('审计元数据必须是对象')
        legacy_objective_missing='research_objective' not in config
        # Match rebuild-job compatibility: pre-0.6 absence means validation.
        # Explicit modes are never inferred again or silently overwritten.
        config.setdefault('research_objective','validation')
        if config.get('research_partition'):raise ValueError('分区训练包不能自动转为全库研究；请按冻结分区协议迁移')
        records=manifest['files'];entries=[];seen=set();total=0
        for rec in records:
            h=rec.get('sha256','')
            if not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h):raise ValueError('原始输入缺少完整SHA-256')
            filename=PureWindowsPath(rec['path']).name
            if not filename or filename in ('.','..') or ':' in filename or not filename.lower().endswith('.csv'):raise ValueError('审计输入文件名不受支持')
            name=h[:12]+'_'+filename
            if name in seen:raise ValueError('输入文件名存在歧义')
            seen.add(name);info=z.getinfo('original_inputs/'+name);total+=info.file_size
            if info.file_size>max(1,info.compress_size)*3000:raise ValueError('审计输入压缩比异常')
            entries.append((rec,name,info))
        if total>max_input_bytes or total+256*1024**2>shutil.disk_usage(base).free:raise ValueError('审计输入超过迁移容量或磁盘余量')
        expected_hashes={r['sha256'] for r in records}
        def validate_inputs(directory):
            inspected=inspect([str(directory)],store.root/'import_cache')
            if {r['sha256'] for r in inspected['files']}!=expected_hashes:raise ValueError('导入目录含未登记或缺失的研究输入')
            if any(directory not in Path(r['path']).resolve().parents for r in inspected['files']):raise ValueError('迁移不能混入目录外的质量清单')
            if (audit['league'],config['company']) not in {(r['league'],r['company']) for r in inspected['leagues']}:raise ValueError('审计任务联赛/公司与原始CSV不符')
            return inspected
        if target.exists():
            if read_json(target/'migration_inputs.json',{}).get('archive_sha256')!=archive_hash:raise ValueError('已有输入目录缺少迁移身份')
            current_manifest=validate_inputs(target)
        else:
            with tempfile.TemporaryDirectory(prefix='import_',dir=base) as td:
                fresh=Path(td).resolve()
                if fresh.parent!=base or target.parent!=base:raise ValueError('输入迁移发布路径越界')
                for rec,name,info in entries:
                    h=hashlib.sha256()
                    with z.open(info) as src,(fresh/name).open('wb') as dst:
                        for chunk in iter(lambda:src.read(1024*1024),b''):dst.write(chunk);h.update(chunk)
                    if h.hexdigest()!=rec['sha256']:raise ValueError('审计输入哈希不符: '+name)
                if sha(archive)!=archive_hash:raise ValueError('迁移过程中原审计包改变')
                current_manifest=validate_inputs(fresh)
                atomic_json(fresh/'migration_inputs.json',{'archive_sha256':archive_hash,'original_records':records,'source_engine':config.get('engine_hash')})
                for rec in current_manifest['files']:rec['path']=str(target/Path(rec['path']).relative_to(fresh))
                current_manifest['input_paths']=[str(target)]
                current_manifest['quality']['files']=[str(target/Path(q).relative_to(fresh)) for q in current_manifest['quality']['files']]
                if target.exists():raise ValueError('目标在迁移期间已被创建，请重试校验')
                atomic_replace(fresh,target)
    from .common import DEPRECATED_CONFIG
    # Retired absolute gates from older audit bundles are dropped explicitly; the new job uses the current standard.
    config={k:v for k,v in config.items() if k not in ('engine_hash','created_version','research_partition') and k not in DEPRECATED_CONFIG}
    request_id='audit-import-'+archive_hash+'-'+source_fingerprint()
    with store.conn() as conn:previous=conn.execute('SELECT job FROM requests WHERE key=?',(request_id,)).fetchone()
    jid=previous['job'] if previous else store.create(audit['league'],config,current_manifest,start_paused=True,request_id=request_id)
    result={'status':store.get(jid)['status'],'job':jid,'version':VERSION,'archive_sha256':archive_hash,'input_directory':str(target),
            'legacy_objective_defaulted':legacy_objective_missing,'research_objective':store.get(jid)['config']['research_objective'],
            'scope':'Original inputs and settings imported; no previous search, roster, or certificate is certified under this version.',
            'automatic_research_started':False,'old_absolute_paths_opened':False}
    atomic_json(store.root/jid/'migration_provenance.json',result);return result

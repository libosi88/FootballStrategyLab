"""Persisted single-league workflow. All read/compute work happens locally."""
import shutil, time, traceback, os, gc
from .common import *
from .store import Store
from .data import load_inputs
from .mining import mine_direction,Paused,ResourcePaused,ACCELERATOR
from .locking import WorkspaceBusy
from .selection import select
from .verify import verify

LIMITATIONS=[
'standard按任务冻结的搜索语法（search_grammar：compact_v1 或 v3_full）计算，实际覆盖以本任务模块计数和S0—S7验收为准；未登记的扩展语法不在范围内。',
'原始表达发生数、规范化原子和历史掩码缓存分别记账；同批信号相同不代表未来逻辑等价。',
'组合采用冻结起点下加入/替换/移除比较，不宣称数学全局最优；预算耗尽明确保留未完成。',
'样本内共同比赛日重采样属于条件诊断，不能自动校正大量规则选择偏差。滚动和真正未见评测另行记录。',
'收益基于上传标签，未独立核验赛果/成交；没有真实公司行情与账户接口，真实下注和第二笔关闭。']

def run(workspace,jid):
 from .locking import WorkerSlot
 try:
  # One compute slot per job; the workspace parallel limit (default 1) bounds how many run at once.
  with WorkerSlot(workspace,Store(workspace).parallel_limit()):
   store=Store(workspace);store.recover()
   if not store.claim(jid,os.getpid()):return None
   return _run_claimed(workspace,jid)
 except WorkspaceBusy:
  return None

def _run_claimed(workspace,jid):
 store=Store(workspace);job=store.get(jid);jobdir=store.root/jid;cfg=job['config'];state={};journal=None
 frozen_execution=execution_fingerprint()
 def update(**kw):
  if journal is not None and kw.get('stage')=='S5':
   if journal.data['stages']['S4']['status']=='RUNNING':journal.finish('S4','PASS' if kw.get('review_complete',True) else 'PARTIAL')
   if journal.data['stages']['S5']['status']!='RUNNING':journal.start('S5')
  state.update(kw);state['updated_at']=time.time();store.update(jid,progress=state)
 activation_started=time.monotonic();last_resource_check=[0];pause_seen=[-1.0,False]
 def interrupted():
  # Search loops ask once per evaluation; the registry pause flag is read at most every half second.
  now=time.monotonic()
  if now-pause_seen[0]>=.5:pause_seen[0]=now;pause_seen[1]=bool(store.get(jid)['pause'])
  if pause_seen[1]:return True
  elapsed=time.monotonic()-activation_started
  if cfg.get('max_run_minutes',0) and elapsed>=cfg['max_run_minutes']*60:raise ResourcePaused('达到本次运行时长预算，断点保留；续跑重新计时，搜索节点预算不重置')
  return False
 def paused():
  if interrupted():return True
  elapsed=time.monotonic()-activation_started
  if elapsed-last_resource_check[0]>=10:
   last_resource_check[0]=elapsed
   from .resources import sample_output_size
   sample=sample_output_size(jobdir,interrupted);total=sample['generated_bytes']
   free=shutil.disk_usage(jobdir).free
   update(active_seconds=round(elapsed,1),**sample,free_disk_bytes=free,resource_checked_at=time.time())
   if cfg.get('max_output_mb',0) and total>=cfg['max_output_mb']*1048576:raise ResourcePaused('任务输出达到容量预算，断点保留；这是检查点软限额，新增更高预算任务才能扩容')
   if free<cfg.get('min_free_disk_mb',256)*1048576:raise ResourcePaused('工作区剩余磁盘不足，已在检查点暂停')
  return False
 try:
  if cfg['engine_hash']!=source_fingerprint():raise ValueError('[SOURCE_REVISION_MISMATCH] 任务冻结源码与当前源码不同，不能混用断点。请使用对应冻结源码或新建任务。')
  from .workflow_journal import StageJournal,verify_engine
  from .workflow_gates import module_coverage,completion_state,update_league_registry
  journal=StageJournal(jobdir,digest({'config':cfg,'manifest':job['manifest']}));journal.start('S0')
  store.update(jid,status='RUNNING',pid=__import__('os').getpid(),error='')
  update(stage='S0',message='校验只读输入和数据口径')
  cache=jobdir/'events.jsonl.gz'
  if cfg.get('research_partition') and not (cache.exists() and (jobdir/'prepared_manifest.json').exists()):
   raise ValueError('分区训练任务缺少冻结的训练子集（准备可能被中断），拒绝读取全量输入；请删除该训练任务并重新生成滚动折')
  if cache.exists() and (jobdir/'prepared_manifest.json').exists():
   from .data import verify_prepared_inputs
   verify_prepared_inputs(jobdir,cfg)
   for rec in job['manifest']['files']:
    if sha(rec['path'])!=rec['sha256']:raise ValueError('原始输入改变，必须新建版本')
   events=list(read_jsonl(cache));labels=read_json(jobdir/'labels.json');audit=read_json(jobdir/'data_audit.json');scale=audit['water_scale']
  else:
   events,labels,scale,audit=load_inputs(job['manifest'],job['league'],cfg['company'],update,should_pause=paused)
   from .history_policy import qualify_results
   audit['result_evidence']=qualify_results(labels,cfg)
   audit['counts']['eligible_matches']=audit['result_evidence']['eligible_matches']
   if cfg['profile']=='standard':
    from .standard_mining import ordered_standard_events
    events=ordered_standard_events(events);audit['event_order']='match,wall_minute,source_row,market,phase; cross-market reads strictly earlier minutes'
   if not events:raise ValueError('该联赛没有可解释的报价事件')
   for l in labels.values():l['league']=job['league']
   # Whole matches of the latest months are held out before any feature domain, candidate or roster exists.
   from .holdout import split_holdout,PLAN_FILE,HOLDOUT_FILES
   events,labels,held_events,held_labels,plan=split_holdout(events,labels,cfg);audit['holdout']=plan
   if not events:raise ValueError('该联赛留出样本外后没有可研究的报价事件')
   written=['events.jsonl.gz','labels.json','data_audit.json',PLAN_FILE]
   if plan['status']=='SPLIT':
    (jobdir/'holdout').mkdir(exist_ok=True);write_jsonl(jobdir/HOLDOUT_FILES[0],held_events);atomic_json(jobdir/HOLDOUT_FILES[1],held_labels);written+=list(HOLDOUT_FILES)
   del held_events,held_labels
   write_jsonl(cache,events);atomic_json(jobdir/'labels.json',labels);atomic_json(jobdir/'data_audit.json',audit);atomic_json(jobdir/PLAN_FILE,plan)
   atomic_json(jobdir/'prepared_manifest.json',{p:sha(jobdir/p) for p in written})
  if paused():raise Paused()
  holdout_plan=read_json(jobdir/'holdout_plan.json',{'status':'NOT_RECORDED'})
  journal.finish('S0',raw_events=len(events),eligible_matches=sum(l['eligible'] for l in labels.values()),input_hashes=[r['sha256'] for r in job['manifest']['files']],
                 holdout=holdout_plan.get('status'),holdout_cutoff=holdout_plan.get('cutoff'),holdout_matches=holdout_plan.get('holdout_matches'))
  journal.start('S1')
  if cfg['profile']=='standard':
   from .standard_mining import mine_standard
   engine_check=verify_engine(store.root,update,paused);shutil.copy2(engine_check['log'],jobdir/'engine_tests.log')
   engine_check={**engine_check,'log':'engine_tests.log'};atomic_json(jobdir/'engine_validation.json',engine_check)
   from .standard_feature_cache import prepare_standard_features
   feature_preparation=prepare_standard_features(jobdir,events,labels,scale,cfg,update,paused)
   atomic_json(jobdir/'standard_feature_preparation.json',feature_preparation)
   frozen={}
   for d in cfg['directions']:
    if paused():raise Paused()
    frozen[d]=mine_standard(jobdir,events,labels,scale,d,cfg,update,paused,prepare_only=True)
   from .standard_spec import spec_version_for,grammar_of
   atomic_json(jobdir/'search_spec.json',{'spec':spec_version_for(cfg),'grammar':grammar_of(cfg),'directions':frozen,'requested_directions':cfg['directions'],'all_16_directions':set(cfg['directions'])==set(DIRECTIONS),'extensions_included':grammar_of(cfg)=='v3_full'})
   journal.finish('S1',engine_validation=engine_check['status'],frozen_directions=len(frozen))
  else:journal.finish('S1',scope='legacy finite profile; not standard v3')
  journal.start('S2')
  update(stage='S1-S2',message='冻结有限字典并执行分方向批量枚举',raw_events=len(events),accelerator=ACCELERATOR)
  cov={'release':VERSION,'local_profile':cfg['profile'],'v3_status':'PARTIAL_CORE','limitations':LIMITATIONS,'directions':{},'units':{'line':4,'water':scale,'pnl':2*scale}}
  for d in cfg['directions']:
   st=mine_direction(jobdir,events,labels,scale,d,cfg,update,paused)
   # Quotes that could not be evaluated for this direction are accounted for, never silently dropped.
   if cfg['profile']=='standard':st={**st,'excluded_quote_rows':read_json(jobdir/'mining'/d/'standard_features.json',{}).get('excluded_quote_rows')}
   cov['directions'][d]=st
   update(search_directions_processed=len(cov['directions']),search_directions_total=len(cfg['directions']),search_directions_complete=sum(bool(s.get('standard_scope_complete',s['status'] in ('COMPLETE','NO_DATA'))) for s in cov['directions'].values()))
   atomic_json(jobdir/'coverage.json',cov)
  cov['local_search_complete']=all(s['status'] in ('COMPLETE','NO_DATA') for s in cov['directions'].values())
  cov['proven_zero']=sum(s.get('proven_zero',0) for s in cov['directions'].values())
  cov['planned_expressions']=sum(s.get('raw_total',s.get('total',0)) for s in cov['directions'].values())
  cov['equivalent_occurrences']=sum(s.get('equivalent_occurrences',0) for s in cov['directions'].values())
  cov['proven_nonprofitable']=sum(s.get('proven_nonprofitable',0) for s in cov['directions'].values())
  cov['evaluated']=sum(s.get('representatives',0) if cfg['profile']=='standard' else s.get('next',0) for s in cov['directions'].values());cov['candidates']=sum(s.get('candidates',0) for s in cov['directions'].values())
  cov['remaining']=cov['planned_expressions']-cov['evaluated']-cov['equivalent_occurrences']-cov['proven_zero']-cov['proven_nonprofitable']
  cov['standard_search_complete']=cfg['profile']=='standard' and set(cfg['directions'])==set(DIRECTIONS) and all(s.get('standard_scope_complete',False) for s in cov['directions'].values())
  if cfg['profile']=='standard':cov.pop('v3_status',None);cov['scope_stage']='S2_SEARCH_ACCOUNTING; final S0-S7 status is in workflow_status.json'
  if cov['remaining']<0:raise RuntimeError('搜索覆盖计数不一致')
  atomic_json(jobdir/'coverage.json',cov)
  if cfg['profile']=='standard':
   evidence=module_coverage(jobdir,cov,engine_check)
   if not evidence['evidence_complete']:raise ValueError('标准搜索证据缺失，不能继续冻结候选池：'+canonical(evidence['evidence_gaps']))
  journal.finish('S2','PASS' if cov['local_search_complete'] else 'PARTIAL',planned=cov['planned_expressions'],remaining=cov['remaining'])
  journal.start('S3');atomic_json(jobdir/'candidate_pool_freeze.json',{'coverage_sha256':sha(jobdir/'coverage.json'),'complete':cov['local_search_complete'],'standard_complete':cov['standard_search_complete'],'profit_threshold':cfg['min_profit'],'candidate_occurrences':cov['candidates']})
  journal.finish('S3','PASS' if cov['local_search_complete'] else 'PARTIAL',candidate_occurrences=cov['candidates'])
  journal.start('S4');update(stage='S4',message='冻结候选池并执行全池审查',evaluated_total=cov['evaluated'],candidates_total=cov['candidates'])
  if paused():raise Paused()
  summary=select(jobdir,events,labels,scale,cfg,update,paused)
  if journal.data['stages']['S4']['status']=='RUNNING':journal.finish('S4','PASS' if summary.get('standard_review',{}).get('complete',True) else 'PARTIAL')
  if cfg['profile']=='standard' and summary.get('standard_review',{}).get('complete',True):
   # The review builds its own column cache per direction; a finished review no longer needs it.
   from .standard_mining import release_direction_caches
   for d in cfg['directions']:
    release_direction_caches(jobdir/'mining'/d,{'status':'COMPLETE'},cfg,update,('review_columns',))
  journal.finish('S5','PASS' if summary['selection_status']=='FINITE_LOCAL_SEARCH_COMPLETE' else 'PARTIAL',selected=summary['selected'],backup=summary['backup_selected'])
  # Selection has persisted all inputs and references. Release its large raw object graph
  # before verification and the independent ZIP subprocess allocate their own input.
  del events
  gc.collect()
  update(stage='S6',message='逐报价触发回放、保存恢复和信号/订单对照',selected=summary['selected'])
  journal.start('S6')
  from .handoff_assets import write_assets
  write_assets(jobdir)
  vr=verify(jobdir,should_pause=paused)
  if vr['status']=='FAIL':raise RuntimeError('触发器与黄金结果不一致')
  if cfg['profile']=='standard' and vr.get('execution_economics',{}).get('status')!='PASS':raise RuntimeError('实际执行经济资格未通过，拒绝将机械回放PASS作为交接验收')
  # The frozen, verified roster meets the held-out latest months exactly once; nothing measured there can reselect.
  from .holdout import evaluate_holdout
  holdout=evaluate_holdout(jobdir,cfg,cov,update,paused)
  journal.finish('S6',verification=vr['status'],holdout=holdout['status'])
  if paused():raise Paused()
  engine_check=read_json(jobdir/'engine_validation.json')
  if cfg['engine_hash']!=source_fingerprint():raise ValueError('[SOURCE_CHANGED_DURING_RUN] 本次研究期间源码被修改；这是版本漂移保护，结果未被认证。请停止编辑后使用固定源码重新验证。')
  if execution_fingerprint()!=frozen_execution:raise ValueError('执行或验收代码在运行期间改变；保留研究断点，但必须重新进行S6/S7验收')
  if cfg['profile']=='standard':
   evidence=module_coverage(jobdir,cov,engine_check,vr)
   if not evidence['evidence_complete']:raise ValueError('标准搜索证据缺失，拒绝交付：'+canonical(evidence['evidence_gaps']))
  package=cfg.get('package_handoff',True)
  completion=completion_state(cov,summary,vr,engine_check,config=cfg,holdout=holdout,packaging_skipped=not package)
  atomic_json(jobdir/'workflow_status.json',completion)
  update(stage='S7',message='独立目录验收与自包含打包' if package else '探索运行：按配置不打交接包，写出研究结论')
  journal.start('S7')
  if package:
   # results/最终决定.md is written by package_run after the actual fresh-directory verification.
   from .packaging import package_run
   packages,completion=package_run(jobdir,summary,cov,vr,engine_check,journal,update,paused,holdout=holdout)
  else:
   # Exploration runs keep every result, verification and conclusion but build no A/B archives.
   from .reporting import final_decision_markdown,gap_notes_markdown
   packages={};validation={'status':'NOT_RUN','reason':'package_handoff=false'}
   (jobdir/'results'/'最终决定.md').write_text(final_decision_markdown(jobdir,summary,cov,vr,completion,validation),encoding='utf-8')
   (jobdir/'results'/'缺口说明.md').write_text(gap_notes_markdown(completion,cov),encoding='utf-8')
   journal.finish('S7','NOT_APPLICABLE',reason='package_handoff=false')
  workflow_state=completion['state'];final={**summary,**completion,'coverage':cov,'verification':vr,'packages':packages}
  terminal='DONE' if workflow_state in ('FINITE_WORKFLOW_DONE','STANDARD_HANDOFF_COMPLETE','RESEARCH_COMPLETE_NO_HANDOFF','HISTORICAL_RESEARCH_COMPLETE','HISTORICAL_RESEARCH_COMPLETE_NO_HANDOFF') else 'PARTIAL_RESULT'
  atomic_json(jobdir/'summary.json',final);update(stage=terminal,message=('历史研究与触发核对完成，是否找到合格策略见最终决定' if workflow_state.startswith('HISTORICAL_RESEARCH_COMPLETE') else '本版标准研究与触发交接验收完成' if workflow_state=='STANDARD_HANDOFF_COMPLETE' else '研究与触发核对完成（按配置未打交接包）' if workflow_state=='RESEARCH_COMPLETE_NO_HANDOFF' else '所声明有限范围流程完成' if workflow_state=='FINITE_WORKFLOW_DONE' else '已导出部分结果；未完成项见覆盖账和完成门禁'),selected=summary['selected'])
  store.update(jid,status=terminal,pid=0)
  return final
 except ResourcePaused as ex:
  if journal is not None:journal.pause(ex)
  update(message=str(ex));store.update(jid,status='PAUSED',pid=0,error=str(ex))
 except Paused:
  if journal is not None:journal.pause('用户暂停；已提交断点保留')
  update(message='已保存评价游标/组合选择断点。已完成方向的基础指标/邻域可校验恢复；当前未完成方向重建，组合比较从已提交动作继续。');store.update(jid,status='PAUSED',pid=0)
 except Exception:
  err=traceback.format_exc();(jobdir/'error.log').write_text(err,encoding='utf-8');store.update(jid,status='ERROR',error=err[-12000:],pid=0)
  if journal is not None:journal.current_failure(err[-4000:])
  raise
 finally:
  from .workflow_gates import update_league_registry
  # The league registry is a derived summary; failing to refresh it must not turn a finished job into an error.
  try:update_league_registry(store)
  except Exception as error:(store.root/'league_registry_error.log').write_text(str(error),encoding='utf-8')

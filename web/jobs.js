function exactCount(value){
  if(value==null)return '—';
  try{return BigInt(value).toLocaleString('zh-CN')}catch{return String(value)}
}
function progressPercent(done,total){
  try{const d=BigInt(done||0),t=BigInt(total||0);return t>0?Math.min(100,Number(d*10000n/t)/100):0}catch{return 0}
}
let renderedJobsKey=null;
function renderJobs(s){
  lastJobs=s.jobs;
  if($('app-version'))$('app-version').textContent=s.version;
  if($('footer-version'))$('footer-version').textContent='FSL '+s.version;
  $('workspace').textContent='任务存储位置（目录名不代表软件版本）：'+s.workspace;
  $('count').textContent=s.jobs.length+' 个任务';
  const health=$('scheduler-health');
  if(health){
    const h=s.scheduler||{},problem=h.state==='DEGRADED'||h.state==='STOPPED';
    health.hidden=!problem;
    health.textContent=h.state==='DEGRADED'?'调度异常，正在重试：'+(h.last_error||'未知错误')+(h.last_log_error?'；错误日志也未能写入。':''):h.state==='STOPPED'?'调度已停止，排队任务不会自动开始。请核对工作台状态。':'';
  }
  const jobsKey=JSON.stringify(s.jobs);if(jobsKey===renderedJobsKey)return;renderedJobsKey=jobsKey;
  if(!s.jobs.length){$('jobs').replaceChildren(text('p','还没有任务。先检查数据或载入合成演示。','empty'));$('jobselect').replaceChildren(new Option('选择有结果的任务',''));return;}
  const active=document.activeElement,focus=$('jobs').contains(active)?{job:active.closest('.job')?.dataset.jobId,tag:active.tagName,key:active.getAttribute('href')||active.textContent}:null;
  const errorOpen=new Set([...$('jobs').querySelectorAll('.job details[open]')].map(d=>d.closest('.job').dataset.jobId));
  const old=$('jobselect').value,historyOpen=$('historyjobs')?.open||false;const history=document.createElement('details');history.id='historyjobs';history.open=historyOpen;history.append(text('summary','历史版本任务（'+s.jobs.filter(j=>j.previous_version).length+'）'));if(!s.jobs.some(j=>!j.previous_version))history.open=historyOpen;
  $('jobs').replaceChildren();$('jobselect').replaceChildren(new Option('选择有结果的任务',''));
  for(const j of s.jobs){
    const box=text('div','','job'),title=text('div','','jobtitle');
    box.dataset.jobId=j.id;
    const inactiveHistory=j.previous_version&&!['RUNNING','PAUSING','QUEUED'].includes(j.status);
    const historical=j.research_objective==='historical',complete=['STANDARD_HANDOFF_COMPLETE','HISTORICAL_RESEARCH_COMPLETE'].includes(j.summary?.state),researchOnly=['RESEARCH_COMPLETE_NO_HANDOFF','HISTORICAL_RESEARCH_COMPLETE_NO_HANDOFF'].includes(j.summary?.state);
    title.append(text('b',j.league+' / '+(j.company||'')),text('span',j.previous_version?'历史版本任务':complete?(historical?'历史研究完成':'标准研究交接完成'):researchOnly?'研究完成（未打交接包）':states[j.status]||j.status,'badge'+(['ERROR','PARTIAL_RESULT'].includes(j.status)?' warn':'')));
    box.append(title,text('div',j.id+' · '+j.profile,'hint'));
    if(j.profile!=='standard')box.append(text('p','旧版回归结果：字典和执行名额口径与当前标准版不同，不作为标准研究的效果结论。','hint'));
    const p=j.progress||{};if(p.active_seconds!=null)box.append(text('p',`本次运行 ${(p.active_seconds/60).toFixed(1)} 分钟 · 已生成 ${(p.generated_bytes/1e9).toFixed(2)} GB · 工作盘余量 ${(p.free_disk_bytes/1e9).toFixed(1)} GB（检查点采样）`,'hint'));box.append(text('p',`冻结预算：每方向 ${j.budgets?.standard_node_budget||'不限'} 节点；单次 ${j.budgets?.max_run_minutes||'不限'} 分钟。`,'hint'));
    box.append(text('div',`${inactiveHistory?'上次进度记录：':''}${p.stage||(inactiveHistory?'无阶段记录':'排队')} · ${p.message||(inactiveHistory?'旧任务已停止推进':'等待计算资源')}`,'hint'));
    if(p.total_rules&&['S2','S1-S2'].includes(p.stage)){
      const bar=text('div','','progress'),fill=document.createElement('i');
      fill.style.width=progressPercent(p.evaluated,p.total_rules)+'%';bar.append(fill);
      box.append(bar,text('div',`${p.direction||''} ${p.standard_module||''} · 当前方向已覆盖 ${exactCount(p.evaluated||0)} / ${exactCount(p.total_rules)} 条表达`,'muted'));
    }
    if(p.dictionary_atoms!=null)box.append(text('div',`${s.directions?.[p.direction]||p.direction||''} · 已生成原子 ${exactCount(p.dictionary_atoms)} · ${p.dictionary_feature||''}`,'hint'));
    if(p.search_directions_total!=null)box.append(text('p',`已处理至预算或结束的方向 ${p.search_directions_processed}/${p.search_directions_total}；完整覆盖 ${p.search_directions_complete}/${p.search_directions_total}。这不是 S0—S7 总进度。`,'hint'));
    if(p.feature_events_total!=null)box.append(text('div',`报价特征 ${exactCount(p.feature_events)} / ${exactCount(p.feature_events_total)} · 全部方向共用一次计算`,'hint'));
    if(p.mask_atoms_total!=null && /计算完整事件原子掩码/.test(p.message||''))box.append(text('div',`条件命中缓存 ${exactCount(p.mask_atoms)} / ${exactCount(p.mask_atoms_total)}`,'hint'));
    if(p.group_rows_total!=null && /成员|分组/.test(p.message||''))box.append(text('div',`成员校验与分组 ${exactCount(p.group_rows)} / ${exactCount(p.group_rows_total)}`,'hint'));
    if(p.search_nodes!=null && /按v3冻结空间搜索/.test(p.message||''))box.append(text('div',`实际访问搜索节点 ${exactCount(p.search_nodes)}`,'hint'));
    if(p.selection_phase==='SINGLETON_PRECHECK')box.append(text('div',`组合起点评分 ${exactCount(p.selection_singletons_evaluated)} / ${exactCount(p.selection_singletons_total)}`,'hint'));
    if(p.risk_pairs_total!=null)box.append(text('div',`风险比较 ${exactCount(p.risk_pairs)} / ${exactCount(p.risk_pairs_total)}`,'hint'));
    const stages=Object.entries(j.stages||{});
    if(stages.length){
      const names={S0:'输入',S1:'规格与测试',S2:'规则搜索',S3:'净胜池',S4:'全池复核',S5:'名单',S6:'触发核对',S7:'交接包'};
      const status={PASS:'通过',PENDING:'待执行',RUNNING:'执行中',PARTIAL:'未完成',FAIL:'失败',BLOCKED:'阻塞',NOT_APPLICABLE:'不适用'};
      box.append(text('p',(inactiveHistory?'上次阶段记录：':'')+stages.map(([k,v])=>`${k} ${names[k]}：${inactiveHistory&&v.status==='RUNNING'?'上次停留于此（未完成）':status[v.status]||v.status}`).join(' · '),'hint'));
    }
    const row=text('div','','row');for(const name of j.worker_logs||[]){const link=text('a','下载进程日志','button secondary');link.href=download(j.id,name);row.append(link)}
    if(!j.previous_version){const configLink=text('a','下载冻结配置','button secondary');configLink.href=download(j.id,'config.json');row.append(configLink);}
    if(j.previous_version){
      box.append(text('p',`旧任务当前状态：${states[j.status]||j.status}。${inactiveHistory&&!j.pid?'当前未登记计算进程；阶段文件保留上次进度，不表示仍在运行。':''}`,'hint'));
      box.append(text('p',`历史版本任务，当前版本不可续跑。此任务冻结于 v${j.created_version}，当前软件为 v${s.version}。原始断点保留；可用 rebuild-job 重建为暂停的新任务，或使用对应旧源码复核。`,'hint'));
      box.append(text('p','重建默认保留旧研究模式和预算。可用 --dry-run 查看参数差异；切换全历史研究需显式配置。','hint'));
      const link=text('a','下载原任务配置','button secondary');link.href=download(j.id,'config.json');row.append(link);
    }
    if(['RUNNING','QUEUED'].includes(j.status)||!j.previous_version&&['PAUSED','ERROR','INTERRUPTED'].includes(j.status)){
      const action=['RUNNING','QUEUED'].includes(j.status)?'pause':'resume';
      const button=text('button',action==='pause'?'暂停':'从断点继续','secondary');
      button.onclick=async()=>{try{await api('/api/control',{job:j.id,action});refresh()}catch(e){toast(e.message)}};row.append(button);
      if(action==='resume')box.append(text('p','续跑会核对冻结的代码与数据哈希。','hint'));
    }
    if(j.summary){
      const m=j.summary,c=m.coverage,metrics=text('div','','metric');
      for(const [v,label] of [[c.candidates,'净胜候选发生数'],[m.selected,'模拟主名单'],[m.backup_selected,'较低风险备选']]){
        const q=text('div','');q.append(text('b',exactCount(v)),text('span',label));metrics.append(q);
      }
      box.append(metrics,text('p',`标准状态：${m.v3_status}。未来独立验证与真实执行另行记录。`,'hint'));
      const rec=m.recommendation||{},hold=m.holdout||{},verdicts={CONFIRMED:'确认为正',FAILED:'未通过',INSUFFICIENT_SAMPLE:'订单不足，无法确认',EMPTY_ROSTER:'名单为空',UNAVAILABLE_SHORT_HISTORY:'数据太短，未留出',UNAVAILABLE_NO_RECENT_MATCHES:'最近12个月无比赛',DISABLED:'已关闭',NOT_RECORDED:'无留出记录'};
      if(historical&&rec.status)box.append(text('p',({'HISTORICAL_SELECTION_READY':'历史稳定高净胜名单已生成','HISTORICAL_SEARCH_EMPTY':'历史检索完成，没有符合本次稳定性条件的策略','PARTIAL_HISTORICAL_RESULT':'历史研究尚未完成'})[rec.status]||rec.status,'notice'));
      if(!historical&&rec.status)box.append(text('p',(rec.status==='RECOMMENDED_FOR_PAPER_TRADING'?'结论：推荐进入自动模拟（不是实盘批准）':'结论：仅观察，不推荐进入模拟')+(rec.reasons?.length?'。原因：'+rec.reasons.join('；'):''),rec.status==='RECOMMENDED_FOR_PAPER_TRADING'?'hint':'notice'));
      if(!historical&&hold.status)box.append(text('p',`样本外（最近12个月留出，名单冻结后评测一次）：${verdicts[hold.main?.verdict||hold.status]||hold.main?.verdict||hold.status}`+(hold.main?` · 订单 ${hold.main.orders??'—'} · 净胜 ${displayValue('net',hold.main.net)} · ROI ${displayValue('roi',hold.main.roi)}`:''),'hint'));
      if(m.coverage_ratio!=null)box.append(text('p',`冻结搜索语法覆盖 ${(m.coverage_ratio*100).toFixed(4)}%`+(m.coverage_ratio<1?'；未算完，名单只能观察':''),m.coverage_ratio<1?'notice':'hint'));
      if(c.planned_expressions!=null)box.append(text('p',`冻结表达 ${exactCount(c.planned_expressions)} · 直接评价 ${exactCount(c.evaluated)} · 历史等价映射 ${exactCount(c.equivalent_occurrences||0)} · 零命中证明 ${exactCount(c.proven_zero)} · 安全上界排除 ${exactCount(c.proven_nonprofitable||0)} · 待评价 ${exactCount(c.remaining)}`,'hint'));
      const gateNames={full_standard_search:'完整标准搜索',engine_tests:'引擎测试',all_pool_review:'全池审查',selection:'组合比较',trigger:'触发核对',nonempty_roster:'非空主名单验收',rule_cases:'规则样例',persistent_paper:'持久化模拟',streaming_paper:'完整流式模拟',execution_economics:'实际执行收益与风险',packaging:'空目录打包',standard_research_thresholds:'标准研究门槛未改动',holdout_evaluation:'样本外评测已执行'};
      const missing=Object.entries(m.gates||{}).filter(([k,v])=>!v&&!(historical&&k==='nonempty_roster')).map(([k])=>gateNames[k]||k);
      if(missing.length)box.append(text('p','未通过项：'+missing.join('、'),'error'));
      if(m.empty_roster_chain_verified)box.append(text('p',historical?'规定范围的历史搜索与筛选已完成，没有满足本次条件的策略。':'搜索及空结果流程已完成；没有主名单，本任务不能认证非空交接链路。','hint'));
      else if(j.status==='PARTIAL_RESULT')box.append(text('p','当前为部分范围观察结果。冻结预算不能通过反复续跑绕过；调整预算需要新建研究版本。','hint'));
      for(const [file,label,dir] of [...(m.packages?.developer?[[m.packages.developer,'开发交接包','packages/']]:[]),...(m.packages?.audit?[[m.packages.audit,'完整审计包','packages/']]:[]),['workflow_status.json','完成门禁',''],['coverage_modules.json','模块覆盖账','']]){
        const link=text('a',label,'button secondary');link.href=download(j.id,dir+file);row.append(link);
      }
    }
    if(j.error){const detail=document.createElement('details');detail.open=errorOpen.has(j.id);detail.append(text('summary','错误详情'),text('pre',j.error,'error'));box.append(detail)}
    box.append(row);(j.previous_version?history:$('jobs')).append(box);
    if(['DONE','PARTIAL_RESULT'].includes(j.status)||['S6','S7'].includes(p.stage)){const option=new Option(j.league+' · v'+j.created_version+' · '+j.profile+' · '+(states[j.status]||j.status)+' · '+j.id,j.id);option.dataset.objective=j.research_objective||'validation';$('jobselect').append(option)}
  }
  if(history.children.length>1)$('jobs').append(history);
  if([...$('jobselect').options].some(o=>o.value===old))$('jobselect').value=old;
  if(focus){const next=[...$('jobs').querySelectorAll('a,button,summary')].find(el=>el.closest('.job')?.dataset.jobId===focus.job&&el.tagName===focus.tag&&(el.getAttribute('href')||el.textContent)===focus.key);next?.focus({preventScroll:true});}
}

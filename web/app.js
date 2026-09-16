const $=id=>document.getElementById(id);let manifest=null,lastJobs=[],initialized=false,scanId=null;let submitting=false,requestKey=null,requestBody=null,previewOffset=0;
const jobConfigCache=new Map();let settingsTimer=null,toastTimer=null,interfaceStopped=false,connectionReady=false,lastSuccessAt=null,refreshRunning=false;
let previewController=null,previewRequest=0,demoRestore=null,shutdownArmed=false;
const states={QUEUED:'排队中',RUNNING:'实际计算中',PAUSING:'正在保存断点',PAUSED:'已暂停',INTERRUPTED:'已中断／可续跑',DONE:'有限流程完成',PARTIAL_RESULT:'部分结果／尚未完成',ERROR:'运行失败'};
function atLeastVersion(version,target){const a=String(version||'0').split('.').map(Number),b=target.split('.').map(Number);for(let i=0;i<b.length;i++){if((a[i]||0)!==b[i])return (a[i]||0)>b[i]}return true}
function initialResearchSettings(state){
 const saved={...(state.research_settings||{})};
 if(saved.research_objective){
  // Settings saved before 0.6.1 carry its 12-move and 200000-evaluation portfolio caps, which stopped full-history searches before a local optimum.
  if(!atLeastVersion(state.research_settings_version,'0.6.1'))for(const key of ['select_budget','selection_eval_budget'])saved[key]=(state.defaults_by_objective?.[saved.research_objective]||state.default_config)[key];
  if(!atLeastVersion(state.research_settings_version,'0.6.3')&&saved.research_objective==='historical'&&String(saved.min_profit)==='0')saved.min_profit=(state.defaults_by_objective?.historical||state.default_config).min_profit;
  return {...(state.defaults_by_objective?.[saved.research_objective]||state.default_config),...saved};
 }
 // Old 0.5 preferences are not an explicit request for its validation policy. Preserve resource choices only.
 const settings={...state.default_config};
 // Research budgets saved by 0.5 (for example 8192 nodes or 120 minutes) would cut or pause full-history jobs; only machine resource choices carry over.
 for(const key of ['max_mask_mb','max_feature_cache_mb','quote_cache_entries','chunk_size','package_handoff'])if(key in saved)settings[key]=saved[key];
 return settings;
}
function text(tag,s,cls){const e=document.createElement(tag);e.textContent=s;if(cls)e.className=cls;return e}
function toast(s){clearTimeout(toastTimer);$('toast').textContent=s;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,7000)}
async function api(path,body,{signal}={}){
 const controller=new AbortController(),cancel=()=>controller.abort();
 if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
 const timer=path==='/api/browse'?null:setTimeout(cancel,15000);
 try{const r=await fetch(path,{signal:controller.signal,method:body?'POST':'GET',headers:{'X-Local-Token':window.LOCAL_TOKEN,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
 catch(error){if(error.name==='AbortError'&&!signal?.aborted)throw Error(path.startsWith('/api/preview')?'结果预览超时，请重试或下载完整文件。':'服务请求超时；提交结果可能已保存，请先查看任务状态，重试将沿用请求标识');throw error}
 finally{if(timer!==null)clearTimeout(timer);signal?.removeEventListener('abort',cancel)}
}
function download(jid,path){return '/download/'+encodeURIComponent(jid)+'/'+path.split('/').map(encodeURIComponent).join('/')}
function showInputs(m){
 manifest=m;$('leaguefilters').hidden=false;
 const countries=[...new Set(m.leagues.flatMap(r=>r.countries||[]))].sort();
 $('countryfilter').replaceChildren(new Option('全部国家 / 地区',''),...countries.map(c=>new Option(c,c)));
 const companies=[...new Set(m.leagues.map(r=>r.company))].sort();
 $('companyfilter').replaceChildren(new Option('全部公司',''),...companies.map(c=>new Option(c,c)));
 if(companies.includes('皇冠'))$('companyfilter').value='皇冠';
 $('qualitysummary').textContent=m.quality?.note||'未发现质量交割清单，结果为上传标签试算。';
 renderTargets();
}
function renderTargets(){
 if(!manifest)return;
 const previous=$('inputs').querySelector('input:checked');const keep=previous?previous.value+'|'+previous.dataset.company:null;
 const query=$('leaguefilter').value.trim().toLocaleLowerCase(),country=$('countryfilter').value,company=$('companyfilter').value;
 const visible=manifest.leagues.filter(r=>(!country||(r.countries||[]).includes(country))&&(!company||r.company===company)&&(!query||[r.league,...(r.file_groups||[])].join(' ').toLocaleLowerCase().includes(query)));
 $('catalogsummary').textContent=`找到 ${visible.length} / ${manifest.leagues.length} 个联赛与公司组合。每次单选一个联赛，合并该联赛所选公司的全部可用年份。`;
 $('inputs').replaceChildren();
 for(const r of visible){
  const l=text('label','','leaguecheck'),c=document.createElement('input');c.type='radio';c.name='league';c.value=r.league;c.dataset.company=r.company;
  c.checked=keep===r.league+'|'+r.company||manifest.leagues.length===1;
  c.onchange=()=>{$('create').disabled=!connectionReady||Boolean(scanId)||!$('inputs').querySelector('input:checked')};
  l.append(c,document.createTextNode(`${r.league} / ${r.company} · ${r.matches??'—'} 场 · ${r.rows.toLocaleString()} 条报价`));
  l.append(text('small',`${(r.countries||[]).join(' / ')} · 年份 ${(r.years||[]).join('、')} · 文件组 ${(r.file_groups||[]).join('、')}`));
  if(r.date_min)l.append(text('small',`${r.date_min} 至 ${r.date_max} · ${r.files} 份指数文件`));
  $('inputs').append(l);
 }
 if(!visible.length)$('inputs').append(text('p','没有匹配的联赛，请调整筛选。','hint'));
 $('create').disabled=!connectionReady||Boolean(scanId)||!$('inputs').querySelector('input:checked');
}
for(const name of ['countryfilter','companyfilter'])$(name).onchange=renderTargets;
$('leaguefilter').oninput=renderTargets;
function invalidateInput(){manifest=null;$('create').disabled=true;$('inputs').replaceChildren(text('p','目录已改变，请重新检查数据。','hint'));$('leaguefilters').hidden=true;}
$('paths').oninput=invalidateInput;
async function inspectInput(){
 try{
  invalidateInput();$('inspect').disabled=true;$('create').disabled=true;$('demo').disabled=true;$('browse').disabled=true;$('paths').disabled=true;
  const paths=$('paths').value.split('\n').map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean);
  const scan=await api('/api/inspect',{paths,background:true});scanId=scan.scan_id;$('cancelinspect').hidden=false;
  while(scanId){
   const result=await api('/api/inspection?id='+encodeURIComponent(scanId));const p=result.progress||{};
   const gb=n=>(Number(n||0)/1e9).toFixed(2);
   $('scanprogress').textContent=`已检查 ${p.completed_files||0} / ${p.total_files||0} 个文件 · ${gb(p.processed_bytes)} / ${gb(p.total_bytes)} GB · ${(p.rows||0).toLocaleString()} 条报价。${p.current_file||''}${p.file_rows?'（当前文件 '+p.file_rows.toLocaleString()+' 行）':''}`;
   if(result.status==='COMPLETE'){scanId=null;showInputs(result.manifest);$('scanprogress').textContent+=' 扫描完成，可选择联赛。';break}
   if(result.status!=='RUNNING')throw Error(result.error||'扫描未完成');
   await new Promise(resolve=>setTimeout(resolve,900));
  }
 }catch(e){toast(e.message);$('scanprogress').textContent=e.message}
 finally{scanId=null;$('inspect').disabled=false;$('demo').disabled=false;$('browse').disabled=false;$('paths').disabled=false;$('cancelinspect').hidden=true;if(manifest)renderTargets()}
}
$('inspect').onclick=inspectInput;
$('cancelinspect').onclick=async()=>{try{if(scanId)await api('/api/inspection/cancel',{scan_id:scanId})}catch(e){toast(e.message)}};
$('browse').onclick=async()=>{try{let x=await api('/api/browse',{});if(x.path){$('paths').value=x.path;invalidateInput()}}catch(e){toast('无法弹出目录窗口，请直接粘贴目录路径。'+e.message)}};
$('demo').onclick=async()=>{try{showInputs(await api('/api/demo',{}));demoRestore=demoRestore||{profile:$('profile').value,objective:$('research_objective').value};$('profile').value='smoke';$('profile').dispatchEvent(new Event('change'));$('paths').value=(manifest.input_paths||[]).join('\n');toast('演示使用明确标注的合成比赛和旧版快速规格，不是真实盈利数据；创建演示任务后界面会恢复原研究设置。')}catch(e){toast(e.message)}};
$('create').onclick=async()=>{if(submitting)return;submitting=true;$('create').disabled=true;try{if(!manifest)throw Error('请先检查当前输入目录');const checks=[...$('inputs').querySelectorAll('input:checked')];if(checks.length!==1)throw Error('请单选一个联赛');if(new Set(checks.map(x=>x.dataset.company)).size>1)throw Error('一次任务批次只能使用同一家公司');let config={profile:$('profile').value,company:checks[0].dataset.company};for(const k of ['min_matches','min_matches_ceiling','match_cap','select_budget','max_mask_mb','selection_eval_budget','standard_node_budget','standard_review_budget','max_pool_pairs','max_feature_cache_mb','max_run_minutes','max_output_mb','holdout_months','holdout_min_orders','min_history_years'])config[k]=Number($(k).value);for(const k of ['min_matches_share','min_segment_share','min_return_drawdown_ratio','portfolio_min_return_drawdown_ratio','conservative_portfolio_min_return_drawdown_ratio','selection_profit_tolerance_share','prematch_trigger_status','search_grammar','research_objective','min_profit'])config[k]=$(k).value;config.package_handoff=$('package_handoff').checked;config.historical_diagnostics=$('historical_diagnostics').checked;Object.assign(config,readHistoricalPolicyControls());{const state=await api('/api/state');config.directions=Object.keys(state.directions).filter(d=>$('profile').value==='standard'||$('supplement').checked||!['HOME','AWAY'].includes(d.split('_').slice(1).join('_')))}const payload={manifest_id:manifest.manifest_id,leagues:checks.map(x=>x.value),config,paths:$('paths').value.split('\n').map(x=>x.trim().replace(/^"|"$/g,'')).filter(Boolean)};const encoded=JSON.stringify(payload);if(encoded!==requestBody){requestBody=encoded;requestKey=crypto.randomUUID()}await api('/api/create',{...payload,request_id:requestKey,persist_settings:!demoRestore});requestBody=null;requestKey=null;if(demoRestore){demoRestore=null;toast('演示任务已创建；界面即将恢复原研究设置。');setTimeout(()=>location.reload(),1500);return}toast('该联赛已加入独立研究任务。结果与其他联赛分开保存。');await refresh()}catch(e){toast(e.message)}finally{submitting=false;$('create').disabled=!connectionReady||!manifest||Boolean(scanId)||!$('inputs').querySelector('input:checked')}};
async function refresh(){
 if(interfaceStopped||refreshRunning)return;refreshRunning=true;
 try{
  const s=await api('/api/state');
  connectionReady=true;lastSuccessAt=new Date();$('connectionstatus').textContent='连接正常 · 最后状态更新 '+lastSuccessAt.toLocaleTimeString();$('connectionstatus').className='hint';$('jobs').classList.remove('stale-data');
  if(!initialized){
   initialized=true;let settings=initialResearchSettings(s);for(const [k,v] of Object.entries(settings)){const el=$(k);if(el&&['INPUT','SELECT'].includes(el.tagName)){if(el.type==='checkbox')el.checked=Boolean(v);else el.value=v}}if($('max_parallel_jobs')&&s.scheduler)$('max_parallel_jobs').value=s.scheduler.max_parallel_jobs;document.querySelectorAll('.panel input,.panel select').forEach(el=>{if(!el.id||['jobselect','file','variant','countryfilter','companyfilter','leaguefilter','supplement'].includes(el.id))return;settings[el.id]=el.type==='checkbox'?el.checked:el.value;el.addEventListener('input',()=>{settings[el.id]=el.type==='checkbox'?el.checked:el.value;clearTimeout(settingsTimer);$('settingsstatus').textContent='正在保存设置…';settingsTimer=setTimeout(async()=>{try{for(const input of document.querySelectorAll('.panel input,.panel select')){if(input.id&&!['jobselect','file','variant','countryfilter','companyfilter','leaguefilter','supplement'].includes(input.id))settings[input.id]=input.type==='checkbox'?input.checked:input.value}await api('/api/settings',settings);$('settingsstatus').textContent='设置已保存到本机，刷新后保留。'}catch(error){$('settingsstatus').textContent='设置未保存：'+error.message}},250)})});
   if(!$('paths').value)$('paths').value=(s.input_paths||[]).join('\n');
   if(s.has_catalog&&!manifest&&!scanId){const saved=await api('/api/catalog');if(saved.manifest_id&&saved.catalog_version===s.catalog_version&&saved.conflict_check===s.conflict_check){showInputs(saved);$('scanprogress').textContent='已载入上次目录扫描；源数据更新后请重新检查。'}}
  }
  thresholdsByObjective=s.thresholds_by_objective||thresholdsByObjective;defaultsByObjective=s.defaults_by_objective||defaultsByObjective;standardThresholds=thresholdsByObjective[$('research_objective').value]||s.standard_thresholds;showProfileScope();renderJobs(s);$('create').disabled=submitting||!manifest||Boolean(scanId)||!$('inputs').querySelector('input:checked');
 }catch(e){if(!interfaceStopped){connectionReady=false;$('create').disabled=true;$('jobs').classList.add('stale-data');$('connectionstatus').className='notice';$('connectionstatus').textContent='连接断开或会话失效：'+e.message+'。所示任务可能已过期；最后成功更新 '+(lastSuccessAt?lastSuccessAt.toLocaleTimeString():'尚未连接')+'。服务重启后请刷新页面。';console.error(e)}}
 finally{refreshRunning=false}
}
function displayValue(k,v){
 if(v==null||v==='')return '—';
 if(typeof v==='object')return Object.entries(v).map(([a,b])=>a+'：'+(typeof b==='object'?JSON.stringify(b):b)).join('；');
 if(/roi|收益率/i.test(k)&&Number.isFinite(Number(v)))return (Number(v)*100).toFixed(2)+'%';
 if(typeof v==='string'&&/^[{[]/.test(v)){try{return displayValue(k,JSON.parse(v))}catch{}}
 if(/net|drawdown|净胜|回撤|worst|remove_|贡献/i.test(k)&&Number.isFinite(Number(v)))return Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3});
 return String(v);
}
async function previewResult(){
 const request=++previewRequest;previewController?.abort();
 const controller=new AbortController();previewController=controller;
 try{
 const jid=$('jobselect').value;if(!jid)throw Error('请选择任务');const filename=$('file').value;
 const d=await api('/api/preview?'+new URLSearchParams({job:jid,file:filename,variant:$('variant').value,offset:previewOffset}),undefined,{signal:controller.signal});
 if(request!==previewRequest)return;
 if(jid!==$('jobselect').value||filename!==$('file').value||d.variant!==$('variant').value||d.offset!==previewOffset)return;const j=d.job;$('resultcontext').textContent=`${j.league} / ${j.company} · v${j.created_version} · ${j.profile==='standard'?'标准规格':'旧版回归'} · ${states[j.status]||j.status} · ${j.id}。${j.previous_version?'历史代码结果，不能作为当前版本验收。':''}${j.research_objective==='historical'?(j.summary?.state?.startsWith('HISTORICAL_RESEARCH_COMPLETE')?'历史研究已完成；是否存在合格策略见最终决定。':'历史研究仍有未完成范围，见覆盖账。'):(j.summary?.state==='STANDARD_HANDOFF_COMPLETE'?'标准范围交接通过。':'未获得完整标准交接认证。')}${d.shared?'本表为主备共用研究记录。':'当前查看：'+($('variant').value==='main'?'主名单':'较低风险备选')+'。'}主备不可叠加。`;
 $('resultmetrics').replaceChildren();for(const [label,m] of [[j.research_objective==='historical'&&d.portfolio_basis==='minute_close_latest_v3'?'组合筛选：分钟末历史口径（含进球前拒单）':d.portfolio_basis==='minute_close_latest_v3'?'组合筛选：分钟末执行原价':'触发瞬间研究',d.portfolio],['分钟末最新报价模拟',d.minute_close?.metrics]]){const card=text('div','');card.append(text('b',label));card.append(text('p',m?`净收益 ${displayValue('net',m.net)} 单位 · ROI ${displayValue('roi',m.roi)} · ${m.n} 笔 · 按场回撤 ${displayValue('drawdown',m.drawdown_match)} 单位`:'此任务尚无该项结果','hint'));$('resultmetrics').append(card)}$('table').replaceChildren(text('p',d.note,'hint'));
 if(d.text)$('table').append(text('pre',d.text,'readable-document'));
 if(d.rows.length){const t=document.createElement('table'),tr=document.createElement('tr');
 const priority=['方向','direction','条件','净胜','net','roi','场次','n','drawdown','streak','状态','原因'];let keys=Object.keys(d.rows[0]);keys.sort((a,b)=>(priority.includes(a)?priority.indexOf(a):100)-(priority.includes(b)?priority.indexOf(b):100));
 const labels={net:'净收益（单位）',roi:'收益率',n:'笔数',drawdown:'逐笔最大回撤',streak:'最长连亏',year_net:'各年净收益',year_counts:'各年笔数',evidence:'本次触发条件证据',strategy_id:'策略编号'};
 keys.forEach(k=>tr.append(text('th',labels[k]||k)));const head=document.createElement('thead');head.append(tr);t.append(head);const body=document.createElement('tbody');
 d.rows.forEach(r=>{const row=document.createElement('tr');keys.forEach(k=>row.append(text('td',displayValue(k,r[k]))));body.append(row)});t.append(body);$('table').append(t)}
 $('csvdownload').href=download(jid,d.relative_path);$('csvdownload').textContent='下载当前完整文件';$('csvdownload').hidden=!d.available;
 $('prevpage').disabled=previewOffset===0;$('nextpage').disabled=!d.has_more;$('pageinfo').textContent=d.rows.length?`第 ${previewOffset+1}—${previewOffset+d.rows.length} 条`:'';
 }catch(e){if(request===previewRequest&&!controller.signal.aborted)toast(e.message)}
 finally{if(previewController===controller)previewController=null}
}
$('preview').onclick=()=>{previewOffset=0;previewResult()};
$('prevpage').onclick=()=>{previewOffset=Math.max(0,previewOffset-200);previewResult()};
$('nextpage').onclick=()=>{previewOffset+=200;previewResult()};
function syncFileOptions(){
 const historical=$('jobselect').selectedOptions[0]?.dataset.objective==='historical';
 for(const option of $('file').options)if(option.value.startsWith('样本外'))option.hidden=historical;
 if($('file').selectedOptions[0]?.hidden)$('file').value='最终决定.md';
}
for(const name of ['jobselect','file','variant'])$(name).onchange=()=>{syncFileOptions();previewOffset=0;if($('jobselect').value)previewResult();else{previewRequest++;previewController?.abort();previewController=null;for(const id of ['table','resultcontext','resultmetrics'])$(id).replaceChildren();$('pageinfo').textContent='';$('prevpage').disabled=true;$('nextpage').disabled=true;$('csvdownload').hidden=true}};
refresh();setInterval(refresh,2500);

document.addEventListener('click',async event=>{
 const link=event.target.closest('a');if(!link)return;const url=new URL(link.href,location.href);
 if(url.origin!==location.origin||!url.pathname.startsWith('/download/')||url.searchParams.has('ticket'))return;
 event.preventDefault();try{const parts=decodeURIComponent(url.pathname).split('/');const reply=await api('/api/download-ticket',{job:parts[2],path:parts.slice(3).join('/')});const downloadLink=document.createElement('a');downloadLink.href=reply.url;downloadLink.download=parts.at(-1);document.body.append(downloadLink);downloadLink.click();downloadLink.remove()}catch(error){toast(error.message)}
});
let standardThresholds=null,thresholdsByObjective={},defaultsByObjective={};
function showProfileScope(){
 const standard=$('profile').value==='standard',historical=$('research_objective').value==='historical';
 standardThresholds=thresholdsByObjective[$('research_objective').value]||standardThresholds||{};
 $('supplement').disabled=standard;$('supplementnote').textContent=standard?'标准规格包含全部16方向。':'此开关只调整旧版有限字典。';
 for(const key of ['min_matches','min_matches_share','min_matches_ceiling','min_segment_share','min_return_drawdown_ratio','portfolio_min_return_drawdown_ratio','conservative_portfolio_min_return_drawdown_ratio','selection_profit_tolerance_share','holdout_months','holdout_min_orders','prematch_trigger_status','min_history_years','min_profit']){
  const el=$(key);if(!el)continue;el.disabled=standard&&key in standardThresholds;if(el.disabled)el.value=standardThresholds[key];
 }
 for(const key of ['holdout_months','holdout_min_orders'])$(key).closest('label').hidden=historical;
 $('min_history_years').closest('label').hidden=!historical;$('historical_diagnostics').closest('label').hidden=!historical;
 if($('min_profit'))$('min_profit').closest('label').hidden=!historical;
 $('thresholdnote').textContent=historical?'使用全部选定历史。第一阶段只保留净胜高于本次门槛的规则（默认10单位，0表示净胜>0全收）。第二阶段按覆盖年数、各段净胜、场次、回撤与集中度筛选，连亏只作报表列。门槛均可在创建任务前调整，开跑后冻结。压力和扰动只作对照，不作为未来预测或历史完成门槛。':'旧验证模式：固定标准门槛，留出最近12个月，样本外结果决定旧模式的模拟推荐。';
 showBudgetWarning();showHistoricalPolicyScope();
}
function showBudgetWarning(){
 const box=$('budgetwarning');if(!box)return;
 // The old validation policy keeps its fixed search limits; historical research is expected to run to completion.
 const limits=$('research_objective').value!=='historical'?[]:[['standard_node_budget','每方向搜索节点','超出后该方向只算部分'],['standard_review_budget','每方向规则复核','超出后复核不完整'],['max_pool_pairs','全池风险比较','超出后比较不完整'],['select_budget','每起点组合改动','超出后组合搜索只算部分'],['selection_eval_budget','每起点组合评价','超出后组合搜索只算部分'],['max_run_minutes','单次运行时长（分钟）','到时暂停'],['max_output_mb','任务输出（MiB）','到限暂停']].filter(([id])=>$(id)&&Number($(id).value)>0);
 box.hidden=!limits.length;
 box.textContent=limits.length?'已设置计算预算：'+limits.map(([id,label,effect])=>`${label} ${$(id).value}（${effect}）`).join('；')+'。预算为0表示不预设计算上限，不代表资源无限；达到预算应保留PARTIAL，不隐藏未完成。':'';
}
$('research_objective').addEventListener('change',()=>{
 if($('research_objective').value==='historical')$('profile').value='standard';
 const settings=defaultsByObjective[$('research_objective').value]||{};
 for(const [key,value] of Object.entries(settings)){const el=$(key);if(el&&key!=='profile'){if(el.type==='checkbox')el.checked=Boolean(value);else el.value=value}}
 showProfileScope();
});
$('profile').addEventListener('change',()=>{
 $('research_objective').value=$('profile').value==='standard'?'historical':'validation';
 $('research_objective').dispatchEvent(new Event('change'));showProfileScope();
});showProfileScope();

$('shutdown').onclick=async()=>{try{
 if(!shutdownArmed){const s=await api('/api/state');const queued=s.jobs.filter(j=>j.status==='QUEUED').length;if(queued){shutdownArmed=true;$('shutdown').textContent='再次点击确认关闭';toast(`还有 ${queued} 个排队任务：关闭界面服务后它们不会开始，直到重新启动工作台；正在计算的任务不受影响。`);setTimeout(()=>{shutdownArmed=false;$('shutdown').textContent='关闭界面服务（不停止计算）'},10000);return}}
 await api('/api/shutdown',{});interfaceStopped=true;$('shutdown').disabled=true;$('create').disabled=true;$('jobs').prepend(text('p','界面服务已关闭。重新双击项目目录中的“启动工作台.bat”即可打开；正在计算的任务不受影响，排队任务要等界面重新打开后才会开始。','notice'))}catch(error){toast(error.message)}};
$('resumeall').onclick=async()=>{try{const r=await api('/api/control-all',{action:'resume'});toast(r.resumed.length?`已重新排队 ${r.resumed.length} 个暂停或中断的任务。`:'没有可续跑的暂停或中断任务；旧版本任务需先用 rebuild-stale 重建。');refresh()}catch(e){toast(e.message)}};

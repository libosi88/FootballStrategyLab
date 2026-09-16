#!/usr/bin/env node
'use strict';

// Node built-ins only. Terminal job rows below are synthetic display fixtures.
// Usage: node browser_smoke.js [--output NEW_AUDIT_FINAL_SUBDIRECTORY] [--chrome EXE]
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..', '..');
const finalRoot = path.join(__dirname, 'final');
let output = path.join(finalRoot, 'ui');
let explicitChrome = process.env.FSL_SMOKE_CHROME || '';
const argv = process.argv.slice(2);
for (let i=0; i<argv.length; i++) {
  if (!['--output','--chrome'].includes(argv[i]) || !argv[i+1]) throw Error('Unknown/missing smoke argument');
  if (argv[i]==='--output') output=path.resolve(root,argv[++i]);
  else explicitChrome=path.resolve(argv[++i]);
}
const relativeOutput=path.relative(finalRoot,output);
if (!relativeOutput || relativeOutput.startsWith('..') || path.isAbsolute(relativeOutput)) throw Error('Evidence must be beneath audit/final');
fs.mkdirSync(finalRoot,{recursive:true});
fs.mkdirSync(output); // Exclusive: never replace earlier evidence.
const python=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const report={schema:'FSL_ISOLATED_CHROME_UI_SMOKE_V1',status:'NOT_VERIFIED',started_at:new Date().toISOString(),
  source_root:root,evidence_directory:output,script:path.relative(root,__filename).split(path.sep).join('/'),
  script_sha256:hash(fs.readFileSync(__filename)),dependencies_installed:false,production_files_edited:false,
  scope:'Real Chrome DOM and production HTTP/JS/CSS; synthetic terminal job fixtures, no league research or orders.',
  capabilities:{node:process.version,node_executable:process.execPath,fetch:typeof fetch,websocket:typeof WebSocket},
  checks:[],screenshots:[],processes:{},cleanup:{}};
let phase='capabilities',token='',temporary,workspace,profile,ui,chrome,uiUrl,page,browser,interrupted=false,testing=false;
const deadline=Date.now()+240000,chromePids=new Set();
const exceptions=[],consoleErrors=[],consoleMessages=[],browserLog=[],responses=[],networkFailures=[];
const expectedRejections=new Set(['invalid_settings_dom','invalid_parallel_fraction','invalid_parallel_boolean']);
for (const signal of ['SIGINT','SIGTERM']) process.once(signal,()=>{interrupted=true;});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const detail=error=>({name:error.name,message:error.message,stack:error.stack});
function assert(value,message){if(!value)throw Error(message);}
function jsonFile(name,value){fs.writeFileSync(path.join(output,name),JSON.stringify(value,null,2)+'\n',{flag:'wx'});}
async function until(action,message,timeout=15000){
  const end=Date.now()+timeout;
  while(Date.now()<end){
    if(interrupted||Date.now()>deadline)throw Error('Smoke interrupted or exceeded its bounded deadline');
    const value=await action();if(value)return value;await sleep(100);
  }
  throw Error('Timed out: '+message);
}
function pythonJSON(source,args=[]){
  const result=spawnSync(python,['-B','-c',source,...args],{cwd:root,encoding:'utf8',windowsHide:true,shell:false,
    timeout:45000,maxBuffer:16*1024*1024,env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}});
  if(result.error)throw result.error;
  if(result.status!==0)throw Error('Python helper: '+(result.stderr||result.stdout));
  return JSON.parse(result.stdout);
}
function fingerprint(){return pythonJSON([
  'import json,sys',
  'from lab.common import ROOT,VERSION,sha,source_fingerprint',
  'from lab.release import release_files,release_fingerprint,workspace_identity',
  'print(json.dumps({"version":VERSION,"engine_hash":source_fingerprint(),"release_hash":release_fingerprint(),"workspace_id":workspace_identity(sys.argv[1]),"files":{p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}},ensure_ascii=False))',
].join('\n'),[workspace]);}
function launch(executable,args,label){
  const stdout=fs.openSync(path.join(output,label+'.stdout.log'),'wx'),stderr=fs.openSync(path.join(output,label+'.stderr.log'),'wx');
  let child;
  try{child=spawn(executable,args,{cwd:root,shell:false,windowsHide:true,stdio:['ignore',stdout,stderr],
    env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}});}
  finally{fs.closeSync(stdout);fs.closeSync(stderr);}
  child.spawnFailure=null;child.once('error',error=>{child.spawnFailure=error;});
  report.processes[label]={pid:child.pid||null,executable,args};
  child.once('exit',(code,signal)=>Object.assign(report.processes[label],{exit_code:code,exit_signal:signal,exited_at:new Date().toISOString()}));
  return child;
}
const running=child=>Boolean(child&&child.pid&&!child.spawnFailure&&child.exitCode===null&&child.signalCode===null);
function ensureRunning(child,name){if(child.spawnFailure)throw child.spawnFailure;if(!running(child))throw Error(name+' exited; see stderr log');}
function pidAlive(pid){if(!Number.isInteger(pid)||pid<=0)return false;try{process.kill(pid,0);return true;}catch(error){return error.code!=='ESRCH';}}
async function waitExit(child,timeout){const end=Date.now()+timeout;while(running(child)&&Date.now()<end)await sleep(100);return !running(child);}
async function stopOwned(child,label){
  if(!child)return {started:false,exited:true};
  if(running(child)&&!await waitExit(child,4000)){
    // Only the exact, still-live ChildProcess created above; never a process name.
    if(process.platform==='win32'){
      const stopped=spawnSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{encoding:'utf8',windowsHide:true,shell:false,timeout:10000});
      report.cleanup[label+'_forced_stop']={pid:child.pid,status:stopped.status,stdout:stopped.stdout,stderr:stopped.stderr,error:stopped.error?.message};
    }else child.kill('SIGTERM');
    await waitExit(child,5000);
  }
  return {started:true,pid:child.pid,exited:!running(child)&&!pidAlive(child.pid),exit_code:child.exitCode,signal:child.signalCode,spawn_error:child.spawnFailure?.message};
}
function record(value){return {phase,observed_at:new Date().toISOString(),...value};}
function protocolEvent(method,p){
  if(method==='Runtime.exceptionThrown')exceptions.push(record(p));
  if(method==='Runtime.consoleAPICalled'){
    const item=record({type:p.type,args:p.args.map(a=>a.value??a.description),stackTrace:p.stackTrace});
    consoleMessages.push(item);if(['error','assert'].includes(p.type))consoleErrors.push(item);
  }
  if(method==='Log.entryAdded'){
    const e=p.entry,expected=expectedRejections.has(phase)&&e.source==='network'&&String(e.url||'').startsWith(uiUrl+'/api/settings')&&/400/.test(e.text);
    browserLog.push(record({...e,expected_settings_rejection:expected}));
  }
  if(method==='Network.responseReceived')responses.push(record({request_id:p.requestId,url:p.response.url,status:p.response.status,mime_type:p.response.mimeType,
    expected_settings_rejection:expectedRejections.has(phase)&&p.response.url===uiUrl+'/api/settings'&&p.response.status===400}));
  if(method==='Network.loadingFailed')networkFailures.push(record(p));
}
class CDP{
  constructor(url,onEvent=()=>{}){
    assert(new URL(url).hostname==='127.0.0.1','CDP must be loopback');
    this.pending=new Map();this.next=0;this.closed=false;this.socket=new WebSocket(url);
    this.ready=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('CDP connection timed out')),10000);
      this.socket.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});
      this.socket.addEventListener('error',()=>{clearTimeout(timer);reject(Error('CDP connection failed'));},{once:true});
    });
    this.socket.addEventListener('message',event=>{
      const message=JSON.parse(String(event.data));
      if(message.id!=null){
        const job=this.pending.get(message.id);if(!job)return;
        clearTimeout(job.timer);this.pending.delete(message.id);
        if(message.error)job.reject(Error(job.method+': '+message.error.message));else job.resolve(message.result);
      }else onEvent(message.method,message.params||{});
    });
    this.socket.addEventListener('close',()=>{
      this.closed=true;for(const job of this.pending.values()){clearTimeout(job.timer);job.reject(Error('CDP closed: '+job.method));}this.pending.clear();
    });
  }
  async send(method,params={},timeout=15000){
    await this.ready;if(this.closed)throw Error('CDP is closed');const id=++this.next;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP timed out: '+method));},timeout);
      this.pending.set(id,{resolve,reject,timer,method});this.socket.send(JSON.stringify({id,method,params}));
    });
  }
  close(){this.socket.close();}
}
async function evaluate(expression){
  const result=await page.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});
  if(result.exceptionDetails)throw Error('Browser evaluation: '+(result.exceptionDetails.exception?.description||result.exceptionDetails.text));
  return result.result?.value;
}
const state=()=>evaluate('api("/api/state")');
async function readyPage(){await until(async()=>{
  try{return await evaluate('document.readyState==="complete" && typeof connectionReady!=="undefined" && connectionReady && typeof renderJobs==="function"');}
  catch(error){if(/context|navigat/i.test(error.message))return false;throw error;}
},'production page ready',25000);}
async function screenshot(name,selector){
  let clip;
  if(selector)clip=await evaluate('(()=>{const r=document.querySelector('+JSON.stringify(selector)+').getBoundingClientRect();return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1};})()');
  else{const m=await page.send('Page.getLayoutMetrics'),s=m.cssContentSize||m.contentSize;clip={x:0,y:0,width:Math.ceil(s.width),height:Math.min(12000,Math.ceil(s.height)),scale:1};}
  assert(clip.width>0&&clip.height>0,'Empty screenshot target');
  const shot=await page.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip},20000),bytes=Buffer.from(shot.data,'base64');
  assert(bytes.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex')),'Invalid PNG');
  fs.writeFileSync(path.join(output,name),bytes,{flag:'wx'});report.screenshots.push({file:name,sha256:hash(bytes),bytes:bytes.length,clip,phase});
}
async function check(name,action,required=false){
  phase=name;const item={name,status:'RUNNING',started_at:new Date().toISOString()};report.checks.push(item);console.log('CHECK '+name);
  try{item.details=await action();item.status='PASS';}
  catch(error){item.status='FAIL';item.error=detail(error);if(required)throw error;}
  finally{item.finished_at=new Date().toISOString();}
}
async function inputSettings(values){return evaluate('(()=>{document.getElementById("settingsstatus").textContent="";for(const [id,value] of Object.entries('+JSON.stringify(values)+')){const el=document.getElementById(id);el.focus();el.value=String(value);el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));}return true;})()');}
async function savedSettings(predicate){
  let last;
  try{return await until(async()=>{const s=await state(),message=await evaluate('document.getElementById("settingsstatus").textContent');last={min_profit:s.research_settings.min_profit,parallel:s.scheduler?.max_parallel_jobs,message};return predicate(s)&&message.includes('设置已保存')?s:false;},'settings saved by production handler');}
  catch(error){error.message+='; observed='+JSON.stringify(last);throw error;}
}
async function switchObjective(value){
  await evaluate('(()=>{document.getElementById("settingsstatus").textContent="";const el=document.getElementById("research_objective");el.focus();el.value='+JSON.stringify(value)+';el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));return true;})()');
  return savedSettings(s=>s.research_settings.research_objective===value);
}
function settingsView(){return evaluate('(()=>{const get=id=>document.getElementById(id);return {objective:get("research_objective").value,profile:get("profile").value,min_profit:get("min_profit").value,min_profit_disabled:get("min_profit").disabled,min_profit_hidden:get("min_profit").closest("label").hidden,holdout_months:get("holdout_months").value,holdout_hidden:get("holdout_months").closest("label").hidden,parallel:get("max_parallel_jobs").value,message:get("settingsstatus").textContent};})()');}

async function main(){
  try{
    assert(typeof fetch==='function'&&typeof WebSocket==='function','Node built-in fetch/WebSocket unavailable');
    assert(fs.existsSync(python),'Project Python executable unavailable');
    const candidates=[explicitChrome,
      process.env.PROGRAMFILES&&path.join(process.env.PROGRAMFILES,'Google/Chrome/Application/chrome.exe'),
      process.env['PROGRAMFILES(X86)']&&path.join(process.env['PROGRAMFILES(X86)'],'Google/Chrome/Application/chrome.exe'),
      process.env.LOCALAPPDATA&&path.join(process.env.LOCALAPPDATA,'Google/Chrome/Application/chrome.exe')].filter(Boolean);
    report.capabilities.chrome_candidates=candidates.map(file=>({file,exists:fs.existsSync(file)}));
    const executable=candidates.find(file=>fs.existsSync(file)&&fs.statSync(file).isFile());
    assert(executable,'Installed Chrome not found; no installation attempted');
    report.capabilities.chrome_executable=executable;
    temporary=fs.mkdtempSync(path.join(os.tmpdir(),'fsl_browser_smoke_'));
    workspace=path.join(temporary,'workspace');profile=path.join(temporary,'chrome-profile');
    fs.mkdirSync(workspace);fs.mkdirSync(profile);
    report.isolation={temporary_root:temporary,workspace,chrome_profile:profile,real_workspace_used:false,existing_browser_used:false};
    report.binding_before=fingerprint();
    assert(!(report.script in report.binding_before.files),'Smoke script unexpectedly belongs to release whitelist');
    jsonFile('source_manifest_before.json',report.binding_before);
    const fixtures=pythonJSON([
      'import json,sys',
      'from pathlib import Path',
      'from lab.common import DEFAULT,VALIDATION_DEFAULT,atomic_json',
      'from lab.store import Store',
      'root=Path(sys.argv[1]); store=Store(root); assert not store.jobs()',
      'cases=[("historical_empty","historical","HISTORICAL_RESEARCH_COMPLETE",True),("historical_no_handoff","historical","HISTORICAL_RESEARCH_COMPLETE_NO_HANDOFF",True),("historical_failed_gate","historical","PARTIAL_RESULT",False),("validation_failed_gate","validation","PARTIAL_RESULT",False),("explicit_error","historical",None,False)]',
      'out=[]',
      'for name,objective,status,complete in cases:',
      ' cfg=dict(DEFAULT if objective=="historical" else VALIDATION_DEFAULT)',
      ' if name=="historical_no_handoff": cfg["package_handoff"]=False',
      ' jid=store.create("UI_SMOKE_SYNTHETIC: "+name,cfg,{"files":[]},start_paused=True)',
      ' summary=None',
      ' if status is not None:',
      '  gates={"full_standard_search":True,"engine_tests":True,"all_pool_review":True,"selection":True,"trigger":complete or objective=="validation","nonempty_roster":False,"rule_cases":True,"persistent_paper":True,"streaming_paper":True,"execution_economics":True,"standard_research_thresholds":True}',
      '  if cfg["package_handoff"]: gates["packaging"]=True',
      '  if objective=="validation": gates["holdout_evaluation"]=True',
      '  summary={"fixture_only":True,"state":status,"research_objective":objective,"selected":0,"backup_selected":0,"v3_status":"SYNTHETIC_UI_FIXTURE","coverage":{"candidates":0,"planned_expressions":16,"evaluated":16,"equivalent_occurrences":0,"proven_zero":0,"proven_nonprofitable":0,"remaining":0,"local_search_complete":True,"standard_search_complete":True},"gates":gates,"packages":{},"empty_roster_chain_verified":complete,"coverage_ratio":1,"packaging_skipped":not cfg["package_handoff"],"recommendation":{"status":"HISTORICAL_SEARCH_EMPTY" if complete else "PARTIAL_HISTORICAL_RESULT" if objective=="historical" else "OBSERVATION_ONLY"}}',
      '  atomic_json(root/jid/"summary.json",summary)',
      ' store.update(jid,status="DONE" if complete else "ERROR" if status is None else "PARTIAL_RESULT",error="UI_SMOKE_CONTROL_ERROR: intentionally injected display fixture" if status is None else "")',
      ' out.append({"case":name,"id":jid,"objective":objective,"summary":summary,"synthetic":True})',
      'assert all(j["status"] not in ("QUEUED","RUNNING","PAUSING") for j in store.jobs())',
      'print(json.dumps(out,ensure_ascii=False))',
    ].join('\n'),[workspace]);
    jsonFile('synthetic_job_fixtures.json',fixtures);
    report.fixtures=fixtures.map(({case:name,id,objective})=>({case:name,id,objective}));
    ui=launch(python,['-B','app.py','serve','--workspace',workspace,'--port','0','--no-browser'],'ui');
    const endpoint=await until(()=>{
      ensureRunning(ui,'Isolated UI');const file=path.join(workspace,'ui_endpoint.json');
      return fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):false;
    },'temporary UI endpoint',30000);
    assert(pidOwnedBy(endpoint.pid,ui)&&Number.isInteger(endpoint.port)&&endpoint.port>0,'UI endpoint ownership mismatch');
    report.processes.ui.server_pid=endpoint.pid;
    uiUrl='http://127.0.0.1:'+endpoint.port;report.ui_endpoint={url:uiUrl,...endpoint};
    const identity=await (await fetch(uiUrl+'/api/identity',{signal:AbortSignal.timeout(5000)})).json();
    report.server_identity=identity;
    for(const key of ['version','engine_hash','release_hash','workspace_id'])assert(identity[key]===report.binding_before[key],'Server/source identity differs: '+key);
    chrome=launch(executable,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
      '--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',
      '--metrics-recording-only','--mute-audio','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
      '--user-data-dir='+profile,'--window-size=1440,1100','about:blank'],'chrome');
    const devtools=await until(()=>{
      ensureRunning(chrome,'Isolated Chrome');const file=path.join(profile,'DevToolsActivePort');
      if(!fs.existsSync(file))return false;
      const [port,browserPath]=fs.readFileSync(file,'utf8').trim().split(/\r?\n/);
      return /^\d+$/.test(port)&&browserPath?.startsWith('/devtools/browser/')?{port:Number(port),browserPath}:false;
    },'owned Chrome debugging endpoint',45000);
    report.chrome_endpoint={port:devtools.port,bound_address:'127.0.0.1'};
    browser=new CDP('ws://127.0.0.1:'+devtools.port+devtools.browserPath);await browser.ready;
    report.capabilities.browser_version=await browser.send('Browser.getVersion');
    const info=await browser.send('SystemInfo.getProcessInfo');
    for(const item of info.processInfo)chromePids.add(Number(item.id));chromePids.add(chrome.pid);
    report.owned_chrome_processes=info.processInfo;
    const targets=await (await fetch('http://127.0.0.1:'+devtools.port+'/json/list',{signal:AbortSignal.timeout(5000)})).json();
    const target=targets.find(item=>item.type==='page'&&item.url==='about:blank');
    assert(target?.webSocketDebuggerUrl,'No isolated blank page CDP target');
    assert(new URL(target.webSocketDebuggerUrl).port===String(devtools.port),'Page CDP port differs from owned Chrome');
    page=new CDP(target.webSocketDebuggerUrl,protocolEvent);await page.ready;
    for(const method of ['Page.enable','Runtime.enable','Log.enable','Network.enable'])await page.send(method);
    await page.send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    testing=true;
    await check('production_page_and_defaults',async()=>{
      await page.send('Page.navigate',{url:uiUrl});await readyPage();token=await evaluate('window.LOCAL_TOKEN');
      const actual=await settingsView(),value=await state();
      const dom=await evaluate('({title:document.title,version:document.getElementById("app-version").textContent,connection:document.getElementById("connectionstatus").textContent,scripts:[...document.scripts].map(s=>s.src),stylesheets:[...document.styleSheets].map(s=>s.href)})');
      assert(dom.version===report.binding_before.version,'Displayed version does not match source');
      assert(actual.objective==='historical'&&actual.profile==='standard'&&actual.min_profit==='10','Default objective/profit threshold is not historical/10');
      assert(!actual.min_profit_disabled&&actual.holdout_months==='0'&&actual.holdout_hidden,'Historical default controls are wrong');
      assert(value.engine_hash===identity.engine_hash&&value.release_hash===identity.release_hash,'State endpoint hash mismatch');
      assert(value.workspace===workspace&&value.server_pid===endpoint.pid,'Page is not connected to isolated UI');
      assert(value.jobs.length===fixtures.length&&value.jobs.every(j=>!['QUEUED','RUNNING','PAUSING'].includes(j.status)),'Unexpected active/foreign jobs');
      const assets={};
      for(const name of ['jobs.js','app.js','style.css']){
        const response=await fetch(uiUrl+'/'+name,{signal:AbortSignal.timeout(5000)});assert(response.ok,'Asset failed: '+name);
        assets[name]=hash(Buffer.from(await response.arrayBuffer()));assert(assets[name]===report.binding_before.files['web/'+name],'Served asset differs: '+name);
      }
      await screenshot('01_initial_page.png');return {dom,settings:actual,assets,job_count:value.jobs.length};
    },true);
    await check('actual_dom_empty_rosters_and_failure_controls',async()=>{
      const actual=await evaluate('(()=>{for(const d of document.querySelectorAll("#jobs .job details")){const s=d.querySelector("summary");if(s&&!d.open)s.click();}return [...document.querySelectorAll("#jobs .job")].map(card=>({id:card.dataset.jobId,badge:card.querySelector(".badge")?.textContent,text:card.textContent,errors:[...card.querySelectorAll(".error")].map(el=>({text:el.textContent,color:getComputedStyle(el).color,visible:el.getClientRects().length>0}))}));})()');
      for(const fixture of fixtures){
        const card=actual.find(item=>item.id===fixture.id);assert(card,'Missing card: '+fixture.case);
        if(['historical_empty','historical_no_handoff'].includes(fixture.case)){
          assert(card.errors.length===0,'Successful empty history card contains red failures');
          assert(card.text.includes('没有满足本次条件的策略')&&!card.text.includes('不能认证非空交接链路'),'Empty completion wording is wrong');
        }else{
          const text=card.errors.map(item=>item.text).join(' ');
          const expected=fixture.case==='historical_failed_gate'?'触发核对':fixture.case==='validation_failed_gate'?'非空主名单验收':'UI_SMOKE_CONTROL_ERROR';
          assert(text.includes(expected),'Failed gate/error was hidden: '+fixture.case);
          assert(card.errors.some(item=>{const rgb=item.color.match(/\d+/g)?.map(Number);return item.visible&&rgb&&rgb[0]>100&&rgb[0]>rgb[1]*1.5&&rgb[0]>rgb[2]*1.2;}),'Failure control is not visibly red');
          if(fixture.case==='historical_failed_gate')assert(!text.includes('非空主名单'),'Historical nonempty gate is still mandatory');
        }
      }
      await screenshot('02_job_cards.png','#jobs');jsonFile('rendered_job_cards.json',actual);return actual;
    });
    await check('valid_settings_dom',async()=>{
      await inputSettings({min_profit:12,max_parallel_jobs:2});
      const s=await savedSettings(s=>String(s.research_settings.min_profit)==='12'&&s.scheduler?.max_parallel_jobs===2);
      return {settings:s.research_settings,parallel:s.scheduler.max_parallel_jobs};
    },true);
    await check('invalid_settings_dom',async()=>{
      await inputSettings({min_profit:-1,max_parallel_jobs:3});
      await until(()=>evaluate('document.getElementById("settingsstatus").textContent.includes("设置未保存")'),'invalid settings rejected');
      const s=await state();
      assert(String(s.research_settings.min_profit)==='12'&&s.scheduler?.max_parallel_jobs===2,'Invalid settings changed persisted state');
      return {settings:s.research_settings,parallel:s.scheduler.max_parallel_jobs};
    });
    await inputSettings({min_profit:12,max_parallel_jobs:2});
    await savedSettings(s=>String(s.research_settings.min_profit)==='12'&&s.scheduler?.max_parallel_jobs===2);
    for(const [name,value] of [['invalid_parallel_fraction',2.5],['invalid_parallel_boolean',true]]){
      await check(name,async()=>{
        const rejected=await evaluate('api("/api/settings",{max_parallel_jobs:'+JSON.stringify(value)+'}).then(()=>({rejected:false}),e=>({rejected:true,error:e.message}))');
        assert(rejected.rejected,'Invalid parallel type was accepted');
        const s=await state();assert(s.scheduler?.max_parallel_jobs===2,'Rejected parallel value changed the scheduler');
        return rejected;
      });
    }
    await check('validation_mode_dom',async()=>{
      const s=await switchObjective('validation'),view=await settingsView();
      assert(view.objective==='validation'&&!view.holdout_hidden&&view.holdout_months==='12','Validation controls/holdout did not update');
      assert(s.research_settings.research_objective==='validation','Validation setting was not persisted');
      return view;
    });
    await check('historical_mode_dom',async()=>{
      const s=await switchObjective('historical'),view=await settingsView();
      assert(view.objective==='historical'&&view.holdout_hidden&&view.holdout_months==='0','Historical controls/holdout did not update');
      assert(s.research_settings.holdout_months===0,'Historical mode retained a holdout');
      await screenshot('03_verified_settings.png');return view;
    });
    await check('source_and_workspace_unchanged',async()=>{
      report.binding_after=fingerprint();
      assert(report.binding_after.engine_hash===report.binding_before.engine_hash&&report.binding_after.release_hash===report.binding_before.release_hash,'Source changed during smoke test');
      assert(JSON.stringify(report.binding_after.files)===JSON.stringify(report.binding_before.files),'Source file manifest changed');
      const s=await state();assert(s.jobs.every(j=>!['RUNNING','QUEUED','PAUSING'].includes(j.status)),'UI test started a research worker');
      const info=await browser.send('SystemInfo.getProcessInfo');for(const p of info.processInfo)chromePids.add(Number(p.id));
      return {source_unchanged:true,real_orders_sent:0,synthetic_jobs:s.jobs.length};
    });
    await check('no_application_javascript_errors',async()=>{
      const unexpected=browserLog.filter(e=>e.level==='error'&&!e.expected_settings_rejection&&!String(e.url||'').endsWith('/favicon.ico'));
      assert(exceptions.length===0&&consoleErrors.length===0&&unexpected.length===0,'Unexpected application/browser errors; see captured logs');
      return {exceptions:exceptions.length,console_errors:consoleErrors.length,unexpected_browser_errors:unexpected.length,
              expected_http_400:responses.filter(e=>e.expected_settings_rejection).length};
    });
    report.status=report.checks.every(c=>c.status==='PASS')?'PASS':'FAIL';
  }catch(error){report.status=testing?'FAIL':'NOT_VERIFIED';report.error=detail(error);}
  finally{
    phase='cleanup';
    if(page&&!page.closed){try{await evaluate('api("/api/shutdown",{}).catch(e=>({error:e.message}))');}catch(error){report.cleanup.ui_shutdown_error=error.message;}}
    if(browser&&!browser.closed){try{await browser.send('Browser.close',{},3000);}catch(error){report.cleanup.chrome_close_message=error.message;}}
    report.cleanup.ui=await stopOwned(ui,'ui');report.cleanup.chrome=await stopOwned(chrome,'chrome');
    try{page?.close();}catch(error){}try{browser?.close();}catch(error){}
    report.cleanup.remaining_owned_chrome_pids=[...chromePids].filter(pidAlive);
    const stopped=report.cleanup.ui.exited&&report.cleanup.chrome.exited&&report.cleanup.remaining_owned_chrome_pids.length===0;
    if(temporary&&stopped){
      try{fs.rmSync(temporary,{recursive:true,force:true,maxRetries:5,retryDelay:200});report.cleanup.temporary_removed=!fs.existsSync(temporary);}
      catch(error){report.cleanup.remove_error=error.message;report.cleanup.temporary_removed=false;}
    }
    if(!stopped||temporary&&!report.cleanup.temporary_removed)report.status='FAIL';
    report.engine_hash=report.binding_before?.engine_hash;report.release_hash=report.binding_before?.release_hash;
    report.finished_at=new Date().toISOString();report.real_orders_sent=0;
    jsonFile('browser_events.json',{exceptions,consoleErrors,consoleMessages,browserLog,responses,networkFailures});
    jsonFile('ui_validation.json',report);
    console.log(JSON.stringify({status:report.status,checks:report.checks.map(c=>({name:c.name,status:c.status})),cleanup:report.cleanup,output},null,2));
    process.exitCode=report.status==='PASS'?0:1;
  }
}
function pidOwnedBy(pid,child){
  if(!child||!Number.isSafeInteger(pid)||pid<1)return false;
  if(pid===child.pid)return true;
  if(process.platform!=='win32')return false;
  let current=pid;const seen=new Set();
  for(let depth=0;depth<8&&!seen.has(current);depth++){
    seen.add(current);
    const command='$p=Get-CimInstance Win32_Process -Filter "ProcessId = '+current+'"; if($p){[Console]::Write([string]$p.ParentProcessId)}';
    const r=spawnSync('powershell.exe',['-NoProfile','-Command',command],{encoding:'utf8',windowsHide:true,timeout:5000});
    if(r.status!==0||!/^\d+$/.test(r.stdout.trim()))return false;
    current=Number(r.stdout.trim());if(current===child.pid)return true;if(current<1)return false;
  }
  return false;
}
main().catch(error=>{console.error(error);process.exitCode=1;});

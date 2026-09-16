"""Backend workflow test plus separately disclosed offline browser rendering."""
from pathlib import Path
import urllib.request,urllib.error,json,re,time
root=Path(__file__).resolve().parent
base='http://127.0.0.1:8765'
html=urllib.request.urlopen(base).read().decode()
token=re.search(r'window.LOCAL_TOKEN="([^"]+)"',html)[1]
def req(path,data=None,auth=True,origin=None):
 headers={'Content-Type':'application/json'}
 if auth:headers['X-Local-Token']=token
 if origin:headers['Origin']=origin
 r=urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
 with urllib.request.urlopen(r,timeout=60) as f:return json.load(f)
man=req('/api/demo',{})
jid=req('/api/create',{'manifest_id':man['manifest_id'],'leagues':['合成演示联赛（非真实比赛）'],'config':{'profile':'smoke'}})['jobs'][0]
start=time.monotonic()
while time.monotonic()-start<180:
 state=req('/api/state');job=next(j for j in state['jobs'] if j['id']==jid)
 if job['status']=='ERROR':raise AssertionError(job['error'])
 if job['status']=='DONE':break
 time.sleep(1)
else:raise AssertionError('task did not complete')
preview=req('/api/preview?job='+jid)
for auth,origin in ((False,None),(True,'http://evil.example')):
 try:req('/api/demo',{},auth,origin);raise AssertionError('unsafe request accepted')
 except urllib.error.HTTPError as e:assert e.code==403
fixture={'state':state,'preview':preview,'manifest':man,'job':jid}
(root/'ui_actual_responses.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2),encoding='utf-8')
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1440,'height':1120},device_scale_factor=1)
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 # The environment's managed browser blocks every navigation URL. Do not change that policy.
 # Render our own HTML/CSS and inject ACTUAL separately captured backend responses, with no navigation.
 html=html.replace('<link rel="stylesheet" href="/style.css">','').replace('<script src="/app.js"></script>','')
 page.set_content(html,wait_until='domcontentloaded')
 page.add_style_tag(content=(root.parent/'web/style.css').read_text())
 page.evaluate('(f)=>{window.__fixture=f;window.fetch=async(path,opts)=>({ok:true,json:async()=>path.startsWith("/api/state")?f.state:path.startsWith("/api/preview")?f.preview:path==="/api/demo"?f.manifest:{ok:true}})}',fixture)
 page.add_script_tag(content=(root.parent/'web/app.js').read_text())
 page.wait_for_selector('.job');page.click('#demo');page.wait_for_selector('.leaguecheck')
 page.select_option('#jobselect',jid);page.click('#preview');page.wait_for_selector('#table table')
 page.screenshot(path=str(root/'软件界面_实测.png'),full_page=True)
 assert not errors,errors
 report={'status':'PASS_WITH_BROWSER_NAVIGATION_LIMIT','backend':['真实HTTP读取界面入口','演示数据检查','HTTP创建任务','真实调度完成挖掘到打包','真实结果预览','无令牌403','跨源403'], 'frontend':['本地HTML/CSS/JavaScript渲染','注入实际HTTP响应后的任务展示、演示选择、结果预览','无脚本错误'], 'browser_direct_navigation':'BLOCKED_BY_ENVIRONMENT_ADMIN_POLICY; not tested end-to-end in browser; no policy changes', 'source_of_displayed_data':'actual completed synthetic job, not fictional trading results','job':jid,'windows_native':'NOT_TESTED'}
 (root/'ui_test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
 browser.close()

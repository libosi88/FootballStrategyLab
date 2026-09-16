"""Development-only browser smoke test. Requires Playwright and local Chromium."""
from pathlib import Path
import json,time
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1440,'height':1150},device_scale_factor=1)
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:8765',wait_until='networkidle')
 page.click('#demo');page.wait_for_selector('.leaguecheck');page.select_option('#profile','smoke')
 token=page.evaluate('window.LOCAL_TOKEN')
 page.click('#create');page.wait_for_selector('.job',timeout=20000)
 # Current app actually runs the synthetic task through its own scheduler.
 start=time.monotonic();jid=None
 while time.monotonic()-start<180:
  s=page.request.get('http://127.0.0.1:8765/api/state',headers={'X-Local-Token':token}).json()
  if s['jobs']:
   j=s['jobs'][0];jid=j['id']
   if j['status']=='ERROR':raise AssertionError(j['error'])
   if j['status']=='DONE':break
  time.sleep(1)
 else:raise AssertionError('UI task did not finish in test budget')
 page.wait_for_function("[...document.querySelectorAll('#jobselect option')].some(o=>o.value===arguments[0])",arg=jid,timeout=10000)
 page.select_option('#jobselect',jid);page.click('#preview');page.wait_for_selector('#table table')
 page.screenshot(path=str(root/'软件界面_实测.png'),full_page=True)
 res=page.request.post('http://127.0.0.1:8765/api/demo',data='{}',headers={'Content-Type':'application/json'})
 assert res.status==403
 res=page.request.post('http://127.0.0.1:8765/api/demo',data='{}',headers={'Content-Type':'application/json','X-Local-Token':token,'Origin':'http://evil.example'})
 assert res.status==403
 assert not errors,errors
 report={'status':'PASS','browser':'Chromium headless','checks':['页面真实加载','本机演示输入检查','界面创建任务','调度器实际完成全流程','真实完成任务结果预览','CSRF无令牌拒绝','跨源请求拒绝','无前端脚本异常'],'job':jid,'screenshot':'软件界面_实测.png','windows_native':'NOT_TESTED'}
 (root/'ui_test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(report)
 browser.close()

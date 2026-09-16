"""Actual local HTTP workflow; browser navigation reported separately if blocked."""
from pathlib import Path
import sys,subprocess,os,socket,time,json,urllib.request,urllib.error,re,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,atomic_json,VERSION


def main():
 p=argparse.ArgumentParser();p.add_argument('--workspace',required=True);a=p.parse_args()
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 base=f'http://127.0.0.1:{port}';log=(ROOT/'validation/v02_ui_server.log').open('w')
 server=subprocess.Popen([sys.executable,'app.py','serve','--workspace',a.workspace,'--port',str(port),'--no-browser'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                         env={**os.environ,'OPENBLAS_NUM_THREADS':'1','NUMBA_NUM_THREADS':'2'})
 try:
  for _ in range(100):
   try:html=urllib.request.urlopen(base,timeout=3).read().decode();break
   except (urllib.error.URLError,TimeoutError):time.sleep(.1)
  else:raise RuntimeError('本机服务未启动')
  token=re.search(r'window.LOCAL_TOKEN="([^"]+)"',html)[1]
  def req(path,data=None,auth=True,origin=None):
   headers={'Content-Type':'application/json'}
   if auth:headers['X-Local-Token']=token
   if origin:headers['Origin']=origin
   r=urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
   with urllib.request.urlopen(r,timeout=60) as f:return json.load(f)
  state=req('/api/state');assert state['version']==VERSION
  assert 'id="portfolio_max_drawdown"' in html and 'id="conservative_portfolio_max_drawdown"' in html
  man=req('/api/demo',{});jid=req('/api/create',{'manifest_id':man['manifest_id'],
   'leagues':['合成演示联赛（非真实比赛）'],'config':{'profile':'smoke','directions':['LIVE_OVER'],'select_budget':4}})['jobs'][0]
  req('/api/control',{'job':jid,'action':'pause'})
  for _ in range(100):
   state=req('/api/state');j=next(j for j in state['jobs'] if j['id']==jid)
   if j['status']=='PAUSED':break
   time.sleep(.1)
  assert j['status']=='PAUSED',j
  req('/api/control',{'job':jid,'action':'resume'})
  for _ in range(300):
   state=req('/api/state');j=next(j for j in state['jobs'] if j['id']==jid)
   if j['status']=='ERROR':raise AssertionError(j['error'])
   if j['status']=='DONE':break
   time.sleep(.3)
  assert j['status']=='DONE',j
  preview=req('/api/preview?job='+jid+'&file='+urllib.parse.quote('方向汇总.csv'))
  for auth,origin in [(False,None),(True,'http://untrusted.example')]:
   try:req('/api/demo',{},auth,origin);raise AssertionError('应拒绝不可信请求')
   except urllib.error.HTTPError as ex:assert ex.code==403
  fixture={'state':state,'preview':preview,'manifest':man,'job':jid}
  atomic_json(ROOT/'validation/v02_ui_actual_responses.json',fixture)
  report={'version':VERSION,'status':'PASS_HTTP','backend':['HTTP本地入口','实际创建任务','暂停排队','恢复调度','运行到A/B打包','读取真实演示结果','无令牌403','跨源403'],
          'source':'完成的合成演示任务，非足球盈利证据','windows':'NOT_TESTED','job':jid}
  try:
   from playwright.sync_api import sync_playwright
   with sync_playwright() as play:
    browser=play.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1);errors=[]
    page.on('pageerror',lambda err:errors.append(str(err)))
    try:
     page.goto(base,wait_until='domcontentloaded',timeout=10000)
     page.wait_for_selector('.job',timeout=10000)
     report['browser_direct_navigation']='PASS'
    except Exception as ex:
     # Do not change browser policies. Only render the owned UI with captured real responses.
     report['browser_direct_navigation']='BLOCKED_OR_UNAVAILABLE: '+str(ex)[:300]
     page.close();page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
     errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
     owned=html.replace('<link rel="stylesheet" href="/style.css">','').replace('<script src="/app.js"></script>','')
     page.set_content(owned,wait_until='domcontentloaded');page.add_style_tag(content=(ROOT/'web/style.css').read_text())
     page.evaluate('(f)=>{window.fetch=async(p,o)=>({ok:true,json:async()=>p.startsWith("/api/state")?f.state:p.startsWith("/api/preview")?f.preview:p==="/api/demo"?f.manifest:{ok:true}})}',fixture)
     page.add_script_tag(content=(ROOT/'web/app.js').read_text());page.wait_for_selector('.job')
    page.select_option('#jobselect',jid);page.select_option('#file','方向汇总.csv');page.click('#preview');page.wait_for_selector('#table table')
    page.click('details summary')
    assert page.locator('#portfolio_max_drawdown').input_value()=='10'
    assert page.locator('#conservative_portfolio_max_drawdown').input_value()=='6'
    page.screenshot(path=str(ROOT/'validation/软件界面_v0.2_演示.png'),full_page=True)
    assert not errors,errors
    report['browser_render']='PASS';browser.close()
  except ImportError:
   report['browser_render']='NOT_AVAILABLE'
  except Exception as ex:
   report['browser_render']='FAIL_OR_UNAVAILABLE: '+str(ex)[:500]
  atomic_json(ROOT/'validation/v02_ui_test.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))
 finally:
  server.terminate()
  try:server.wait(timeout=8)
  except subprocess.TimeoutExpired:server.kill();server.wait()
  log.close()
if __name__=='__main__':main()

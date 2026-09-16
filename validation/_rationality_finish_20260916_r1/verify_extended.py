"""Actual balanced search and isolated local HTTP acceptance; synthetic only."""
import argparse,json,os,re,socket,subprocess,sys,tempfile,time,traceback,urllib.request
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'lab/common.py').is_file())
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from lab.common import check_config,source_fingerprint,execution_fingerprint,atomic_json,sha
from lab.release import release_fingerprint
from lab.standard_mining import mine_standard
from test_rationality_070 import portfolio_fixture

def balanced(out):
    events,labels,_,_,_,_,_=portfolio_fixture()
    sid=events[0]['sid'];events=[dict(e,eid=i) for i,e in enumerate(e for e in events if e['sid']==sid)]
    labels={sid:labels[sid]};result={}
    for name,profit in (('upper_bound',10),('profitable_dfs',0)):
        config=check_config({'search_grammar':'balanced_v1','directions':['LIVE_GIVE'],'min_profit':profit})
        root=out/name;root.mkdir()
        state=mine_standard(root,events,labels,100,'LIVE_GIVE',config,lambda **kw:None,lambda:False)
        spec=json.loads((root/'mining/LIVE_GIVE/search_spec.json').read_text(encoding='utf-8'))
        if spec['grammar']!='balanced_v1' or spec['max_conditions']!=3:raise AssertionError('Balanced grammar was not applied')
        if spec['modules']['M03']<=0 or spec['modules']['M05']<=0 or spec['modules']['M04']!=0:raise AssertionError('Balanced module declaration mismatch')
        if state['status']!='COMPLETE' or state['remaining'] or state['raw_total']!=spec['total']:raise AssertionError('Balanced coverage does not reconcile')
        if name=='upper_bound' and state['candidates']!=0:raise AssertionError('Impossible profitable candidate under safe bound')
        if name=='profitable_dfs' and state['candidates']<=0:raise AssertionError('Later profitable child was lost')
        result[name]={'status':'PASS','source_matches':1,'search_backend':state['search_backend'],
            'candidates':state['candidates'],'remaining':state['remaining'],'raw_total':state['raw_total'],'modules':spec['modules']}
    return result

def http_check(out):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    url='http://127.0.0.1:'+str(port);token=None
    with (out/'http.log').open('x',encoding='utf-8') as log:
        p=subprocess.Popen([sys.executable,'-B','-c','from lab.server import serve; import sys; serve(sys.argv[1],int(sys.argv[2]),False)',str(out/'http_workspace'),str(port)],
            cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+30
            while True:
                if p.poll() is not None:raise RuntimeError('Isolated HTTP server exited')
                try:
                    with urllib.request.urlopen(url,timeout=2) as response:html=response.read().decode('utf-8')
                    break
                except OSError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.1)
            if 'balanced_v1' not in html or 'research_policy.js' not in html:raise AssertionError('New UI not wired')
            token=re.search(r'window\.LOCAL_TOKEN="([^"]+)"',html)[1]
            for name in ('research_policy.js','app.js','jobs.js'):
                with urllib.request.urlopen(url+'/'+name,timeout=5) as response:
                    if response.read()!=(ROOT/'web'/name).read_bytes():raise AssertionError('Unexpected UI source: '+name)
            request=urllib.request.Request(url+'/api/state',headers={'X-Local-Token':token})
            with urllib.request.urlopen(request,timeout=5) as response:state=json.load(response)
            if state['jobs'] or not state['default_config']['portfolio_segment_checks'] or state['default_config']['missing_result_status']!='exclude':raise AssertionError('Unexpected isolated UI defaults or jobs')
            return {'status':'PASS','scope':'actual loopback HTTP and source bytes, not full-browser visual acceptance','jobs_started':0}
        finally:
            if token and p.poll() is None:
                try:
                    request=urllib.request.Request(url+'/api/shutdown',data=b'{}',headers={'X-Local-Token':token,'Content-Type':'application/json','Origin':url})
                    urllib.request.urlopen(request,timeout=3).close()
                except OSError:pass
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',required=True);args=parser.parse_args()
    out=Path(args.output_dir).resolve()
    if ROOT not in out.parents:raise ValueError('Evidence must stay in this project')
    out.mkdir(parents=True,exist_ok=False)
    identity=lambda:(source_fingerprint(),execution_fingerprint(),release_fingerprint(ROOT))
    before=identity();report={'status':'RUNNING','engine_hash':before[0],'execution_hash':before[1],'release_hash':before[2],
        'helper_sha256':sha(__file__),'synthetic':True,'real_orders_sent':0}
    try:
        report['balanced']=balanced(out);report['http']=http_check(out)
        report['source_unchanged']=before==identity()
        if not report['source_unchanged']:raise RuntimeError('Source changed')
        report['status']='PASS';return 0
    except Exception as error:
        report.update(status='FAIL',error=str(error),traceback=traceback.format_exc());return 1
    finally:
        atomic_json(out/'extended_acceptance.json',report);print(json.dumps(report,ensure_ascii=True),flush=True)
if __name__=='__main__':raise SystemExit(main())

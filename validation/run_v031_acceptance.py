"""Real Windows service/worker, pause/resume, partial-result and handoff acceptance."""
from pathlib import Path
import argparse,json,os,re,signal,subprocess,sys,time,urllib.request,urllib.error,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,source_fingerprint,atomic_json,sha

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--workspace',required=True)
    args=parser.parse_args();workspace=Path(args.workspace).resolve();workspace.mkdir(parents=True,exist_ok=True)
    out=ROOT/'validation'/'v031';out.mkdir(exist_ok=True);engine_hash=source_fingerprint();started=time.monotonic()
    proc=subprocess.Popen([sys.executable,'-B',str(ROOT/'app.py'),'serve','--no-browser','--port','0','--workspace',str(workspace)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    server_pid=None;report={'engine_hash':engine_hash,'workspace':str(workspace),'platform':sys.platform,'python':sys.version}
    try:
        line=proc.stdout.readline();url=re.search(r'http://127\.0\.0\.1:\d+',line).group(0)
        page=urllib.request.urlopen(url,timeout=20).read().decode('utf-8');token=re.search(r'window.LOCAL_TOKEN="([^"]+)"',page).group(1)
        def api(path,data=None):
            req=urllib.request.Request(url+path,data=None if data is None else json.dumps(data,ensure_ascii=False).encode('utf-8'),headers={'X-Local-Token':token,'Content-Type':'application/json'})
            return json.load(urllib.request.urlopen(req,timeout=60))
        server_pid=api('/api/state')['server_pid']
        try:urllib.request.urlopen(url+'/api/state',timeout=10);raise AssertionError('missing auth accepted')
        except urllib.error.HTTPError as e:assert e.code==403
        def inspect(paths):
            scan=api('/api/inspect',{'paths':paths,'background':True});deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                state=api('/api/inspection?id='+scan['scan_id'])
                if state['status']=='COMPLETE':return state['manifest']
                if state['status']!='RUNNING':raise RuntimeError(state)
                time.sleep(.2)
            raise TimeoutError('input scan')
        def wait_job(jid,want=('DONE','PARTIAL_RESULT'),timeout=1200):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                job=next(j for j in api('/api/state')['jobs'] if j['id']==jid)
                atomic_json(out/'acceptance_progress.json',{'job':jid,'league':job['league'],'status':job['status'],'progress':job['progress']})
                if job['status'] in want:return job
                if job['status']=='ERROR':raise RuntimeError(job['error'])
                time.sleep(.3)
            raise TimeoutError(jid)
        demo=inspect([str(ROOT/'demo')]);league=demo['leagues'][0]['league']
        demo_id=api('/api/create',{'manifest_id':demo['manifest_id'],'leagues':[league],'config':{'profile':'smoke','chunk_size':64}})['jobs'][0]
        wait_job(demo_id,('RUNNING',));api('/api/control',{'job':demo_id,'action':'pause'});wait_job(demo_id,('PAUSED',));api('/api/control',{'job':demo_id,'action':'resume'})
        done=wait_job(demo_id);assert done['status']=='DONE';report['demo_pause_resume']='PASS'
        partial_id=api('/api/create',{'manifest_id':demo['manifest_id'],'leagues':[league],'config':{'profile':'smoke','directions':['LIVE_OVER'],'max_rules_per_direction':2}})['jobs'][0]
        partial=wait_job(partial_id);assert partial['status']=='PARTIAL_RESULT';assert partial['summary']['coverage']['remaining']>0
        report['partial']={'job':partial_id,'api':partial['summary']}
        for name in ('A_有限范围观察开发包.zip','B_本次研究审计包.zip'):
            with zipfile.ZipFile(workspace/partial_id/'packages'/name) as z:
                state=json.loads(z.read('workflow_status.json'));assert state['state']=='PARTIAL_RESULT'
        report['partial_handoff_state']='PASS'
        paths=sorted(Path(args.data).glob('完整指数_中超_*.csv'));assert paths
        real=inspect([str(p) for p in paths]);assert any(x['kind']=='quality' for x in real['files'])
        real_id=api('/api/create',{'manifest_id':real['manifest_id'],'leagues':['中超'],'config':{'profile':'smoke'}})['jobs'][0]
        job=wait_job(real_id,timeout=2400);assert job['status']=='DONE'
        summary=json.loads((workspace/real_id/'summary.json').read_text(encoding='utf-8'))
        audit=json.loads((workspace/real_id/'data_audit.json').read_text(encoding='utf-8'))
        assert summary['verification']['status'] in ('PASS','EMPTY_ROSTER');assert audit['source_quality_loaded'];assert audit['counts']['source_quality_excluded_matches']>0
        report['real']={'job':real_id,'jobdir':str(workspace/real_id),'data_counts':audit['counts'],'summary':summary}
        report['demo_job']=demo_id;report['auth']='PASS';report['status']='PASS';report['seconds']=round(time.monotonic()-started,3)
        assert engine_hash==source_fingerprint(),'code changed during acceptance'
        atomic_json(out/'acceptance.json',report);print(json.dumps({'status':'PASS','real_job':real_id,'counts':audit['counts'],'selected':summary['selected'],'seconds':report['seconds']},ensure_ascii=False),flush=True)
    finally:
        if server_pid is not None:
            try:os.kill(server_pid,signal.SIGTERM)
            except OSError:pass
        else:proc.terminate()
        proc.wait(timeout=20);proc.stdout.close()

if __name__=='__main__':main()

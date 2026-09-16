"""Real-data software smoke regression, not full search or new investment roster."""
from pathlib import Path
import sys,time,json,argparse,shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import *
from lab.store import Store
from lab.data import inspect
from lab.pipeline import run

def main():
 p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True);p.add_argument('--workspace',required=True)
 p.add_argument('--prepared');p.add_argument('--report-dir',default=str(ROOT/'validation'))
 a=p.parse_args();base=Path(a.input_dir);out=Path(a.workspace);reportdir=Path(a.report_dir);reportdir.mkdir(parents=True,exist_ok=True)
 paths=[str(base/f'{k}_{y}.csv') for y in (2024,2025,2026) for k in ('完整指数','比赛索引')]
 cfg={**DEFAULT,'profile':'smoke','directions':list(DIRECTIONS),'select_budget':6}
 manifest=inspect(paths,out);store=Store(out);jid=store.create('中超',cfg,manifest);jd=out/jid
 if a.prepared:
  prepared=Path(a.prepared);audit=read_json(prepared/'data_audit.json')
  assert {(r['sha256'],r['kind']) for r in audit['inputs']}=={(r['sha256'],r['kind']) for r in manifest['files']}
  for n in ('events.jsonl.gz','labels.json','data_audit.json'):shutil.copy2(prepared/n,jd/n)
  atomic_json(jd/'prepared_manifest.json',{n:sha(jd/n) for n in ('events.jsonl.gz','labels.json','data_audit.json')})
 atomic_json(reportdir/'real_smoke_location.json',{'run_id':jid,'workspace':str(out),'jobdir':str(jd),'profile':'smoke'})
 start=time.perf_counter();before=source_fingerprint();r=run(out,jid);after=source_fingerprint()
 if r is None:raise RuntimeError('本次未完成运行，不可标通过')
 if before!=after:raise RuntimeError('测试期间源码改变，作废此次验收')
 result={'run_id':jid,'engine_hash':after,'elapsed_seconds':round(time.perf_counter()-start,3),
  'input_counts':read_json(jd/'data_audit.json')['counts'],'evaluated':r['coverage']['evaluated'],
  'candidates':r['coverage']['candidates'],'selected':r['selected'],'backup_selected':r['backup_selected'],
  'local_search_complete':r['coverage']['local_search_complete'],'selection_status':r['selection_status'],
  'verification':r['verification'],'packages':r['packages'],'packaging':read_json(jd/'packaging_verification.json'),
  'v3_status':r['v3_status'],'status':'PASS','profile':'smoke',
  'scope':'真实中超输入，有限快速功能规格全链路；非完整v3重挖，不替换旧38条名单'}
 atomic_json(reportdir/'real_smoke_run.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
 return 0
if __name__=='__main__':raise SystemExit(main())

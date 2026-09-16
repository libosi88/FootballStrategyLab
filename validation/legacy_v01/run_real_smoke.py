from pathlib import Path
import sys,time,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import *
from lab.store import Store
from lab.data import inspect
from lab.pipeline import run
base=Path('/mnt/data');out=base/'_fsl_tests'/'real_workspace';store=Store(out)
paths=[str(base/f'{k}_{y}.csv') for y in (2024,2025,2026) for k in ('完整指数','比赛索引')]
config={**DEFAULT,'profile':'smoke','directions':list(DIRECTIONS),'select_budget':4}
manifest=inspect(paths,out);jid=store.create('中超',config,manifest)
# Reuse ONLY byte-verified preparation made from this exact input; no reused strategy results.
jd=out/jid
import shutil
for src,dst in [('real_events.jsonl.gz','events.jsonl.gz'),('real_labels.json','labels.json'),('real_audit.json','data_audit.json')]:shutil.copy2(base/'_fsl_tests'/src,jd/dst)
atomic_json(jd/'prepared_manifest.json',{n:sha(jd/n) for n in ('events.jsonl.gz','labels.json','data_audit.json')})
start=time.perf_counter();r=run(out,jid)
result={'run_id':jid,'elapsed_seconds':round(time.perf_counter()-start,3),'events':read_json(jd/'data_audit.json')['counts'],'coverage':r['coverage'],'selected':r['selected'],'verification':r['verification'],'profile':'smoke','note':'真实中超数据，仅本版快速功能规格；不声称完整搜索'}
atomic_json(ROOT/'validation'/'real_smoke_run.json',result)
print(json.dumps({k:v for k,v in result.items() if k!='coverage'},ensure_ascii=False,indent=2))

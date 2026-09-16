"""Optional regression against an actual prior handoff (not bundled private data)."""
from pathlib import Path
import sys,json,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import *
from lab.features import SignalEngine
from lab.data import inspect,load_inputs
base=Path('/mnt/data');root=Path(__file__).resolve().parent
paths=[str(base/f'{kind}_{year}.csv') for year in (2024,2025,2026) for kind in ('完整指数','比赛索引')]
man=inspect(paths,base/'_fsl_tests')
ev,labels,scale,audit=load_inputs(man,'中超','皇冠')
write_jsonl(base/'_fsl_tests'/'real_events.jsonl.gz',ev);atomic_json(base/'_fsl_tests'/'real_labels.json',labels);atomic_json(base/'_fsl_tests'/'real_audit.json',audit)
with zipfile.ZipFile(base/'中超38条_触发程序开发交接包_v1.zip') as z:
 old=json.loads(z.read('01_原38条规则/自动模拟名单_38条.json'))
 expected=[json.loads(x) for x in z.read('03_历史标准记录/首触发验收_源字段.jsonl').decode('utf-8-sig').splitlines() if x]
 mapping={'same':'samewater','lastenter':'returnwater_keep','diff':'goal_diff','absdiff':'abs_diff','total':'total_goals','score':'score_code'}
 rules=[]
 for r in old['strategies']:
  cs=[]
  for a in r['atoms']:
   f=mapping.get(a['feature'],a['feature']).replace('mix','path')
   c={'feature':f,'op':a['op'],'value':a['value']}
   if c['op']=='between':c.update(op='range',value=a['value'][0],upper=a['value'][1])
   cs.append(c)
  rules.append({'id':r['id'],'direction':r['direction'],'conditions':cs})
engine=SignalEngine(rules);got=[]
for e in ev:
 s=engine.feed(e)
 if labels[e['sid']]['eligible']:got+=s
by_hash={r['sha256']:Path(r['path']).name for r in man['files']}
a={(s['strategy_id'],s['sId'],s['source_file'],s['source_line'],s['physical_side']-1,scaled(s['expected_line'],4),scaled(s['expected_hk_water'],scale)) for s in expected}
b={(s['strategy_id'],s['sid'],by_hash[ev[s['eid']]['source']],ev[s['eid']]['row'],s['side'],s['line'],s['water']) for s in got}
result={'events':len(ev),'strategies':len(rules),'expected':len(a),'actual':len(b),'missing':len(a-b),'extra_or_mismatch':len(b-a),'missing_examples':list(a-b)[:10],'extra_examples':list(b-a)[:10],'status':'PASS' if a==b else 'FAIL','scope':'历史38条首次信号迁移；不是新v3挖掘覆盖，也不是实盘验证'}
atomic_json(root/'legacy38_regression.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))

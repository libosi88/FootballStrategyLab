"""Optional regression using the user's frozen legacy handoff, never reranks it."""
from pathlib import Path
import sys,json,zipfile,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from collections import Counter
from lab.common import *
from lab.contracts import bind_rule,event_contract
from lab.features import SignalEngine
from lab.data import inspect,load_inputs


def main():
 p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True);p.add_argument('--handoff',required=True);p.add_argument('--output',required=True)
 a=p.parse_args();base=Path(a.input_dir);out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
 paths=[str(base/f'{kind}_{year}.csv') for year in (2024,2025,2026) for kind in ('完整指数','比赛索引')]
 man=inspect(paths,out);ev,labels,scale,audit=load_inputs(man,'中超','皇冠')
 with zipfile.ZipFile(a.handoff) as z:
  old=json.loads(z.read('01_原38条规则/自动模拟名单_38条.json'))
  expected=[json.loads(x) for x in z.read('03_历史标准记录/首触发验收_源字段.jsonl').decode('utf-8-sig').splitlines() if x]
 mapping={'same':'samewater','lastenter':'returnwater_keep','diff':'goal_diff','absdiff':'abs_diff','total':'total_goals','score':'score_code'}
 rules=[];c=event_contract(ev[0])
 for r in old['strategies']:
  cs=[]
  for atom in r['atoms']:
   f=mapping.get(atom['feature'],atom['feature']).replace('mix','path');x={'feature':f,'op':atom['op'],'value':atom['value']}
   if x['op']=='between':x.update(op='range',value=atom['value'][0],upper=atom['value'][1])
   cs.append(x)
  rules.append(bind_rule({'id':r['id'],'direction':r['direction'],'conditions':cs},c))
 engine=SignalEngine(rules);got=[]
 for i,e in enumerate(ev):
  sig=engine.feed(e)
  if labels[e['sid']]['eligible']:got+=sig
  if i==len(ev)//2:
   engine=SignalEngine(rules,json.loads(canonical(engine.snapshot())))
   assert engine.feed(e)==[]
 by_hash={r['sha256']:Path(r['path']).name for r in man['files']}
 def expected_key(s):
  return (s['strategy_id'],s['sId'],s['source_file'],s['source_line'],s['physical_side']-1,
          scaled(s['expected_line'],4),scaled(s['expected_hk_water'],scale),timestamp(s['raw_change_time']),
          tuple(score(s['raw_score'])) if s['raw_status']=='滚' else (0,0))
 def actual_key(s):
  e=ev[s['eid']]
  return (s['strategy_id'],s['sid'],by_hash[e['source']],e['row'],s['side'],s['line'],s['water'],s['ts'],tuple(s['score']))
 left=Counter(map(expected_key,expected));right=Counter(map(actual_key,got))
 missing=left-right;extra=right-left
 result={'events':len(ev),'strategies':len(rules),'expected':sum(left.values()),'actual':sum(right.values()),
  'missing':sum(missing.values()),'extra_or_changed':sum(extra.values()),'scope':'旧38条逐笔信号迁移，含时间和当时比分；非重新挖掘/实盘验证',
  'restart_duplicate':'PASS','input_hashes':[(r['path'],r['sha256']) for r in man['files']],
  'status':'PASS' if not missing and not extra else 'FAIL','engine_version':VERSION,'engine_hash':source_fingerprint()}
 atomic_json(out/'legacy38_regression.json',result)
 # Prepared cache retains full original lineage for the full-flow smoke test, not old signals/candidates.
 write_jsonl(out/'events.jsonl.gz',ev);atomic_json(out/'labels.json',labels);atomic_json(out/'data_audit.json',audit)
 print(json.dumps(result,ensure_ascii=False,indent=2))
 return 0 if result['status']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())

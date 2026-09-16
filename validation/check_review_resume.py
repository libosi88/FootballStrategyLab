"""Re-enter the completed screen using its content-bound direction checkpoints.
Fail deliberately if any candidate must be recalculated; do not re-run mining.
"""
from pathlib import Path
import sys,argparse,time,tempfile,shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import *
import lab.selection as selection
p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--out',required=True);a=p.parse_args()
root=Path(a.run);before=read_json(root/'results'/'rules.json')['rules'];h=source_fingerprint();t=time.perf_counter()
with tempfile.TemporaryDirectory(prefix='review_resume_') as td:
 dst=Path(td)
 for n in ('config.json','prepared_manifest.json','labels.json','data_audit.json'):shutil.copy2(root/n,dst/n)
 for n in ('mining','review_checkpoints','selection_checkpoints'):shutil.copytree(root/n,dst/n)
 events=list(read_jsonl(root/'events.jsonl.gz'));labels=read_json(root/'labels.json');scale=read_json(root/'data_audit.json')['water_scale']
 def should_not_recalculate(*args,**kwargs):raise AssertionError('已完成方向不应重新计算候选首触发')
 selection.first_indices=should_not_recalculate
 result=selection.select(dst,events,labels,scale,read_json(root/'config.json'),lambda **kw:None,lambda:False)
 after=read_json(dst/'results'/'rules.json')['rules']
 assert [r['id'] for r in before]==[r['id'] for r in after]
 assert h==source_fingerprint()
 report={'status':'PASS','selected':len(after),'completed_direction_checkpoints':len(list((dst/'review_checkpoints').glob('*/manifest.json'))),
 'candidate_recalculation_forbidden':True,'same_roster':True,'engine_hash':h,'seconds':round(time.perf_counter()-t,3),
 'scope':'completed-direction restore; incomplete current direction still rebuilds'}
 atomic_json(a.out,report);print(canonical(report))

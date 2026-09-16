"""Fixed real-event test domain; no outcome-dependent choice of atoms.
Full v3 is NOT claimed. Compares all raw combinations in this test domain with
full-event no-hit pruning, preserving all positive/negative/zero economics.
"""
from pathlib import Path
import sys,argparse,time,itertools
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import *
from lab.rules import dictionary
from lab.search_plan import make_plan_spec,TaskPlan
from lab.mining import pack_masks,evaluate,first_indices
from lab.sparse_plan import prepare_sparse_plan

def main():
 p=argparse.ArgumentParser();p.add_argument('--arrays',required=True);p.add_argument('--work',required=True);a=p.parse_args()
 t=time.perf_counter();h=source_fingerprint();out=Path(a.work);out.mkdir(parents=True,exist_ok=True)
 with np.load(a.arrays,allow_pickle=False) as z:arr={k:z[k] for k in z.files}
 allats,_,_=dictionary(arr,'LIVE_OVER',100,'routine');selected=[]
 # Mechanical first/last atoms of each declared feature group; never use pnl to choose.
 features=['line','water','otherwater','samewater','goal_diff','score_code','minute','path2_keep','path3_keep','path3_reset',
           'window_16_31_line_init','window_16_31_water_init','window_31_46_otherwater_init']
 for f in features:
  pool=[x for x in allats if x['feature']==f]
  selected.extend(pool[:3]+pool[-3:])
 selected=list({canonical(a):a for a in selected}.values());n=len(selected)
 raw=make_plan_spec(selected,list(range(n)),list(range(n)),'routine')
 ma,nz,off,en,pay=pack_masks(arr,selected)
 sp=prepare_sparse_plan(out,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False)
 dense={};sparse={}
 for plan,result in ((TaskPlan(raw),dense),(sp,sparse)):
  it=plan.iter_from()
  while block:=list(itertools.islice(it,2048)):
   c=np.full((len(block),3),-1,np.int64)
   for j,ids in enumerate(block):c[j,:len(ids)]=ids
   rr=evaluate(c,ma,nz,off,en,pay)
   for ids,r in zip(block,rr):result[ids]=(int(r[0]),int(r[1]))
 differences=[]
 for ids,r in dense.items():
  if ids in sparse:
   if sparse[ids]!=r:differences.append(ids)
  elif not sp.is_proven_zero(ids) or r!=(0,0):differences.append(ids)
 candidate_missing=sum(r[1]>2000 and ids not in sparse for ids,r in dense.items())
 # Direct numpy all-event conjunctive reference on an outcome-independent sample.
 checks=0
 for ids in list(dense)[::max(1,len(dense)//200)]:
  ix=first_indices(ids,arr,selected);expected=(len(ix),int(arr['pnl'][ix].sum()))
  if dense[ids]!=expected:differences.append(ids)
  checks+=1
 report={'engine_hash':h,'input_arrays_sha256':sha(a.arrays),'events':len(arr['eid']),'atoms':n,'raw_expressions':len(dense),
 'direct_evaluations_with_proof':len(sparse),'proven_zero':sp.spec['proven_zero'],
 'metric_differences':len(differences),'profitable_missing':candidate_missing,'direct_first_event_reference_checks':checks,
 'positive_candidates_in_this_test':sum(r[1]>2000 for r in dense.values()),'seconds':round(time.perf_counter()-t,3),
 'status':'PASS' if not differences and not candidate_missing else 'FAIL',
 'scope':'fixed validation subdictionary only, on all real LIVE_OVER eligible events; not full routine/expanded or v3'}
 assert h==source_fingerprint(),'code changed during test'
 atomic_json(out/'report.json',report);atomic_json(out/'test_atoms.json',selected)
 print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['status']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())

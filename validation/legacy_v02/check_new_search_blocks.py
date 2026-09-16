"""Targeted real-data rule evaluation, NOT complete routine/expanded search.
Checks new ordinary-triple/timed/fine contextual plan membership and exact P&L
against a separate predicate+first-match implementation, using frozen feature arrays.
Feature correctness itself is covered by separate prefix-reference tests/old38 golden.
"""
from pathlib import Path
import sys,json,itertools,time,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from lab.common import *
from lab.rules import dictionary
from lab.search_plan import *
from lab.mining import pack_masks,evaluate


def rank_comb(pool,selected):
 ix=[pool.index(x) for x in selected];n=len(pool);k=len(ix);rank=0;lo=0
 for j,x in enumerate(ix):
  rest=k-j-1;rank+=choose(n-lo,rest+1)-choose(n-x,rest+1);lo=x+1
 return rank


def main():
 p=argparse.ArgumentParser();p.add_argument('--arrays',required=True);a=p.parse_args()
 with np.load(a.arrays,allow_pickle=False) as z:arr={k:z[k] for k in z.files}
 scale=100;aa,pp,tt=dictionary(arr,'LIVE_OVER',scale,'expanded');plan=TaskPlan(make_plan_spec(aa,pp,tt,'expanded'))
 def find(f,op,v,u=None):
  return next(i for i,x in enumerate(aa) if x['feature']==f and x['op']==op and x['value']==v and (u is None or x.get('upper')==u))
 timeid=find('minute','range',0,46);scoreid=find('goal_diff','eq',-1);ctx=sorted((timeid,scoreid))
 picked=[];base=0
 for b in plan.blocks:
  if b['id']=='L03_CORE_TRIPLE':
   ids=[i for i,x in enumerate(aa) if x['feature'].startswith('path3') and 'span' not in x]
   for ix in ids:
    c=tuple(sorted((*ctx,ix)));offset=base+rank_comb(b['pool'],c)
    assert next(plan.iter_from(offset))==c;picked.append((b['id'],offset,c))
  if b['kind']=='anchored':
   # Fixed evenly-spaced anchor indices selected without inspecting any return labels.
   anchors=b['anchors'];indices=sorted(set([0,len(anchors)//4,len(anchors)//2,len(anchors)*3//4,len(anchors)-1])) if anchors else []
   tail=ctx if b['k']==2 else [timeid]
   for ai in indices:
    anchor=anchors[ai];c=tuple(sorted((anchor,*tail)))
    offset=base+ai*choose(len(b['pool']),b['k'])+rank_comb(b['pool'],tail)
    assert next(plan.iter_from(offset))==c;picked.append((b['id'],offset,c))
  base+=b['size']
 cc=np.full((len(picked),3),-1,dtype=np.int64)
 for i,(_,_,c) in enumerate(picked):cc[i,:len(c)]=c
 masks,nz,off,end,pnl=pack_masks(arr,aa);got=evaluate(cc,masks,nz,off,end,pnl)
 rows=[];diff=0
 for i,(block,pos,c) in enumerate(picked):
  ok=np.ones(len(arr['mid']),bool)
  for ix in c:
   x=aa[ix];v=arr[x['feature']];valid=v!=MISSING
   if x['op']=='ge':valid &= v>=x['value']
   elif x['op']=='le':valid &= v<=x['value']
   elif x['op']=='eq':valid &= v==x['value']
   else:valid &= (v>=x['value'])&(v<x['upper'])
   mode=x['feature'].rsplit('_',1)[-1]
   if 'span' in x:
    k=x['feature'].split('_')[0][4:];sp=arr['span'+k+'_'+mode];valid &= (sp!=MISSING)&(sp<=x['span'])
   if x.get('pulse'):valid &= arr['pulse_'+mode]==1
   ok &= valid
  first={}
  for j in np.flatnonzero(ok):
   mid=int(arr['mid'][j])
   if mid not in first:first[mid]=int(j)
  ref=[len(first),sum(int(arr['pnl'][j]) for j in first.values())]
  if ref!=got[i].tolist():diff+=1
  rows.append({'block':block,'plan_position':pos,'atom_indices':';'.join(map(str,c)),
               'conditions':' AND '.join(aa[x]['label'] for x in c),'n':ref[0],'net_i':ref[1],
               'engine_n':int(got[i,0]),'engine_net_i':int(got[i,1]),'same':ref==got[i].tolist()})
 assert diff==0
 csv_write(ROOT/'validation/新增搜索交叉_定点回放.csv',rows)
 r={'status':'PASS','version':VERSION,'engine_hash':source_fingerprint(),
  'data_scope':'真实中超三年LIVE_OVER冻结全报价特征数组；非全量routine/expanded重挖',
  'input_arrays_sha256':sha(a.arrays),'feature_events':len(arr['eid']),'evaluated_rules':len(rows),
  'new_block_rule_counts':{k:sum(x['block']==k for x in rows) for k in sorted(set(x['block'] for x in rows))},
  'return_or_count_differences':diff,'evaluated_with_hits':sum(x['n']>0 for x in rows),
  'reference_limit':'独立谓词/每场首行/累计计算；输入特征数组共享，非独立赛果/新样本/全部特征核验',
  'full_routine_or_expanded_complete':False,'v3_status':'PARTIAL_CORE'}
 atomic_json(ROOT/'validation/新增搜索交叉_定点回放.json',r);print(json.dumps(r,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

"""Chunked, exact all-event conjunction scan. Every evaluated rule is persisted."""
from pathlib import Path
from collections import defaultdict
from contextlib import ExitStack
import numpy as np, itertools, os, shutil
from .common import *
from .features import FeatureStream,release_match_state
from .rules import dictionary,mask
from .search_plan import make_plan_spec,TaskPlan
from .contracts import event_contract
try:
 from numba import njit
 ACCELERATOR='numba'
except ImportError:
 ACCELERATOR='python'
 def njit(*args,**kwargs):
  return args[0] if args and callable(args[0]) else lambda f:f

@njit(cache=True,inline='always')
def _ctz(x):
 """Bit index for nonzero uint64 words, in at most six checks."""
 n=0
 if (x & np.uint64(0xffffffff))==0:n+=32;x=x>>np.uint64(32)
 if (x & np.uint64(0xffff))==0:n+=16;x=x>>np.uint64(16)
 if (x & np.uint64(0xff))==0:n+=8;x=x>>np.uint64(8)
 if (x & np.uint64(0xf))==0:n+=4;x=x>>np.uint64(4)
 if (x & np.uint64(0x3))==0:n+=2;x=x>>np.uint64(2)
 if (x & np.uint64(0x1))==0:n+=1
 return n

@njit(cache=True)
def evaluate(combos,masks,nonzero,offsets,word_end,pnl):
 out=np.zeros((len(combos),2),dtype=np.int64)
 for r in range(len(combos)):
  a,b,c=combos[r];n=0;net=0;nextw=0
  if a<0:
   for w in range(len(word_end)):
    if w<nextw:continue
    bits=masks[-1,w]
    if bits!=0:
     bit=_ctz(bits);n+=1;net+=pnl[w*64+bit];nextw=word_end[w]
  else:
   rare=a
   if b>=0 and offsets[b+1]-offsets[b]<offsets[rare+1]-offsets[rare]:rare=b
   if c>=0 and offsets[c+1]-offsets[c]<offsets[rare+1]-offsets[rare]:rare=c
   for z in range(offsets[rare],offsets[rare+1]):
    w=nonzero[z]
    if w<nextw:continue
    bits=masks[a,w]
    if b>=0:bits=bits&masks[b,w]
    if c>=0:bits=bits&masks[c,w]
    if bits!=0:
     bit=_ctz(bits);n+=1;net+=pnl[w*64+bit];nextw=word_end[w]
  out[r,0]=n;out[r,1]=net
 return out

def first_indices(ids,arrays,atoms):
 m=np.ones(len(arrays['eid']),dtype=bool)
 for i in ids:m &= mask(atoms[i],arrays)
 idx=np.flatnonzero(m)
 if not len(idx):return idx
 return idx[np.r_[True,arrays['mid'][idx[1:]]!=arrays['mid'][idx[:-1]]]]

def build_direction(events,labels,scale,direction,config=None):
 phase=0 if direction.startswith('PRE') else 1
 market=0 if direction.endswith(('OVER','UNDER')) else 1
 standard=bool(config and config.get('profile')=='standard')
 if standard:
  from .standard_features import StandardFeatureStream
  from .research_standard import trigger_window_allows
  stream=StandardFeatureStream(contract=event_contract(events[0]) if events else None,cross_stale_minutes=config.get('cross_stale_minutes'),cross_stale_minutes_prematch=config.get('cross_stale_minutes_prematch'))
 else:stream=FeatureStream(contract=event_contract(events[0]) if events else None)
 cols=defaultdict(list);previous_sid=None
 historical_pnl=None
 if config and historical_objective(config):
  from .historical_pricing import minute_close_payoffs
  historical_pnl=minute_close_payoffs(events,labels,scale)
 for e in events:
  target=(e['phase'],e['market'])==(phase,market)
  if not target and not standard:continue
  if previous_sid is not None and e['sid']!=previous_sid:release_match_state(stream,previous_sid)
  previous_sid=e['sid']
  fs=stream.feed(e)
  if not target:continue
  if fs is None:continue
  if standard and not trigger_window_allows(e,config):continue
  side=side_for(direction,e['line']);l=labels[e['sid']]
  if side is None or not l['eligible']:continue
  if phase and market and e['score'][0]==MISSING:continue
  f=fs[side];margin=sum(l['final']) if market==0 else l['final'][0]-l['final'][1]-(e['score'][0]-e['score'][1] if phase else 0)
  pnl=int(historical_pnl[e['eid'],side]) if historical_pnl is not None else settlement(e['line'],e['water'][side],margin,side,scale)
  q=int(bool(phase and e['score'][0]!=MISSING and any(e['score'][i]>l['final'][i] for i in (0,1))))
  f.update(eid=e['eid'],mid=e['mid'],side=side,pnl=pnl,year=l['year'],ts=e['ts'],quality=q)
  for k,v in f.items():cols[k].append(v)
 if not cols:
  # Build empty schema from known stream fields if the direction has no eligible quote.
  return {'eid':np.array([],dtype=np.int64),'mid':np.array([],dtype=np.int64)}
 return {k:np.asarray(v,dtype=np.int64) for k,v in cols.items()}

def pack_masks(arrays,atoms):
 mids=arrays['mid'];n=len(mids)
 if not n:return np.empty((1,0),dtype=np.uint64),np.empty(0,dtype=np.int64),np.zeros(2,dtype=np.int64),np.empty(0,dtype=np.int64),np.empty(0,dtype=np.int64)
 cuts=np.r_[0,np.flatnonzero(mids[1:]!=mids[:-1])+1,n]
 positions=np.empty(n,dtype=np.int64);word_ends=[];cur=0
 for a,b in zip(cuts[:-1],cuts[1:]):
  words=(int(b-a)+63)//64;positions[a:b]=np.arange(cur*64,cur*64+b-a);word_ends.extend([cur+words]*words);cur+=words
 masks=np.zeros((len(atoms)+1,cur),dtype=np.uint64);size=cur*64;buf=np.zeros(size,dtype=np.uint8)
 for i,a in enumerate(atoms):
  buf.fill(0);buf[positions]=mask(a,arrays);masks[i]=np.packbits(buf,bitorder='little').view('<u8')
 buf.fill(0);buf[positions]=1;masks[-1]=np.packbits(buf,bitorder='little').view('<u8')
 pp=np.zeros(size,dtype=np.int64);pp[positions]=arrays['pnl']
 nz=[np.flatnonzero(x).astype(np.int64) for x in masks[:-1]];offsets=np.r_[0,np.cumsum([len(x) for x in nz])].astype(np.int64)
 return masks,np.concatenate(nz) if nz else np.array([],dtype=np.int64),offsets,np.asarray(word_ends,dtype=np.int64),pp

class Paused(Exception):pass
class ResourcePaused(Paused):pass

def check_resources(path,estimated_bytes,config):
 limit=int(config.get('max_mask_mb',1024))*1024*1024
 if estimated_bytes>limit:
  raise ResourcePaused(f'预计掩码/索引峰值{estimated_bytes/1048576:.1f}MiB超过预算{limit/1048576:.0f}MiB；未删搜索范围，请提高资源预算或等待分片升级')
 free=shutil.disk_usage(path).free
 if free<int(config.get('min_free_disk_mb',256))*1024*1024:
  raise ResourcePaused('磁盘剩余空间低于保护阈值；已提交断点保留')

def estimate_mask_bytes(arrays,atoms):
 mids=arrays['mid']
 if not len(mids):return 0
 cuts=np.r_[0,np.flatnonzero(mids[1:]!=mids[:-1])+1,len(mids)]
 words=int(sum((int(b-a)+63)//64 for a,b in zip(cuts[:-1],cuts[1:])))
 # Masks + worst-case sparse index list and its concatenation + padded payoff/work buffers.
 return int((len(atoms)+1)*words*8*3+words*64*9+sum(a.nbytes for a in arrays.values()))


def mine_direction(jobdir,events,labels,scale,direction,config,update,should_pause):
 if config.get('profile')=='standard':
  from .standard_mining import mine_standard,release_direction_caches
  state=mine_standard(jobdir,events,labels,scale,direction,config,update,should_pause)
  # Both cache context managers have closed by now, so a finished direction can give
  # back its mask store and column cache. review_columns is released after S4 instead.
  release_direction_caches(Path(jobdir)/'mining'/direction,state,config,update,('masks.sqlite3','virtual_columns'))
  return state
 dest=Path(jobdir)/'mining'/direction;dest.mkdir(parents=True,exist_ok=True)
 cache=dest/'arrays.npz';dictionary_file=dest/'dictionary.json'
 state=read_json(dest/'state.json',{'next':0,'status':'NEW','parts':[]})
 semantic_config={k:v for k,v in config.items() if k not in ('max_rules_per_direction','chunk_size','max_mask_mb','min_free_disk_mb')}
 run_hash=digest({'version':VERSION,'direction':direction,'config':semantic_config,
  'input':read_json(Path(jobdir)/'prepared_manifest.json') or digest({'events':events,'labels':labels,'scale':scale})})
 if state.get('run_hash',run_hash)!=run_hash:raise ValueError('挖掘任务身份改变，拒绝混用断点')
 state['run_hash']=run_hash
 expected_start=0
 for x in state.get('parts',[]):
  if x['start']!=expected_start or sha(dest/x['file'])!=x['sha256']:raise ValueError('评价账不连续或校验不一致')
  expected_start+=x['rows']
 if expected_start!=state.get('next',0):raise ValueError('断点与已提交记录数不一致')
 if state.get('status')=='COMPLETE':
  if sha(cache)!=state.get('arrays_hash') or sha(dictionary_file)!=state.get('dictionary_hash'):raise ValueError('完成任务的缓存或字典被改动')
  for name,h in state.get('proof_files',{}).items():
   if sha(dest/name)!=h:raise ValueError('零交集覆盖证明被修改，不能认定已完成')
  return state
 if cache.exists() and dictionary_file.exists() and state.get('arrays_hash')==sha(cache):
  with np.load(cache,allow_pickle=False) as z:arrays={k:z[k] for k in z.files}
  spec=read_json(dictionary_file)
 else:
  if state.get('next',0):raise ValueError('有断点但缓存缺失/改变，禁止混用结果')
  arrays=build_direction(events,labels,scale,direction)
  if not len(arrays['eid']):
   st={'next':0,'total':0,'status':'NO_DATA','parts':[],'direction':direction}
   atomic_json(dest/'state.json',st);atomic_json(dictionary_file,{'atoms':[],'pairs':[],'triples':[]});np.savez_compressed(cache,**arrays);return st
  atoms,pairs,triples=dictionary(arrays,direction,scale,config['profile'])
  plan_spec=make_plan_spec(atoms,pairs,triples,config['profile'])
  spec={'profile':config['profile'],'semantics':SEMANTICS,'atoms':atoms,'pairs':pairs,'triples':triples,'scale':scale,'plan':plan_spec}
  np.savez_compressed(cache,**arrays);atomic_json(dictionary_file,spec)
  state['arrays_hash']=sha(cache);state['dictionary_hash']=sha(dictionary_file)
 if sha(dictionary_file)!=state.get('dictionary_hash'):raise ValueError('搜索字典改变，必须新建任务')
 atoms=spec['atoms'];plan=TaskPlan(spec['plan']);total=plan.total
 state['total']=total;state['direction']=direction;state['status']='RUNNING';atomic_json(dest/'state.json',state)
 update(direction=direction,total_rules=total,evaluated=state['next'],message='建立完整事件位集合（不是盈利父规则扩展）')
 state['mask_estimate_bytes']=estimate_mask_bytes(arrays,atoms)
 state['local_blocks']=plan.coverage(state['next']);atomic_json(dest/'state.json',state)
 try:check_resources(dest,state['mask_estimate_bytes'],config)
 except ResourcePaused as ex:
  state['status']='RESOURCE_BLOCKED';state['resource_reason']=str(ex);atomic_json(dest/'state.json',state);raise
 if len(arrays['pnl']) and len(set(arrays['mid'].tolist()))*max(abs(int(arrays['pnl'].min())),abs(int(arrays['pnl'].max())))>2**63-1:raise OverflowError('收益累计可能超出int64')
 masks,nz,offsets,ends,pp=pack_masks(arrays,atoms)
 with ExitStack() as resources:
  if config.get('safe_nohit_pruning',True) and config['profile']!='smoke':
   from .sparse_plan import prepare_sparse_plan
   plan=resources.enter_context(prepare_sparse_plan(dest,spec['plan'],masks,nz,offsets,config,update,should_pause))
   total=plan.total;state['raw_total']=spec['plan']['total'];state['proven_zero']=plan.spec['proven_zero'];state['total']=total
   state['proof_files']={n:sha(dest/n) for n in ('sparse_plan.json','sparse_groups.npz','nohit_graph.npy')}
   state['plan_kind']='full_event_nohit_graph';state['local_blocks']=plan.coverage(state['next'])
   atomic_json(dest/'state.json',state)
  itr=plan.iter_from(state['next'])
  while True:
   if should_pause():state['status']='PAUSED';atomic_json(dest/'state.json',state);raise Paused()
   cap=int(config.get('max_rules_per_direction',0));left=cap-state['next'] if cap else int(config['chunk_size'])
   if cap and left<=0:
    state['status']='COMPLETE' if state['next']==total else 'BUDGET_STOP'
    atomic_json(dest/'state.json',state);return state
   check_resources(dest,0,config)
   block=list(itertools.islice(itr,min(int(config['chunk_size']),left)))
   if not block:break
   combos=np.full((len(block),3),-1,dtype=np.int64)
   for j,ids in enumerate(block):combos[j,:len(ids)]=ids
   result=evaluate(combos,masks,nz,offsets,ends,pp)
   name=f"part_{state['next']:014d}.npz";tmp=dest/(name+'.tmp')
   with tmp.open('wb') as f:np.savez_compressed(f,conditions=combos,metrics=result);f.flush();os.fsync(f.fileno())
   atomic_replace(tmp,dest/name)
   count=int((result[:,1]>money_threshold(config['min_profit'],2*scale)).sum())
   state['parts'].append({'file':name,'start':state['next'],'rows':len(block),'sha256':sha(dest/name),'candidates':count})
   state['next']+=len(block);state['candidates']=state.get('candidates',0)+count
   state['local_blocks']=plan.coverage(state['next']);state['cursor']=list(plan.locate(state['next']))
   atomic_json(dest/'state.json',state)
   update(direction=direction,total_rules=total,evaluated=state['next'],candidates=state['candidates'],message='实际已提交评价分片')
  state['status']='COMPLETE';atomic_json(dest/'state.json',state);return state

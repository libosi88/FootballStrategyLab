from contextlib import ExitStack
import unittest, tempfile, json, copy
from pathlib import Path
import numpy as np
from lab.common import DEFAULT,MISSING,canonical
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.features import FeatureStream, SignalEngine,validate_rules
from lab.feature_extensions import WINDOWS
from lab.rules import matches,mask,dictionary,unit,label
from lab.search_plan import make_plan_spec,TaskPlan
from lab.sparse_plan import prepare_sparse_plan
from lab.mining import pack_masks,evaluate
from lab.review_cache import load_review,save_review
from lab.selection import neighbors
from test_core import event,TEST_CONTRACT

class BoundWindows(unittest.TestCase):
 def test_bound_first_not_stage_first(self):
  s=FeatureStream();s.feed({**event(0,line=2,w0=100),'minute':15})
  f=s.feed({**event(1,line=4,w0=90),'minute':16})[0]
  self.assertEqual(f['line_init'],2);self.assertEqual(f['window_16_31_line_init'],0)
  f=s.feed({**event(2,line=5,w0=95),'minute':20})[0]
  self.assertEqual(f['window_16_31_line_init'],1);self.assertEqual(f['window_16_31_water_init'],5)
 def test_bound_first_before_score_filter(self):
  r={'id':'x','direction':'LIVE_GIVE','conditions':[{'feature':'window_16_31_water_init','op':'ge','value':10},{'feature':'goal_diff','op':'eq','value':-1}]}
  eng=SignalEngine([r],contract=TEST_CONTRACT)
  a={**event(0,w0=80,score0=(0,0)),'minute':16}
  b={**event(1,w0=95,score0=(0,1)),'minute':20}
  self.assertEqual(eng.feed(a),[]);self.assertEqual(len(eng.feed(b)),1)
 def test_window_end_is_missing(self):
  s=FeatureStream();s.feed({**event(0),'minute':16})
  f=s.feed({**event(1),'minute':31})[0]
  self.assertEqual(f['window_16_31_water_init'],MISSING)
  self.assertEqual(f['window_31_46_water_init'],0)
 def test_first_valid_not_closed(self):
  s=FeatureStream();s.feed({**event(0,valid=False),'minute':16})
  f=s.feed({**event(1,w0=80),'minute':17})[0]
  self.assertEqual(f['window_16_31_water_init'],0)
 def test_closure_retains_bound_base(self):
  s=FeatureStream();s.feed({**event(0,w0=80),'minute':16});s.feed({**event(1,valid=False),'minute':17})
  f=s.feed({**event(2,w0=95),'minute':18})[0]
  self.assertEqual(f['window_16_31_water_init'],15)
 def test_pre_and_halftime_not_numeric_zero(self):
  s=FeatureStream();f=s.feed({**event(0),'minute':-2})[0]
  self.assertTrue(all(f[f'window_{lo}_{hi}_line_init']==MISSING for lo,hi in WINDOWS))
 def test_units_and_perturbation(self):
  a={'feature':'window_16_31_line_init','op':'ge','value':3}
  self.assertEqual(unit(a['feature'],100),4)
  self.assertEqual([x['value'] for x in neighbors(a,100)],[2,4]);self.assertIn('0.75',label(a,100))
 def test_independent_prefix_window_reference(self):
  rng=np.random.default_rng(9102);hist=[];s=FeatureStream()
  for i in range(120):
   e={**event(i,line=int(rng.integers(-4,10)),w0=int(rng.integers(60,120)),w1=int(rng.integers(60,120)),valid=i%13!=0),'minute':i}
   f=s.feed(e);hist.append(e)
   if f is None:continue
   for side in (0,1):
    for lo,hi in WINDOWS:
     prefix=[x for x in hist if x['valid'] and x['phase']==1 and lo<=x['minute']<hi]
     want=e['water'][side]-prefix[0]['water'][side] if lo<=e['minute']<hi else MISSING
     self.assertEqual(f[side][f'window_{lo}_{hi}_water_init'],want)
 def test_restore_window_state(self):
  r={'id':'x','direction':'LIVE_GIVE','conditions':[{'feature':'window_16_31_water_init','op':'ge','value':10}]}
  en=SignalEngine([r],contract=TEST_CONTRACT);en.feed({**event(0,w0=80),'minute':16})
  en=SignalEngine([r],json.loads(canonical(en.snapshot())),contract=TEST_CONTRACT)
  self.assertEqual(len(en.feed({**event(1,w0=90),'minute':20})),1)

class TimedMagnitude(unittest.TestCase):
 def make(self):
  return {'feature':'path3_keep','op':'eq','value':243,'span':30,'pulse':True,'min_line_step':1,'min_water_step':5}
 def test_amplitude_and_threshold_equal(self):
  s=FeatureStream();fs=[]
  for e in [event(0,line=2,w0=95),event(1,line=1,w0=91),event(2,line=1,w0=86),event(3,line=1,w0=91)]:fs.append(s.feed(e)[0])
  self.assertTrue(matches(self.make(),fs[-1]))
  self.assertFalse(matches({**self.make(),'min_water_step':6},fs[-1]))
  self.assertFalse(matches({**self.make(),'min_line_step':2},fs[-1]))
  arr={k:np.array([f[k] for f in fs]) for k in fs[0]}
  self.assertEqual(mask(self.make(),arr).tolist(),[False,False,False,True])
 def test_small_interposed_step_not_skipped(self):
  s=FeatureStream()
  for e in [event(0,line=2,w0=95),event(1,line=1,w0=90),event(2,line=1,w0=89),event(3,line=1,w0=95)]:f=s.feed(e)[0]
  self.assertFalse(matches(self.make(),f))
 def test_joint_change_counts_once(self):
  s=FeatureStream();s.feed(event(0,line=2,w0=95));f=s.feed(event(1,line=1,w0=80))[0]
  self.assertEqual(f['path1_keep'],2);self.assertEqual(f['path2_keep'],MISSING)
 def test_window_elapsed_vs_span(self):
  s=FeatureStream()
  for e in [event(0,line=2,w0=95,ts=0),event(1,line=1,w0=95,ts=1),event(2,line=1,w0=90,ts=20),event(3,line=1,w0=95,ts=31)]:f=s.feed(e)[0]
  self.assertTrue(matches(self.make(),f));self.assertFalse(matches({**self.make(),'span':29},f))
 def test_magnitude_invalid(self):
  r={'id':'x','direction':'LIVE_GIVE','conditions':[self.make()]};validate_rules([r])
  for x in (0,-1,1.5,True):
   q=copy.deepcopy(r);q['conditions'][0]['min_water_step']=x
   with self.assertRaises(ValueError):validate_rules([q])
 def test_path_neighbors_no_retune(self):
  # pulse flip, keep->reset, span -5/+5, line step +1 and water step +0.05; steps below one unit are skipped
  a=self.make();n=neighbors(a,100);self.assertEqual(len(n),6);self.assertEqual(a,self.make())
 def test_reset_clears_magnitude_history(self):
  s=FeatureStream()
  for e in [event(0),event(1,line=1),event(2,line=1,w0=90),event(3,valid=False),event(4,line=1,w0=100)]:f=s.feed(e)
  self.assertEqual(f[0]['pmin_water3_reset'],MISSING)

class ProofAndReview(unittest.TestCase):
 def test_pairwise_nonempty_empty_triple_kept(self):
  arr={'eid':np.arange(3),'mid':np.arange(3),'pnl':np.array([100]*3),'a':np.array([1,1,0]),'b':np.array([0,1,1]),'c':np.array([1,0,1])}
  atoms=[{'feature':f,'op':'eq','value':1} for f in ('a','b','c')];raw=make_plan_spec(atoms,[0,1,2],[0,1,2],'routine')
  ma,nz,off,en,p=pack_masks(arr,atoms)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   sp=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   self.assertIn((0,1,2),list(sp.iter_from()));self.assertFalse(sp.is_proven_zero((0,1,2)))
   self.assertEqual(evaluate(np.array([[0,1,2]],np.int64),ma,nz,off,en,p).tolist(),[[0,0]])
 def test_same_shape_different_masks_rejected(self):
  arr={'eid':np.arange(3),'mid':np.arange(3),'pnl':np.array([100]*3),'a':np.array([1,1,0]),'b':np.array([0,1,1])}
  ats=[{'feature':f,'op':'eq','value':1} for f in ('a','b')];raw=make_plan_spec(ats,[0,1],[0,1],'routine');ma,nz,off,en,p=pack_masks(arr,ats)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False));ma[0,0]=0
   with self.assertRaises(ValueError):prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False)
 def test_proof_coverage_reconciles_all_offsets(self):
  arr={'eid':np.arange(5),'mid':np.arange(5),'pnl':np.ones(5,dtype=int),'x':np.arange(5)};ats=[{'feature':'x','op':'eq','value':v} for v in range(5)]
  raw=make_plan_spec(ats,list(range(5)),list(range(5)),'routine');ma,nz,off,en,p=pack_masks(arr,ats)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   sp=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   for i in range(sp.total+1):
    rows=sp.coverage(i);self.assertEqual(sum(x['evaluated']+x['proven_zero']+x['remaining'] for x in rows),raw['total'])
 def test_review_cache_roundtrip_and_integrity(self):
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   self.assertIsNone(load_review(td,'bind'))
   save_review(td,'bind',{'count':1},rows=[{'id':'a'}],qualified=[],neighbors=[{'net':1}],stress=[])
   self.assertEqual(load_review(td,'bind')['rows'],[{'id':'a'}])
   with self.assertRaises(ValueError):load_review(td,'wrong')
   (Path(td)/'rows.jsonl.gz').write_bytes(b'broken')
   with self.assertRaises(ValueError):load_review(td,'bind')
 def test_window_and_steps_registered_contexts(self):
  ss=FeatureStream();vals=[]
  for i in range(40):vals.append(ss.feed(event(i,line=i%4,w0=80+i%17))[0])
  arrays={k:np.array([f[k] for f in vals]) for k in vals[0]}
  ats,core,tr=dictionary(arrays,'LIVE_GIVE',100,'routine');plan=make_plan_spec(ats,core,tr,'routine')
  wi=next(i for i,a in enumerate(ats) if a['feature']=='window_16_31_line_init')
  ti=next(i for i,a in enumerate(ats) if a.get('min_water_step')==5 and a.get('span')==30)
  self.assertIn(wi,next(b for b in plan['blocks'] if b['id']=='L05_FINE_CONTEXT2')['anchors'])
  self.assertIn(ti,next(b for b in plan['blocks'] if b['id']=='L04_TIMED_CONTEXT2')['anchors'])

if __name__=='__main__':unittest.main()

class SparseMiningIntegration(unittest.TestCase):
 def test_miner_checkpoint_zero_proofs_and_tamper(self):
  from unittest.mock import patch
  from lab.mining import mine_direction
  ev=[];ls={}
  for j in range(24):
   sid=f'm{j:03d}';ls[sid]={'eligible':True,'final':[1,0],'year':2024}
   for q in range(3):
    i=len(ev);ev.append({**event(i,line=2,w0=90+q*5),'sid':sid,'mid':j,'minute':q})
  atoms=[{'feature':'water','op':'eq','value':v} for v in (90,95,100,105)]+[{'feature':'minute','op':'eq','value':q} for q in (0,1,2)]
  def fixed(*args):return atoms,list(range(len(atoms))),list(range(len(atoms)))
  with tempfile.TemporaryDirectory() as td,patch('lab.mining.dictionary',fixed):
   cfg={**DEFAULT,'profile':'routine','chunk_size':8,'max_rules_per_direction':3}
   first=mine_direction(td,ev,ls,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
   self.assertEqual(first['status'],'BUDGET_STOP')
   cfg['max_rules_per_direction']=0
   final=mine_direction(td,ev,ls,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
   self.assertEqual(final['status'],'COMPLETE')
   self.assertEqual(final['next']+final['proven_zero'],final['raw_total'])
   self.assertEqual(final['next'],sum(x['rows'] for x in final['parts']))
   for b in final['local_blocks']:self.assertEqual(b['total'],b['evaluated']+b['proven_zero']+b['remaining'])
   gp=Path(td)/'mining'/'LIVE_GIVE'/'nohit_graph.npy';raw=gp.read_bytes();gp.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
   with self.assertRaises(ValueError):mine_direction(td,ev,ls,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)

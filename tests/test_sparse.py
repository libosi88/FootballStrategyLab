from contextlib import ExitStack
import unittest,tempfile
from pathlib import Path
import numpy as np
from lab.mining import pack_masks,evaluate
from lab.search_plan import make_plan_spec,TaskPlan
from lab.sparse_plan import prepare_sparse_plan
from lab.common import DEFAULT
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT

class SparseTest(unittest.TestCase):
 def test_every_expression_preserved_or_proven_zero(self):
  rng=np.random.default_rng(319)
  arr={'eid':np.arange(360),'mid':np.repeat(np.arange(30),12),'pnl':rng.integers(-200,201,360)}
  for f in ('a','b','c'):arr[f]=rng.integers(0,6,360)
  atoms=[{'feature':f,'op':'eq','value':v} for f in ('a','b','c') for v in range(8)]
  core=list(range(18));raw=make_plan_spec(atoms,core,core,'expanded')
  ma,nz,off,en,p=pack_masks(arr,atoms)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   sp=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   dense=list(TaskPlan(raw).iter_from());sparse=list(sp.iter_from())
   self.assertEqual(len(sparse),sp.total)
   self.assertEqual(len(sparse),len(set(sparse)))
   expected=[]
   for ids in dense:
    c=np.full((1,3),-1,np.int64);c[0,:len(ids)]=ids
    if sp.is_proven_zero(ids):self.assertEqual(evaluate(c,ma,nz,off,en,p).tolist(),[[0,0]])
    else:expected.append(ids)
   self.assertEqual(expected,sparse)
   self.assertEqual(len(dense),len(sparse)+sp.spec['proven_zero'])
   for i in (0,1,10,sp.total//2,sp.total-1,sp.total):self.assertEqual(list(sp.iter_from(i)),sparse[i:])
   re=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   self.assertEqual(list(re.iter_from()),sparse)
 def test_pairwise_compatible_but_empty_triple_is_evaluated(self):
  arr={'eid':np.arange(3),'mid':np.arange(3),'pnl':np.array([100,100,100]),'x':np.array([0,1,2])}
  atoms=[{'feature':'x','op':'range','value':0,'upper':2},{'feature':'x','op':'ge','value':1},{'feature':'x','op':'eq','value':0}]
  ma,nz,off,en,p=pack_masks(arr,atoms)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   raw=make_plan_spec(atoms,[0,1,2],[0,1,2],'routine')
   sp=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   # The second and third masks have no shared event; triple safely elided.
   self.assertTrue(sp.is_proven_zero((0,1,2)))
 def test_parent_payoff_does_not_prune_child(self):
  arr={'eid':np.arange(48),'mid':np.repeat(np.arange(24),2),'pnl':np.tile([-200,190],24),'a':np.ones(48,int),'b':np.tile([0,1],24)}
  atoms=[{'feature':'a','op':'eq','value':1},{'feature':'b','op':'eq','value':1}]
  ma,nz,off,en,p=pack_masks(arr,atoms)
  with tempfile.TemporaryDirectory() as td, ExitStack() as plans:
   raw=make_plan_spec(atoms,[0,1],[0,1],'routine')
   sp=plans.enter_context(prepare_sparse_plan(td,raw,ma,nz,off,DEFAULT,lambda **kw:None,lambda:False))
   self.assertIn((0,1),list(sp.iter_from()))
   self.assertEqual(evaluate(np.array([[0,-1,-1],[0,1,-1]],np.int64),ma,nz,off,en,p).tolist(),[[24,-4800],[24,4560]])

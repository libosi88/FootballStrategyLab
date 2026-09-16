import unittest,tempfile
from pathlib import Path
import numpy as np
from lab.common import *
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.data import inspect,load_inputs
from lab.mining import mine_direction,Paused

class Recovery(unittest.TestCase):
 def test_checkpoint_matches_uninterrupted_and_hash(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);man=inspect([str(ROOT/'demo')],p);ev,la,sc,a=load_inputs(man,'合成演示联赛（非真实比赛）','皇冠')
   cfg={**DEFAULT,'profile':'smoke','chunk_size':100};count=[0]
   def progress(**kw):count[0]+=int('candidates' in kw)
   with self.assertRaises(Paused):mine_direction(p/'paused',ev,la,sc,'LIVE_OVER',cfg,progress,lambda:count[0]>=2)
   one=mine_direction(p/'paused',ev,la,sc,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
   two=mine_direction(p/'whole',ev,la,sc,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
   self.assertEqual(one['next'],two['next']);self.assertEqual(one['candidates'],two['candidates'])
   for x,y in zip(one['parts'],two['parts']):
    with np.load(p/'paused'/'mining'/'LIVE_OVER'/x['file']) as z, np.load(p/'whole'/'mining'/'LIVE_OVER'/y['file']) as q:
     self.assertTrue(np.array_equal(z['metrics'],q['metrics']));self.assertTrue(np.array_equal(z['conditions'],q['conditions']))
   f=p/'paused'/'mining'/'LIVE_OVER'/one['parts'][0]['file'];f.write_bytes(f.read_bytes()+b'tamper')
   with self.assertRaises(ValueError):mine_direction(p/'paused',ev,la,sc,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
if __name__=='__main__':unittest.main()

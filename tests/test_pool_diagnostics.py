import tempfile,unittest,csv
from pathlib import Path
import numpy as np
from lab.common import DEFAULT,read_json,digest
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.pool_diagnostics import all_pool_diagnostics
from lab.mining import Paused
from test_core import event


class PoolDiagnostics(unittest.TestCase):
    def fixture(self):
        labels={s:{'eligible':True} for s in ('a','b','c')}
        # Risk availability uses the real evaluable-quote contract, including line.
        events=[{**event(i,line=8,ts=i),'sid':s,'market':0} for i,s in enumerate(labels)]
        trades=[{'sid':s,'market':0,'phase':1,'ts':i,'side':0,'line':8,'water':95,'score':[0,0],'pnl':v} for i,(s,v) in enumerate(zip(labels,(190,-200,190)))]
        population=[{'id':f'r{i}','direction':'LIVE_OVER','signature':str(i),'_trade_ref':{'sha256':digest(trades[:i+1])},'_trades':[trades[:i+1]]} for i in range(3)]
        return population,events,labels
    def test_pairwise_pause_resume_matches_fresh_and_detects_missing_matrix(self):
        population,events,labels=self.fixture();config={**DEFAULT,'min_free_disk_mb':0};calls=0
        def pause():
            nonlocal calls
            calls+=1;return calls==5
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a';b=Path(td)/'b'
            with self.assertRaises(Paused):all_pool_diagnostics(a,population,events,labels,config,lambda **kw:None,pause)
            self.assertEqual(read_json(a/'pair_state.json')['next_row'],1)
            result=all_pool_diagnostics(a,population,events,labels,config,lambda **kw:None,lambda:False)
            all_pool_diagnostics(b,population,events,labels,config,lambda **kw:None,lambda:False)
            self.assertEqual(result['pairs_evaluated'],3)
            self.assertEqual((a/'全池重叠与共同亏损.csv').read_bytes(),(b/'全池重叠与共同亏损.csv').read_bytes())
            (a/'pnl.npy').unlink()
            with self.assertRaisesRegex(ValueError,'缺失'):all_pool_diagnostics(a,population,events,labels,config,lambda **kw:None,lambda:False)
    def test_shape_and_budget_are_not_silently_completed(self):
        population,events,labels=self.fixture();config={**DEFAULT,'max_pool_pairs':1,'min_free_disk_mb':0}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            result=all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)
            self.assertEqual(result['status'],'BUDGET_STOP');self.assertFalse(result['all_pool_pairwise_done'])
            np.save(root/'pnl.npy',np.zeros((1,1),np.int64))
            with self.assertRaisesRegex(ValueError,'形状'):all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)


if __name__=='__main__':unittest.main()

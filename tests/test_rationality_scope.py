import tempfile
import unittest
from pathlib import Path
from lab.common import check_config
from lab.standard_spec import balanced_atoms,compact_atoms,max_conditions_for,spec_version_for
from lab.standard_mining import mine_standard
from lab.historical_pricing import pregoal_rejected_quotes,minute_close_payoffs
from lab.selection import Quotes,PREGOAL_REJECTION_POLICY
from lab.standard_review import pregoal_disclosure
from test_core import event


class ScopeAndProxy(unittest.TestCase):
    def test_middle_scope_contains_mandatory_moves_and_all_projected_paths(self):
        domain=lambda name:(-200,200);values=lambda name:set(range(1,445))
        pairs=list(balanced_atoms('LIVE_GIVE',100,domain,values));atoms=[a for a,_ in pairs]
        for step in (-15,-20):self.assertIn({'feature':'samewater','op':'le','value':step},atoms)
        for mode in ('keep','reset'):
            for pulse in (False,True):
                self.assertIn({'feature':'path3_'+mode,'op':'eq','value':134,'pulse':pulse},atoms)
        self.assertFalse(any('span' in a for a in atoms))
        self.assertEqual(max_conditions_for({'search_grammar':'balanced_v1'}),3)
        self.assertEqual(spec_version_for({'search_grammar':'balanced_v1'}),'FSL_STANDARD_BALANCED_V1')
        self.assertTrue(all(a in atoms for a in compact_atoms('LIVE_GIVE',100,domain,values)))

    def test_balanced_real_dictionary_and_global_bound_accounting(self):
        cfg=check_config({'search_grammar':'balanced_v1','directions':['LIVE_GIVE']})
        events=[event(0,ts=1),event(1,ts=2,w0=80)]
        labels={'a':{'eligible':True,'date':'2024-01-01','year':2024,'kickoff':0,'final':[2,1]}}
        with tempfile.TemporaryDirectory() as td:
            result=mine_standard(Path(td),events,labels,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
            self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['remaining'],0)
            self.assertGreater(result['raw_total'],100)
            self.assertEqual(result['raw_total'],result['representatives']+result['proven_nonprofitable'])

    def test_quality_invalid_is_not_evidence_of_explicit_closure(self):
        a=event(0,ts=1)
        b={**event(1,ts=2,score0=(1,0)),'valid':False,'closed':False,'quality_blocked':True}
        labels={'a':{'eligible':True,'final':[2,1],'year':2024,'kickoff':0,'date':'2024-01-01'}}
        quotes=Quotes([a,b],labels,100,5,minute_close=True)
        self.assertFalse(quotes.pregoal_rejected(0,1));self.assertNotIn(0,pregoal_rejected_quotes([a,b]))
        self.assertIsNotNone(quotes.trade(0,0,reject_pregoal=True))

    def test_explicit_closure_proxy_and_fast_payoffs_agree(self):
        a=event(0,ts=1);b=event(1,ts=2,valid=False,score0=(1,0))
        labels={'a':{'eligible':True,'final':[2,1],'year':2024,'kickoff':0,'date':'2024-01-01'}}
        quotes=Quotes([a,b],labels,100,5,minute_close=True)
        self.assertTrue(quotes.pregoal_rejected(0,1));self.assertIn(0,pregoal_rejected_quotes([a,b]))
        self.assertIsNone(quotes.trade(0,0,reject_pregoal=True))
        self.assertEqual(minute_close_payoffs([a,b],labels,100)[0,0],0)
        base=[quotes.trade(0,0)];disclosed=pregoal_disclosure(base,[],100,[2024])
        self.assertEqual(disclosed['removed_winners'],1)
        self.assertTrue(disclosed['assumed_rejections_not_confirmed'])
        self.assertIn('v2',PREGOAL_REJECTION_POLICY)


if __name__=='__main__':unittest.main()

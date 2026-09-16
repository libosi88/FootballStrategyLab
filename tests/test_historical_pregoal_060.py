"""Historical facts exclude fills the pre-goal rejection rule rejects: zero payoff, never replaced."""
import tempfile,unittest
from pathlib import Path
from lab.common import DEFAULT,VALIDATION_DEFAULT,check_config
from lab.historical_pricing import minute_close_payoffs,pregoal_rejected_quotes
from lab.selection import Quotes
from lab.research_standard import decision_metrics
from lab.portfolio_search import PortfolioSearch
from test_core import event

def goal_fixture(phase=1):
    # Match a: an over quote at minute 10; in minute 11 the market closes and reopens after a goal.
    # Match b: the same quote with no goal afterwards.
    rows=[{**event(0,line=8,ts=10),'market':0},
          {**event(1,line=8,ts=11,valid=False),'market':0},
          {**event(2,line=12,ts=11,score0=(1,0)),'market':0},
          {**event(3,line=8,ts=10),'sid':'b','mid':1,'market':0}]
    rows=[{**r,'phase':phase} for r in rows]
    labels={'a':{'eligible':True,'date':'2024-01-01','year':2024,'kickoff':0,'final':[3,0]},
            'b':{'eligible':True,'date':'2025-01-01','year':2025,'kickoff':0,'final':[3,0]}}
    return rows,labels

class HistoricalPregoalRejection(unittest.TestCase):
    def test_payoffs_match_the_quote_reference_with_pregoal_rejection(self):
        events,labels=goal_fixture()
        self.assertEqual(pregoal_rejected_quotes(events),{0})
        payoffs=minute_close_payoffs(events,labels,100);quotes=Quotes(events,labels,100,5,minute_close=True)
        for e in events:
            for side in (0,1):
                trade=quotes.trade(e['eid'],side,reject_pregoal=True)
                self.assertEqual(int(payoffs[e['eid'],side]),trade['pnl'] if trade else 0)
        self.assertEqual(int(payoffs[0,0]),0);self.assertGreater(int(payoffs[3,0]),0)
        self.assertGreater(int(minute_close_payoffs(events,labels,100,reject_pregoal=False)[0,0]),0)

    def test_prematch_quotes_and_closures_without_a_new_score_are_not_rejected(self):
        events,_=goal_fixture(phase=0)
        self.assertEqual(pregoal_rejected_quotes(events),set())
        events,_=goal_fixture()
        events[2]={**events[2],'score':[0,0]}
        self.assertEqual(pregoal_rejected_quotes(events),set())

    def test_profit_only_from_a_pregoal_fill_is_not_in_the_historical_pool(self):
        from lab.standard_mining import mine_standard
        from lab.standard_search import candidate_families
        events,labels=goal_fixture()
        cfg=check_config({'directions':['LIVE_OVER'],'standard_node_budget':1,'min_profit':'0'})
        clean=int(minute_close_payoffs(events,labels,100)[3,0])
        inflated=clean+int(minute_close_payoffs(events,labels,100,reject_pregoal=False)[0,0])
        with tempfile.TemporaryDirectory() as td:
            state=mine_standard(Path(td),events,labels,100,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
            nets=[f['net'] for f in candidate_families(Path(td)/'mining/LIVE_OVER',0)]
        # Match a's pre-goal fill adds nothing; only match b's executable fill is profitable.
        self.assertGreater(state['candidates'],0);self.assertIn(clean,nets)
        self.assertTrue(all(net<inflated for net in nets))

    def test_completed_global_nonprofit_proof_has_an_empty_candidate_iterator(self):
        from lab.standard_mining import mine_standard
        from lab.standard_search import candidate_families
        events,labels=goal_fixture();cfg=check_config({'directions':['LIVE_OVER'],'standard_node_budget':1})
        with tempfile.TemporaryDirectory() as td:
            state=mine_standard(Path(td),events,labels,100,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
            self.assertEqual(state['search_backend'],'standard_global_bound')
            self.assertEqual(list(candidate_families(Path(td)/'mining/LIVE_OVER',0)),[])

    def test_historical_portfolio_decision_uses_the_rejection_scenario(self):
        raw={'net_i':1000,'drawdown_match_i':100,'matches':50};rejected={**raw,'net_i':-500,'drawdown_match_i':600}
        score={'raw':raw,'stress':[raw,raw,raw,rejected],'historical':rejected}
        self.assertEqual(decision_metrics(DEFAULT,score),[rejected])
        self.assertEqual(decision_metrics(VALIDATION_DEFAULT,score),[raw,raw,raw,raw,rejected])
        rule={'id':'r','conditions':[],'_trades':[]}
        with tempfile.TemporaryDirectory() as td:
            result=PortfolioSearch([rule],lambda rs:score,DEFAULT,100,Path(td)/'h','h','2').score(('r',))
        self.assertFalse(result['feasible']);self.assertEqual(result['min_profit_i'],-500)

if __name__=='__main__':unittest.main()

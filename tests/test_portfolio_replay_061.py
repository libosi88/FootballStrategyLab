"""Historical portfolio search: match-by-match incremental replay equals the full order replay."""
import random,tempfile,unittest
from pathlib import Path
from lab.common import DEFAULT
from lab.portfolio_replay import MatchReplayScorer,ReplayUnsupported
from lab.portfolio_search import PortfolioSearch
from lab.selection import dispatch,portfolio_metrics

DIRECTIONS=('LIVE_HOME','LIVE_PK_HOME','LIVE_GIVE','LIVE_OVER','LIVE_UNDER','PRE_OVER','PRE_RECEIVE')

def synthetic_pool(seed,rules=14,matches=9):
    """Few matches with repeated minutes, quotes and lines, so slots, duplicate contracts, opposite sides and caps collide."""
    rng=random.Random(seed);pool=[]
    for i,priority in enumerate(rng.sample(range(10*rules),rules)):
        direction=rng.choice(DIRECTIONS);market=0 if direction.endswith(('OVER','UNDER')) else 1;trades=[]
        for sid in rng.sample(range(matches),rng.randint(1,matches)):
            for _ in range(rng.randint(1,3)):
                ts=100*sid+rng.choice((5,5,10,20,20,30))
                trades.append({'sid':f'm{sid}','eid':ts*4+rng.randint(0,2),'ts':ts,'quote_ts':ts-rng.randint(0,1),'market':market,
                               'phase':0 if direction.startswith('PRE') else 1,'side':rng.randint(0,1),'line':rng.choice((0,1,-1,2)),
                               'water':rng.choice((80,90)),'score':rng.choice(([0,0],[1,0])),'pnl':rng.choice((-200,-100,0,80,160,180)),
                               # Kickoff order differs from sid order.
                               'sort_time':100*sid-50*(sid%2),'year':2024,'segment':'s'})
        pool.append({'id':f'r{i:02d}','direction':direction,'conditions':[],'_execution_priority':priority,'_trades':[[],[],[],[],trades]})
    return pool

def full_replay(rules,cap):
    m=portfolio_metrics(dispatch(rules,4,cap,priority_mode='frozen_rule_priority')[0],100,[2024])
    return m['net_i'],m['drawdown_match_i'],m['matches']

class MatchReplay061(unittest.TestCase):
    def test_every_neighbour_and_distant_roster_equals_the_full_replay(self):
        for seed in range(30):
            pool=synthetic_pool(seed);cap=(1,2,4)[seed%3];byid={r['id']:r for r in pool};ids=sorted(byid)
            scorer=MatchReplayScorer(pool,4,cap);rng=random.Random(1000+seed)
            for _ in range(8):
                base=rng.sample(ids,rng.randint(0,len(ids)));scorer.rebase(base)
                for _ in range(20):
                    target=set(base);kind=rng.choice(('same','add','remove','replace','distant'))
                    outside=[i for i in ids if i not in target]
                    if kind in ('add','replace') and outside:target.add(rng.choice(outside))
                    if kind in ('remove','replace') and base:target.discard(rng.choice(base))
                    if kind=='distant':target=set(rng.sample(ids,rng.randint(0,len(ids))))
                    roster=[byid[i] for i in sorted(target)];got=scorer(roster)['historical']
                    with self.subTest(seed=seed,kind=kind,roster=sorted(target)):
                        self.assertEqual((got['net_i'],got['drawdown_match_i'],got['matches']),full_replay(roster,cap))

    def test_search_results_and_journals_are_identical_to_full_replays(self):
        pool=synthetic_pool(11,rules=9,matches=7);config={**DEFAULT,'selection_checkpoint_every':16}
        def full(rules):
            return {'raw':None,'stress':[],'historical':portfolio_metrics(dispatch(rules,4,2,priority_mode='frozen_rule_priority')[0],100,[2024])}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            slow=PortfolioSearch(pool,full,config,100,root/'full','T','2').run()
            fast=PortfolioSearch(pool,MatchReplayScorer(pool,4,2),config,100,root/'fast','T','2').run()
            self.assertEqual((slow['replay_method'],fast['replay_method']),('dispatch_full_replay',MatchReplayScorer.method))
            self.assertEqual({k:v for k,v in slow.items() if k!='replay_method'},{k:v for k,v in fast.items() if k!='replay_method'})
            self.assertGreater(sum(p['evaluated'] for p in slow['paths']),0)
            journals=sorted((root/'full').glob('*.jsonl'));self.assertTrue(journals)
            for f in journals:self.assertEqual(f.read_bytes(),(root/'fast'/f.name).read_bytes())

    def test_pools_it_cannot_represent_are_refused(self):
        pool=synthetic_pool(3,rules=3,matches=3)
        with self.assertRaises(ReplayUnsupported):MatchReplayScorer([{**pool[0],'_execution_priority':None}],4,4)
        first=dict(pool[0]['_trades'][4][0])
        with self.assertRaises(ReplayUnsupported):MatchReplayScorer([{**pool[0],'_trades':[[],[],[],[],[first,{**first,'eid':first['eid']+1,'sort_time':first['sort_time']+1}]]}],4,4)
        with self.assertRaises(ReplayUnsupported):MatchReplayScorer(pool,4,4,max_offers=1)

if __name__=='__main__':unittest.main()

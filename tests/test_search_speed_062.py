"""0.6.2 portfolio search speedups are exact: cached neighbourhoods, visited rosters, ratio parsing and cap shortcuts."""
import random,tempfile,unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from lab.common import DEFAULT
from lab.mining import Paused
from lab.portfolio_replay import MatchReplayScorer
from lab.portfolio_search import MoveList,PortfolioSearch,move_count
from lab.research_standard import fraction,portfolio_feasible
from test_portfolio_replay_061 import synthetic_pool,full_replay

def original_move_at(pool,chosen,index):
    # The 0.6.1 implementation, which sorted the pool again for every move.
    ids=sorted(chosen);unselected=sorted(set(pool)-set(ids));u=len(unselected);n=len(ids)
    if not 0<=index<u+n+n*u:raise IndexError('组合动作游标越界')
    if index<u:
        add=unselected[index];return 'ADD',add,None,tuple(sorted(ids+[add]))
    index-=u
    if index<n:
        remove=ids[index];return 'REMOVE',None,remove,tuple(i for i in ids if i!=remove)
    index-=n;ri,ai=divmod(index,u)
    add,remove=unselected[ai],ids[ri]
    return 'REPLACE',add,remove,tuple(sorted([i for i in ids if i!=remove]+[add]))

class SearchSpeed062(unittest.TestCase):
    def test_cached_neighbourhood_lists_the_same_moves_in_the_same_order(self):
        rng=random.Random(62)
        for _ in range(60):
            pool=[f'r{i:03d}' for i in rng.sample(range(500),rng.randint(1,25))]
            chosen=rng.sample(pool,rng.randint(0,len(pool)))
            moves=MoveList(pool,chosen);self.assertEqual(moves.total,move_count(pool,chosen))
            self.assertEqual([moves.at(i) for i in range(moves.total)],[original_move_at(pool,chosen,i) for i in range(moves.total)])
            with self.assertRaises(IndexError):moves.at(moves.total)

    def test_cached_ratio_parsing_is_exact(self):
        for value in ('2','2.5','3',2,Decimal('1.75'),'0.333'):self.assertEqual(fraction(value),Fraction(str(value)))
        self.assertTrue(portfolio_feasible(400,200,'2'));self.assertFalse(portfolio_feasible(399,200,'2'));self.assertTrue(portfolio_feasible(0,0,'3',empty=True))

    def test_capped_replacements_match_the_full_replay(self):
        # Cap 1 and 2 close most matches early, so both replacement shortcuts are taken often.
        for seed in range(40):
            pool=synthetic_pool(100+seed,rules=12,matches=6);cap=1+seed%2;byid={r['id']:r for r in pool};ids=sorted(byid)
            scorer=MatchReplayScorer(pool,4,cap);rng=random.Random(seed)
            for _ in range(6):
                base=rng.sample(ids,rng.randint(1,len(ids)-1));scorer.rebase(base);outside=[i for i in ids if i not in base]
                for removed in base:
                    for added in outside:
                        roster=[byid[i] for i in sorted(set(base)-{removed}|{added})];got=scorer(roster)['historical']
                        self.assertEqual((got['net_i'],got['drawdown_match_i'],got['matches']),full_replay(roster,cap))

    def test_paused_and_resumed_search_equals_a_whole_run(self):
        # This eight-match fixture tests old search/resume mechanics rather than multi-year gates.
        pool=synthetic_pool(5,rules=10,matches=8);config={**DEFAULT,'selection_checkpoint_every':7,'portfolio_segment_checks':False}
        with tempfile.TemporaryDirectory() as td:
            whole=PortfolioSearch(pool,MatchReplayScorer(pool,4,2),config,100,Path(td)/'whole','T','2').run()
            calls=[0];pauses=0
            def pause():
                calls[0]+=1;return calls[0] in (40,95,160,240)
            while True:
                try:
                    resumed=PortfolioSearch(pool,MatchReplayScorer(pool,4,2),config,100,Path(td)/'paused','T','2',should_pause=pause).run();break
                except Paused:pauses+=1
            self.assertGreater(pauses,1)
            self.assertEqual({k:v for k,v in whole.items() if k!='score_computations'},{k:v for k,v in resumed.items() if k!='score_computations'})
            journals=sorted((Path(td)/'whole').glob('*.jsonl'));self.assertTrue(journals)
            for f in journals:self.assertEqual(f.read_bytes(),(Path(td)/'paused'/f.name).read_bytes())

if __name__=='__main__':unittest.main()

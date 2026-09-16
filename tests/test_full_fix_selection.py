"""Locks in five just-made research fixes.

C1-1 替代池按整份名单的逐笔签名去重（不只是同ID）；C1-2 备选池排名依据按研究目标分文案；
C2-1 历史覆盖跨度按自然日计；C2-4 全池预算停止与按方向可观察；C1-3 压力/拒单质量旗标只在validation拦截。
"""
import csv,tempfile,unittest
from pathlib import Path
import numpy as np
from lab.common import DEFAULT,VALIDATION_DEFAULT,digest,read_json
from lab.selection import PREGOAL_REJECTION_POLICY,Quotes,build_alternative_comparisons,metrics,portfolio_metrics
from lab.pool_diagnostics import all_pool_diagnostics
from lab.execution_economics import verify_economics
from test_core import event

NO_ALTERNATIVE='当前可比池无其他同方向代表（受样本及比例带限制）；不是全空间没有替代'
STRESS_QUALITY_REASON='压力或拒单情景报价存在已知比分质量旗标'


def candidate(name,signature,net=3000,n=100,streak=2,drawdown=200):
    """One qualified historical representative: id and per-trade signature vary independently."""
    return {'id':name,'direction':'LIVE_GIVE','signature':signature,'conditions':[],
            'metrics':{'n':n,'net_i':net,'year_counts':{'2024':n},'streak':streak,'drawdown_i':drawdown,'drawdown':drawdown/200},
            'stress':[{'net_i':net}]*3,'_trades':[[{'sid':name}]]}


def scorer(rules):
    m={'net_i':5000,'drawdown_match_i':100,'matches':80}
    return {'raw':m,'stress':[m]*3,'historical':m}


def pool_fixture():
    """Two directions observable on different matches, so availability cannot be one shared union."""
    labels={s:{'eligible':True} for s in ('a','b')}
    events=[{**event(0,line=8,ts=0),'sid':'a','market':0},{**event(1,line=8,ts=1),'sid':'b','market':1}]
    trade=lambda sid,market,pnl:{'sid':sid,'market':market,'phase':1,'ts':0,'side':0,'line':8,'water':95,'score':[0,0],'pnl':pnl}
    population=[{'id':'over','direction':'LIVE_OVER','signature':'s0','_trade_ref':{'sha256':digest('over')},
                 '_trades':[[trade('a',0,190)],[],[],[],[trade('a',0,-200)]]},
                {'id':'give','direction':'LIVE_GIVE','signature':'s1','_trade_ref':{'sha256':digest('give')},
                 '_trades':[[trade('b',1,190)],[],[],[],[trade('b',1,190)]]}]
    return population,events,labels


def budget_fixture():
    labels={s:{'eligible':True} for s in ('a','b','c')}
    events=[{**event(i,line=8,ts=i),'sid':s,'market':0} for i,s in enumerate(labels)]
    trades=[{'sid':s,'market':0,'phase':1,'ts':i,'side':0,'line':8,'water':95,'score':[0,0],'pnl':v}
            for i,(s,v) in enumerate(zip(labels,(190,-200,190)))]
    population=[{'id':f'r{i}','direction':'LIVE_OVER','signature':str(i),'_trade_ref':{'sha256':digest(trades[:i+1])},
                 '_trades':[trades[:i+1]]} for i in range(3)]
    return population,events,labels


def quality_fixture(matches=45):
    """Minute-close live quotes: the trigger minute is clean, the delayed stress state shows a score
    above the uploaded final result, so only stress fills carry the known quality flag."""
    rows=[];labels={}
    for mid in range(matches):
        sid=f'q-{mid:03d}';start=mid*10
        labels[sid]={'eligible':True,'final':[1,0],'year':2024,'date':'2024-01-01','kickoff':start}
        for offset,score0 in ((0,(0,0)),(1,(2,0)),(2,(2,0))):
            e=event(len(rows),line=10,w0=160,ts=start+offset,score0=score0)
            e.update(sid=sid,mid=mid,market=0);rows.append(e)
    return rows,labels


class FullFixSelection(unittest.TestCase):
    # ---- C1-1 -------------------------------------------------------------------------------------
    def test_alternative_pool_excludes_twins_of_any_roster_signature_not_only_same_id(self):
        a=candidate('a','SIG_A');b=candidate('b','SIG_B')
        twin=candidate('twin_of_b','SIG_B')  # different id, identical per-trade history to roster member b
        other=candidate('other','SIG_X')
        rows=build_alternative_comparisons([('默认',[a,b])],[twin,other],VALIDATION_DEFAULT,100,scorer)
        chosen={r.get('替代ID') for r in rows if r.get('替代ID')}
        self.assertNotIn('twin_of_b',chosen)
        self.assertEqual(chosen,{'other'})
        # Without the genuinely different candidate the twin cannot stand in for anyone on the roster.
        only_twin=build_alternative_comparisons([('默认',[a,b])],[twin],VALIDATION_DEFAULT,100,scorer)
        self.assertEqual(len(only_twin),2)
        self.assertEqual({r['备注'] for r in only_twin},{NO_ALTERNATIVE})
        self.assertFalse(any('替代ID' in r for r in only_twin))
        # The twin is excluded by signature alone: the same rule under a fresh signature is comparable.
        renamed=build_alternative_comparisons([('默认',[a,b])],[{**twin,'signature':'SIG_FRESH'}],VALIDATION_DEFAULT,100,scorer)
        self.assertEqual({r.get('替代ID') for r in renamed},{'twin_of_b'})
        # An absent signature is unknown, not a match, so a pool without signatures stays comparable.
        unsigned={k:v for k,v in other.items() if k!='signature'}
        self.assertEqual({r.get('替代ID') for r in build_alternative_comparisons([('默认',[a,b])],[unsigned],VALIDATION_DEFAULT,100,scorer)},{'other'})

    # ---- C1-2 -------------------------------------------------------------------------------------
    def test_alternative_pool_ranking_text_differs_by_research_objective(self):
        current=candidate('current','SIG_C',net=2000);alternative=candidate('alt','SIG_D',net=3000)
        historical='同方向同样本门槛备选中历史净胜最高';validation='同方向同样本门槛备选中最差压力净胜最高'
        for config,metric,expected,banned,column in ((DEFAULT,'历史净胜',historical,validation,'替换后组合历史净胜'),
                                                     (VALIDATION_DEFAULT,'最差压力净胜',validation,historical,'替换后组合最差压力净胜')):
            with self.subTest(objective=config['research_objective']):
                rows=build_alternative_comparisons([('默认',[current])],[alternative],config,100,scorer)
                row=next(r for r in rows if r['比较指标']==metric)
                self.assertEqual(row['备选池排名依据'],expected)
                self.assertNotIn(banned,{r['备选池排名依据'] for r in rows})
                self.assertIn(column,row)

    # ---- C2-1 -------------------------------------------------------------------------------------
    def test_history_days_counts_calendar_days_not_minutes_or_rows(self):
        def trade(index,sort_time):return {'sid':str(index),'ts':index,'eid':index,'pnl':190,'year':2024,'sort_time':sort_time}
        # Day 1 at 23:50 and day 3 at 00:10: 1460 wall minutes, but three distinct match days apart by two.
        across=[trade(0,1*1440+23*60+50),trade(1,3*1440+10)]
        result=metrics(across,100,[2024])
        self.assertEqual(result['history_days'],2)
        self.assertEqual(result['n'],2)
        self.assertNotEqual(result['history_days'],(across[1]['sort_time']-across[0]['sort_time'])//1440)
        # Same match day, two rows: a row count or a minute span would not be zero.
        same_day=[trade(0,1*1440+10),trade(1,1*1440+23*60+50)]
        self.assertEqual(metrics(same_day,100,[2024])['history_days'],0)
        self.assertEqual(metrics(same_day,100,[2024])['n'],2)
        self.assertEqual(metrics([],100,[2024])['history_days'],0)
        # Unordered input still measures first to last traded match day.
        self.assertEqual(metrics(list(reversed(across)),100,[2024])['history_days'],2)

    # ---- C2-4 -------------------------------------------------------------------------------------
    def test_all_pool_budget_stop_is_partial_and_committed_state_is_reusable(self):
        population,events,labels=budget_fixture()
        config={**VALIDATION_DEFAULT,'max_pool_pairs':1,'min_free_disk_mb':0}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'budget';complete_root=Path(td)/'complete'
            stopped=all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)
            self.assertEqual(stopped['status'],'BUDGET_STOP')
            self.assertFalse(stopped['all_pool_pairwise_done'])
            self.assertLess(stopped['pairs_evaluated'],stopped['pairs'])
            self.assertEqual(read_json(root/'summary.json')['status'],'BUDGET_STOP')
            state=read_json(root/'pair_state.json');partial=(root/'全池重叠与共同亏损.csv').read_bytes()
            # A second identical call re-verifies the committed matrix and CSV prefix instead of rebuilding.
            again=all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)
            self.assertEqual(again['status'],'BUDGET_STOP')
            self.assertEqual(again['pairs_evaluated'],stopped['pairs_evaluated'])
            self.assertEqual(read_json(root/'pair_state.json')['next_row'],state['next_row'])
            self.assertEqual((root/'全池重叠与共同亏损.csv').read_bytes(),partial)
            # The budgeted output is an exact prefix of the unbudgeted result; no pair is silently dropped.
            done=all_pool_diagnostics(complete_root,population,events,labels,{**config,'max_pool_pairs':0},lambda **kw:None,lambda:False)
            self.assertEqual(done['status'],'COMPLETE')
            self.assertTrue(done['all_pool_pairwise_done'])
            self.assertEqual(done['pairs_evaluated'],done['pairs'])
            self.assertEqual(done['pairs_with_shared_matches']+done['zero_overlap_pairs'],done['pairs'])
            self.assertTrue((complete_root/'全池重叠与共同亏损.csv').read_bytes().startswith(partial))

    def test_all_pool_budget_stop_resumes_when_the_budget_is_raised(self):
        population,events,labels=budget_fixture()
        config={**VALIDATION_DEFAULT,'max_pool_pairs':1,'min_free_disk_mb':0}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            stopped=all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)
            self.assertEqual(stopped['status'],'BUDGET_STOP')
            self.assertFalse(stopped['all_pool_pairwise_done'])
            partial=(root/'全池重叠与共同亏损.csv').read_bytes()
            # A pure budget knob gates how much is computed, so raising it resumes the committed matrix.
            resumed=all_pool_diagnostics(root,population,events,labels,{**config,'max_pool_pairs':10},lambda **kw:None,lambda:False)
            self.assertEqual(resumed['status'],'COMPLETE')
            self.assertTrue(resumed['all_pool_pairwise_done'])
            self.assertEqual(resumed['pairs_evaluated'],resumed['pairs'])
            self.assertEqual(resumed['pairs_with_shared_matches']+resumed['zero_overlap_pairs'],resumed['pairs'])
            self.assertGreater(resumed['pairs_evaluated'],stopped['pairs_evaluated'])
            self.assertEqual(read_json(root/'summary.json')['status'],'COMPLETE')
            # Unlimited afterwards is idempotent and never rewrites a committed row.
            unlimited=all_pool_diagnostics(root,population,events,labels,{**config,'max_pool_pairs':0},lambda **kw:None,lambda:False)
            self.assertEqual(unlimited['status'],'COMPLETE')
            self.assertTrue(unlimited['all_pool_pairwise_done'])
            self.assertEqual(unlimited['pairs_evaluated'],unlimited['pairs'])
            self.assertTrue((root/'全池重叠与共同亏损.csv').read_bytes().startswith(partial))

    def test_real_scope_changes_still_invalidate_the_committed_pool_matrix(self):
        population,events,labels=budget_fixture()
        config={**VALIDATION_DEFAULT,'max_pool_pairs':1,'min_free_disk_mb':0}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            all_pool_diagnostics(root,population,events,labels,config,lambda **kw:None,lambda:False)
            for change,args in (('研究门槛',(population,events,labels,{**config,'min_profit':'20'})),
                                ('研究目标',(population,events,labels,{**DEFAULT,'max_pool_pairs':1,'min_free_disk_mb':0})),
                                ('比赛范围',(population,events,{k:v for k,v in labels.items() if k!='c'},config)),
                                ('可观察报价',(population,events[1:],labels,config))):
                with self.subTest(change=change),self.assertRaisesRegex(ValueError,'全池风险矩阵范围改变'):
                    all_pool_diagnostics(root,*args,lambda **kw:None,lambda:False)

    def test_all_pool_records_trade_basis_and_availability_per_direction(self):
        population,events,labels=pool_fixture()
        for config,basis,pnl in ((VALIDATION_DEFAULT,'minute_close_scenario_0',190),(DEFAULT,'historical_pregoal_rejected_scenario_4',-200)):
            with self.subTest(objective=config['research_objective']),tempfile.TemporaryDirectory() as td:
                root=Path(td)
                result=all_pool_diagnostics(root,population,events,labels,{**config,'min_free_disk_mb':0},lambda **kw:None,lambda:False)
                self.assertEqual(result['trade_basis'],basis)
                self.assertEqual(result['availability'],'per_direction_evaluable_trigger_quotes')
                self.assertEqual(read_json(root/'summary.json')['trade_basis'],basis)
                # Sids are sorted: column 0 is match a (only LIVE_OVER observable), column 1 is match b (only LIVE_GIVE).
                available=np.load(root/'available.npy')
                self.assertEqual(available.tolist(),[[True,False],[False,True]])
                self.assertEqual(int(np.load(root/'pnl.npy')[0,0]),pnl)

    # ---- C1-3 -------------------------------------------------------------------------------------
    def test_known_quality_flag_on_stress_fill_is_a_validation_only_economic_reason(self):
        rows,labels=quality_fixture();quotes=Quotes(rows,labels,100,5,minute_close=True)
        eids=range(0,len(rows),3);sides=[0]*len(labels)
        major=[quotes.trades(eids,sides,max(0,i-1),0 if i==0 else 5) for i in range(4)]
        kept=quotes.trades(eids,sides,0,0,reject_pregoal=True)
        # The fixture flags a stress fill only: the trigger minute and the pre-goal-rejected fill stay clean.
        self.assertEqual([sum(t['quality'] for t in t_) for t_ in major],[0,0,len(labels),len(labels)])
        self.assertEqual(sum(t['quality'] for t in kept),0)
        self.assertTrue(kept)
        scenarios={str(i):{'metrics':portfolio_metrics(major[i],100,[2024])} for i in range(4)}
        packet={'rules':[{'id':'one','direction':'LIVE_OVER'}],'water_scale':100,
                'execution_policy':{'quote_mapping':'minute_close_latest_v3','pregoal_rejection':PREGOAL_REJECTION_POLICY,
                                    'match_cap':4,'priority':'strategy_id_lexical'}}
        signals={'one':[{'eid':i,'side':0} for i in eids]}
        report=lambda pkt,config:verify_economics(pkt,signals,quotes,labels,config,{'scenarios':scenarios})
        blocked=report(packet,VALIDATION_DEFAULT)
        self.assertIn(STRESS_QUALITY_REASON,blocked['rules'][0]['reasons'])
        self.assertEqual(blocked['rules'][0]['status'],'FAIL')
        allowed=report(packet,DEFAULT)
        self.assertNotIn(STRESS_QUALITY_REASON,allowed['rules'][0]['reasons'])
        self.assertFalse([r for r in allowed['rules'][0]['reasons'] if '质量旗标' in r])
        # The extra gate follows the research objective, not the packet: a historical run never adds it, even
        # on a legacy packet without the pre-goal policy. Quality on the decided basis stays gated by
        # research_gate_reasons ('存在已知比分质量旗标'), so nothing is left ungated.
        legacy={**packet,'execution_policy':{'quote_mapping':'minute_close_latest_v3'}}
        self.assertFalse([r for r in report(legacy,DEFAULT)['rules'][0]['reasons'] if '质量旗标' in r])
        self.assertIn(STRESS_QUALITY_REASON,report(legacy,VALIDATION_DEFAULT)['rules'][0]['reasons'])


if __name__=='__main__':unittest.main()

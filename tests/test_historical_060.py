"""Historical objectives, whole-history preservation and offline league assignment."""
import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT,VALIDATION_DEFAULT,check_config,atomic_json,read_json,sha,digest
from lab.holdout import split_holdout
from lab.research_standard import research_gate_reasons
from lab.selection import metrics
from lab.portfolio_search import PortfolioSearch
from lab.workflow_gates import completion_state
from lab.fleet import make_plans,import_plan
from lab.store import Store
from test_core import event

class HistoricalPolicy(unittest.TestCase):
    def test_profitable_family_report_uses_the_same_price_basis_as_discovery(self):
        from lab.standard_review import family_report
        family={'direction':'LIVE_OVER','family_id':1,'expression_occurrences':3,'metrics':{'net':-0.9,'n':2},
                'execution_metrics':{'net':1.0,'n':2},'status':'BASIC_FAILED','reasons':['样本不足'],'tags':[]}
        row=family_report(family,DEFAULT)
        self.assertEqual(row['净胜'],1.0);self.assertEqual(row['触发瞬间净胜对照'],-0.9)
        self.assertEqual(row['收益口径'],'分钟末历史净胜（剔除进球前拒单）')

    def test_minute_close_profit_is_not_lost_when_instant_profit_is_negative(self):
        from lab.historical_pricing import minute_close_payoffs
        from lab.mining import build_direction
        from lab.standard_mining import mine_standard
        from lab.standard_search import candidate_families
        from lab.selection import Quotes
        events=[{**event(0,line=8,w0=10,ts=10),'sid':'a','mid':0,'market':0},
                {**event(1,line=8,w0=200,ts=10),'sid':'a','mid':0,'market':0},
                {**event(2,line=8,w0=10,ts=20),'sid':'b','mid':1,'market':0}]
        labels={'a':{'eligible':True,'date':'2022-01-01','year':2022,'kickoff':10,'final':[3,0]},
                'b':{'eligible':True,'date':'2024-01-01','year':2024,'kickoff':20,'final':[0,0]}}
        instant=Quotes(events,labels,100,5);close=Quotes(events,labels,100,5,minute_close=True)
        self.assertLess(sum(t['pnl'] for t in instant.trades([0,2],[0,0])),0)
        expected=sum(t['pnl'] for t in close.trades([0,2],[0,0]));self.assertGreater(expected,0)
        payoffs=minute_close_payoffs(events,labels,100)
        for e in events:
            for side in (0,1):self.assertEqual(payoffs[e['eid'],side],(close.trade(e['eid'],side,reject_pregoal=True) or {'pnl':0})['pnl'])
        cfg=check_config({'directions':['LIVE_OVER'],'standard_node_budget':1,'min_profit':'0'})
        with tempfile.TemporaryDirectory() as td:
            mine_standard(Path(td),events,labels,100,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
            families=list(candidate_families(Path(td)/'mining/LIVE_OVER',0));self.assertTrue(any(f['net']==expected for f in families))

    def test_unfillable_first_signal_is_zero_not_a_later_replacement(self):
        from lab.mining import build_direction
        events=[{**event(i,line=8,ts=10 if i<2 else 11,valid=i!=1),'market':0} for i in range(3)]
        labels={'a':{'eligible':True,'date':'2024-01-01','year':2024,'kickoff':0,'final':[3,0]}}
        arrays=build_direction(events,labels,100,'LIVE_OVER',DEFAULT)
        self.assertEqual(arrays['eid'].tolist(),[0,2]);self.assertEqual(int(arrays['pnl'][0]),0);self.assertGreater(arrays['pnl'][1],0)

    def test_default_is_whole_history_with_adjustable_profit_floor(self):
        cfg=check_config({})
        self.assertEqual((cfg['research_objective'],cfg['holdout_months'],cfg['min_profit']),('historical',0,'10'))
        self.assertEqual(cfg['min_history_years'],2);self.assertFalse(cfg['historical_diagnostics'])
        self.assertEqual(cfg['prematch_trigger_status'],'any')
        with self.assertRaises(ValueError):check_config({'holdout_months':12})
        self.assertEqual(check_config({'min_profit':'0'})['min_profit'],'0')
        self.assertEqual(check_config({'min_profit':'20'})['min_profit'],'20')
        self.assertEqual(check_config({'research_objective':'validation'})['holdout_months'],12)
        self.assertEqual(check_config({'min_matches':20,'min_history_years':3})['min_matches'],20)
        self.assertEqual(check_config({'min_history_years':'3'})['min_history_years'],3)

    def test_recent_year_stays_in_the_discovery_set(self):
        labels={str(y):{'date':f'{y}-06-01','year':y,'eligible':True} for y in (2022,2023,2024)}
        events=[{**event(i),'sid':str(y),'mid':i} for i,y in enumerate((2022,2023,2024))]
        found,found_labels,held,held_labels,plan=split_holdout(events,labels,DEFAULT)
        self.assertEqual(set(found_labels),set(labels));self.assertEqual(len(found),3)
        self.assertEqual((held,held_labels,plan['status']),([],{},'FULL_HISTORY'))
        self.assertTrue(all(l.get('segment') for l in found_labels.values()))

    def test_historical_stability_uses_real_history_not_stress_profit(self):
        trades=[{'sid':str(i),'eid':i,'ts':i*11*1440,'sort_time':i*11*1440,'year':2022 if i<40 else 2024,
                 'segment':'A' if i<40 else 'B','pnl':200,'water':100,'quality':0} for i in range(80)]
        m=metrics(trades,100,[2022,2024],['A','B']);stress={**m,'net_i':-10000,'drawdown_i':20000}
        reasons,tags=research_gate_reasons(trades,m,[stress]*3,DEFAULT,100,{'A':40,'B':40},stress)
        self.assertEqual(reasons,[])
        _,tags=research_gate_reasons(trades,{**m,'history_days':200},[stress]*3,DEFAULT,100,{'A':40,'B':40},stress)
        self.assertIn('SHORT_HISTORY',tags)
        _,tags=research_gate_reasons(trades,m,[stress]*3,VALIDATION_DEFAULT,100,{'A':40,'B':40},stress)
        self.assertIn('STRESS_FAILED',tags)

    def test_portfolio_optimizes_historical_base_when_stress_is_negative(self):
        raw={'net_i':1000,'drawdown_match_i':100,'matches':50,
             'segment_net_i':{'A':500,'B':500},'segment_match_counts':{'A':25,'B':25},
             'segment_availability':{'A':25,'B':25},'history_days':800,'denominator':200}
        bad={**raw,'net_i':-1000,'drawdown_match_i':1000}
        score=lambda rs:{'raw':raw,'stress':[bad]*4}
        rule={'id':'r','conditions':[],'_trades':[]}
        with tempfile.TemporaryDirectory() as td:
            historical=PortfolioSearch([rule],score,DEFAULT,100,Path(td)/'h','h','2').score(('r',))
            validation=PortfolioSearch([rule],score,VALIDATION_DEFAULT,100,Path(td)/'v','v','2').score(('r',))
        self.assertTrue(historical['feasible']);self.assertFalse(validation['feasible']);self.assertEqual(historical['min_profit_i'],1000)

    def test_completion_needs_no_holdout_and_empty_search_can_finish(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'execution_economics':{'status':'PASS'},
                 'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(5)},'streaming_paper_execution':{'status':'PASS','scenarios':{'4':{'status':'PASS'}}}}
        result=completion_state(coverage,summary,trigger,{'status':'PASS'},True,DEFAULT)
        self.assertEqual(result['state'],'HISTORICAL_RESEARCH_COMPLETE');self.assertNotIn('holdout_evaluation',result['gates'])
        empty=completion_state(coverage,summary,{**trigger,'status':'EMPTY_ROSTER','rules':0},{'status':'PASS'},True,DEFAULT)
        self.assertEqual(empty['state'],'HISTORICAL_RESEARCH_COMPLETE');self.assertEqual(empty['recommendation']['status'],'HISTORICAL_SEARCH_EMPTY')
        partial=completion_state({**coverage,'remaining':1},summary,trigger,{'status':'PASS'},True,DEFAULT)
        self.assertEqual(partial['state'],'PARTIAL_RESULT')

    def test_small_positive_rule_is_preserved_before_stability_filtering(self):
        from lab.standard_mining import mine_standard
        from lab.standard_search import candidate_families
        events=[];labels={}
        for i in range(5):
            sid=str(i);labels[sid]={'eligible':True,'date':f'202{i}-06-01','year':2020+i,'kickoff':i,'final':[3,0]}
            events.append({**event(i,line=8,w0=10),'sid':sid,'mid':i,'market':0})
        cfg=check_config({'directions':['LIVE_OVER'],'standard_node_budget':1,'min_profit':'0'})
        with tempfile.TemporaryDirectory() as td:
            state=mine_standard(Path(td),events,labels,100,'LIVE_OVER',cfg,lambda **kw:None,lambda:False)
            families=list(candidate_families(Path(td)/'mining/LIVE_OVER',0))
            self.assertGreater(state['candidates'],0);self.assertTrue(any(0<f['net']<=2000 for f in families))

class FleetPlans(unittest.TestCase):
    def test_assignment_is_complete_disjoint_and_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'data';data.mkdir();records=[];leagues=[]
            for i,rows in enumerate((100,80,30)):
                league=f'L{i}';p=data/f'{league}.csv';p.write_text('fixture',encoding='utf-8')
                records.append({'kind':'quotes','path':str(p),'sha256':sha(p),'targets':[[league,'皇冠']]})
                leagues.append({'league':league,'company':'皇冠','rows':rows})
            with patch('lab.fleet.inspect',return_value={'files':records,'leagues':leagues}):
                summary=make_plans([str(data)],root/'plans',2,'皇冠')
            assigned=[l for p in summary['plans'] for l in p['leagues']]
            self.assertEqual(sorted(assigned),['L0','L1','L2']);self.assertEqual(len(set(assigned)),3)
            plan=root/'plans/node_001.json';first=import_plan(plan,data,root/'work');again=import_plan(plan,data,root/'work')
            self.assertEqual(first['jobs'],again['jobs']);self.assertTrue(all(j['status']=='PAUSED' for j in Store(root/'work').jobs()))
            other_plan=root/'plans/node_002.json';other=read_json(other_plan);relocated=root/'machine2/data';relocated.mkdir(parents=True)
            for rec in other['catalog']['files']:
                if any(l in other['leagues'] for l,c in rec['targets']):(relocated/rec['path']).write_bytes((data/rec['path']).read_bytes())
            imported=import_plan(other_plan,relocated,root/'work2',queue=True)
            self.assertEqual(len(imported['jobs']),len(other['leagues']));self.assertTrue(all(j['status']=='QUEUED' for j in Store(root/'work2').jobs()))
            saved=read_json(plan);rec=next(r for r in saved['catalog']['files'] if any(l in saved['leagues'] for l,c in r['targets']))
            (data/rec['path']).write_text('changed',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'哈希不一致'):import_plan(plan,data,root/'other')

    def test_plan_tampering_is_rejected_before_job_creation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'node.json';atomic_json(p,{'schema':'FSL_OFFLINE_LEAGUE_PLAN_V1','plan_hash':'bad'})
            with self.assertRaisesRegex(ValueError,'计划内容改变'):import_plan(p,root,root/'work')
            self.assertFalse((root/'work').exists())

if __name__=='__main__':unittest.main()

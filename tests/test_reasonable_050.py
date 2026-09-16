"""0.5.0 reasonableness optimizations: scale-aware gates on research segments, a pre-registered holdout,
return/drawdown portfolio risk, the computable compact grammar, the prematch trigger window and cross-market
staleness, material neighbourhood steps, parallel worker slots and the non-semantic engine fingerprint scope."""
import os,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import numpy as np
from lab import common
from lab.common import DEFAULT,MISSING,check_config,STANDARD_RESEARCH_THRESHOLDS,DEPRECATED_CONFIG,standard_thresholds_match,NON_SEMANTIC_MODULES,atomic_json,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.research_standard import (required_matches,segment_shortfalls,research_gate_reasons,portfolio_feasible,holdout_plan,holdout_verdict,
    direction_availability,trigger_window_allows,bonferroni)
from lab.selection import metrics,neighbors
from lab.workflow_gates import completion_state
from lab.store import Store
from lab.locking import WorkerSlot,AllWorkerSlots,WorkspaceBusy
from test_core import event,TEST_CONTRACT

_check_config=check_config
def check_config(value):
    return _check_config({'research_objective':'validation',**value}) if isinstance(value,dict) else _check_config(value)

WIN,LOSS=180,-200  # full win at water 0.90 and full loss, in 1/(2*100) stake units

def trade(i,pnl,segment='A'):
    return {'sid':f'm{i}','eid':i,'signal_eid':i,'ts':i,'sort_time':i,'pnl':pnl,'year':2024,'segment':segment,'quality':0,'water':90,'stake':1}

def prematch(i,status,market=1,ts=None,line=2):
    return {**event(i,line=line,ts=i if ts is None else ts),'phase':0,'status':status,'market':market}


class ScaleAwareConfiguration(unittest.TestCase):
    def test_retired_absolute_gates_are_refused_with_their_replacement(self):
        for key in DEPRECATED_CONFIG:
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'已废弃'):check_config({key:'1'})

    def test_standard_locks_every_research_setting_but_legacy_profiles_may_change_them(self):
        changes={'min_profit':'9','stress_min_profit':'8','min_matches':30,'min_matches_share':'0.2','min_matches_ceiling':100,'min_segment_share':'0.4',
                 'min_return_drawdown_ratio':'1.5','portfolio_min_return_drawdown_ratio':'1.5','conservative_portfolio_min_return_drawdown_ratio':'4',
                 'selection_profit_tolerance':'1','selection_profit_tolerance_share':'0.1','holdout_months':6,'holdout_max_share':'0.4','holdout_min_orders':20,
                 'prematch_trigger_status':'any','cross_stale_minutes':10,'cross_stale_minutes_prematch':600}
        self.assertEqual(set(changes),set(STANDARD_RESEARCH_THRESHOLDS))
        for key,value in changes.items():
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,key):check_config({key:value})
        legacy=check_config({'profile':'routine',**changes})
        self.assertEqual((legacy['min_matches'],legacy['min_return_drawdown_ratio']),(30,'1.5'))
        self.assertTrue(standard_thresholds_match(check_config({})));self.assertFalse(standard_thresholds_match(legacy))

    def test_inconsistent_ratio_sample_and_policy_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'较低风险'):check_config({'profile':'routine','conservative_portfolio_min_return_drawdown_ratio':'1'})
        with self.assertRaisesRegex(ValueError,'下限'):check_config({'profile':'routine','min_matches':90})
        for key,value in (('prematch_trigger_status','早'),('search_grammar','v4'),('package_handoff','yes'),('holdout_months',61)):
            with self.subTest(key=key),self.assertRaises(ValueError):check_config({'profile':'routine',key:value})


class SampleAndRiskGates(unittest.TestCase):
    def test_required_matches_scale_with_direction_availability(self):
        self.assertEqual([required_matches(DEFAULT,a) for a in (0,93,200,320,1000)],[40,40,50,80,80])

    def test_large_profitable_sample_passes_although_old_absolute_streak_and_drawdown_gates_fail(self):
        base=[trade(i,p,'A' if i%2 else 'B') for i,p in enumerate([LOSS]*8+[WIN,WIN,LOSS]*37+[WIN])]
        m=metrics(base,100,[2024],['A','B'])
        self.assertGreater(m['streak'],4);self.assertGreater(m['drawdown'],5);self.assertAlmostEqual(m['net'],22.5)
        self.assertEqual(research_gate_reasons(base,m,[m,m,m],DEFAULT,100,{'A':100,'B':100},m),([],[]))

    def test_small_lucky_sample_with_a_deep_drawdown_is_high_risk(self):
        base=[trade(i,p,'A' if i%2 else 'B') for i,p in enumerate([WIN]*15+[LOSS]*12+[WIN]*13)]
        m=metrics(base,100,[2024],['A','B']);_,tags=research_gate_reasons(base,m,[m,m,m],DEFAULT,100,{'A':80,'B':80})
        self.assertGreater(m['net'],10);self.assertIn('HIGH_RISK_ALTERNATIVE',tags)

    def test_time_concentration_fails_segment_coverage_and_remove_best_segment(self):
        base=[trade(i,p,'A') for i,p in enumerate([WIN,WIN,LOSS]*20)]
        m=metrics(base,100,[2024],['A','B'])
        self.assertEqual(segment_shortfalls(m,DEFAULT,{'A':60,'B':60}),['B'])
        self.assertEqual(segment_shortfalls(m,DEFAULT,{'A':90,'B':10}),[])  # a small segment is disclosed, not gated
        _,tags=research_gate_reasons(base,m,[m,m,m],DEFAULT,100,{'A':60,'B':60})
        self.assertIn('LOW_SAMPLE_OBSERVATION',tags);self.assertIn('CONCENTRATION_FAILED',tags)
        self.assertIsNone(metrics(base,100,[2024],['A'])['remove_best_segment'])

    def test_metrics_disclose_segments_and_uncorrected_significance(self):
        base=[trade(i,p,'A' if i<30 else 'B') for i,p in enumerate([WIN,WIN,LOSS]*20)]
        m=metrics(base,100,[2024],['A','B','C'])
        self.assertEqual(m['segment_counts'],{'A':30,'B':30,'C':0});self.assertAlmostEqual(m['segment_net']['A']+m['segment_net']['B'],m['net'])
        self.assertIsNotNone(m['z']);self.assertTrue(0<m['p_one_sided']<0.05)
        self.assertEqual(bonferroni(0.01,1000),1.0);self.assertAlmostEqual(bonferroni(0.0001,10),0.001)


class PortfolioRatio(unittest.TestCase):
    def test_feasibility_is_relative_to_profit(self):
        self.assertTrue(portfolio_feasible(0,0,'2',empty=True))
        self.assertTrue(portfolio_feasible(1000,500,'2'));self.assertFalse(portfolio_feasible(1000,501,'2'))
        self.assertFalse(portfolio_feasible(0,0,'2'));self.assertFalse(portfolio_feasible(-10,0,'2'))

    def test_a_growing_roster_is_not_capped_by_an_absolute_drawdown(self):
        from lab.portfolio_search import PortfolioSearch
        from test_v02 import synthetic_rule,scorer
        pool=[synthetic_rule(name,[-200]*6+[190]*30) for name in 'abcd']
        with tempfile.TemporaryDirectory() as td:result=PortfolioSearch(pool,scorer,DEFAULT,100,td,'grow','2').run()
        self.assertEqual(result['chosen'],['a','b','c','d'])
        self.assertEqual(result['score']['max_drawdown_i'],4800)  # 24 units: far beyond the retired 10-unit cap

    def test_profit_tolerance_is_relative_with_an_absolute_floor(self):
        from lab.portfolio_search import PortfolioSearch
        with tempfile.TemporaryDirectory() as td:search=PortfolioSearch([],lambda rs:None,DEFAULT,100,td,'tolerance','2')
        self.assertEqual((search.tolerance(0),search.tolerance(1000),search.tolerance(10000)),(100,100,500))


class Holdout(unittest.TestCase):
    def labels(self,dates):
        return {f's{i}':{'eligible':True,'date':d,'year':int(d[:4]),'final':[1,0]} for i,d in enumerate(dates)}

    def test_latest_twelve_months_are_split_off_before_discovery(self):
        from lab.holdout import split_holdout
        labels=self.labels(['2024-01-10','2024-09-20','2025-03-01','2025-06-01','2026-02-14','2026-05-21'])
        events=[{**event(len(sorted(labels))*2),'sid':sid,'mid':i} for i,sid in enumerate(sorted(labels)) for _ in range(2)]
        discovery,found,held,held_labels,plan=split_holdout(events,labels,DEFAULT)
        self.assertEqual((plan['status'],plan['cutoff'],plan['holdout_matches']),('SPLIT','2025-05-21',3))
        self.assertEqual(set(held_labels),{'s3','s4','s5'});self.assertFalse({e['sid'] for e in discovery}&set(held_labels))
        self.assertEqual([e['eid'] for e in discovery],list(range(6)));self.assertEqual([e['eid'] for e in held],list(range(6)))
        self.assertEqual(len({l['segment'] for l in found.values()}),2)

    def test_short_history_partitioned_training_and_disabled_plans(self):
        short=self.labels(['2025-09-01','2026-01-01','2026-03-01'])
        self.assertEqual(holdout_plan(short,DEFAULT)['status'],'UNAVAILABLE_SHORT_HISTORY')
        self.assertEqual(holdout_plan(short,{**DEFAULT,'holdout_months':0})['status'],'DISABLED')
        self.assertEqual(holdout_plan(short,{**DEFAULT,'research_partition':{'kind':'walk_forward_training'}})['status'],'DISABLED_FOR_PARTITIONED_TRAINING')

    def test_verdict_needs_enough_orders_and_positive_minute_close_and_rejection_nets(self):
        packet={'rules':[{'id':'r'}]}
        def report(n,nets):return {'scenarios':[{'n':n,'net_i':v} for v in nets]}
        self.assertEqual(holdout_verdict(packet,report(30,[10,-5,-5,-5,10]),DEFAULT),'CONFIRMED')
        self.assertEqual(holdout_verdict(packet,report(30,[10,5,5,5,0]),DEFAULT),'FAILED')
        self.assertEqual(holdout_verdict(packet,report(29,[10,5,5,5,10]),DEFAULT),'INSUFFICIENT_SAMPLE')
        self.assertEqual(holdout_verdict({'rules':[]},report(0,[0]*5),DEFAULT),'EMPTY_ROSTER')

    def test_unavailable_holdout_is_recorded_with_multiple_testing_disclosure(self):
        from lab.holdout import evaluate_holdout
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);atomic_json(root/'holdout_plan.json',{'status':'UNAVAILABLE_SHORT_HISTORY'})
            atomic_json(root/'results/rules.json',{'rules':[{'id':'r','direction':'LIVE_OVER','execution_metrics':{'z':3.0,'p_one_sided':0.0013}}]})
            summary=evaluate_holdout(root,DEFAULT,{'evaluated':100});saved=read_json(root/'results/样本外评测.json')
        self.assertEqual(summary['status'],'UNAVAILABLE_SHORT_HISTORY')
        self.assertAlmostEqual(saved['multiple_testing']['selected_rules'][0]['bonferroni_p'],0.13)
        self.assertEqual(saved['multiple_testing']['significant_after_correction'],0)


class Recommendation(unittest.TestCase):
    coverage={'standard_search_complete':True,'remaining':0,'planned_expressions':100,'local_search_complete':True,'local_profile':'standard'}
    summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
    trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'execution_economics':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)}}

    def state(self,verdict,coverage=None,**kw):
        return completion_state(coverage or self.coverage,self.summary,self.trigger,{'status':'PASS'},True,DEFAULT,holdout={'status':verdict,'main':{'verdict':verdict}},**kw)

    def test_only_a_confirmed_holdout_recommends_paper_trading(self):
        ok=self.state('CONFIRMED');failed=self.state('FAILED')
        self.assertEqual((ok['state'],ok['recommendation']['status']),('STANDARD_HANDOFF_COMPLETE','RECOMMENDED_FOR_PAPER_TRADING'))
        self.assertEqual((failed['state'],failed['recommendation']['status']),('STANDARD_HANDOFF_COMPLETE','OBSERVATION_ONLY'))
        self.assertIn('样本外',failed['recommendation']['reasons'][0]);self.assertFalse(ok['recommendation']['live_approval'])

    def test_incomplete_search_skipped_packaging_and_missing_holdout(self):
        partial=self.state('CONFIRMED',{**self.coverage,'standard_search_complete':False,'remaining':40})
        self.assertEqual(partial['coverage_ratio'],0.6);self.assertIn('60.0000%',partial['recommendation']['reasons'][0])
        skipped=self.state('CONFIRMED',packaging_skipped=True)
        self.assertEqual(skipped['state'],'RESEARCH_COMPLETE_NO_HANDOFF');self.assertNotIn('packaging',skipped['gates'])
        missing=completion_state(self.coverage,self.summary,self.trigger,{'status':'PASS'},True,DEFAULT)
        self.assertFalse(missing['gates']['holdout_evaluation']);self.assertEqual(missing['state'],'PARTIAL_RESULT')


class PrematchSemantics(unittest.TestCase):
    policy={'match_cap':4,'stale_minutes':5,'priority':'frozen_rule_priority','feature_version':'v3','cross_stale_minutes':5,'cross_stale_minutes_prematch':1440,
            'prematch_trigger_status':'即','live_enabled':False,'second_slot_enabled':False}

    def test_trigger_window_policy(self):
        early=prematch(0,'早');same_day=prematch(1,'即')
        self.assertFalse(trigger_window_allows(early,self.policy));self.assertTrue(trigger_window_allows(same_day,self.policy))
        self.assertTrue(trigger_window_allows(event(2),self.policy))
        self.assertTrue(trigger_window_allows(early,{'prematch_trigger_status':'any'}));self.assertTrue(trigger_window_allows(early,{}))

    def test_signal_engine_never_triggers_on_early_market_quotes(self):
        from lab.features import SignalEngine
        from lab.contracts import bind_rule
        rule=bind_rule({'id':'pre','priority':0,'direction':'PRE_GIVE','conditions':[{'feature':'water','op':'ge','value':90}]},TEST_CONTRACT)
        engine=SignalEngine([rule],contract=TEST_CONTRACT,execution_policy=self.policy);engine.mark_history_complete('a')
        self.assertEqual(engine.feed(prematch(0,'早')),[])
        self.assertEqual([s['strategy_id'] for s in engine.feed(prematch(1,'即'))],['pre'])
        legacy=SignalEngine([rule],contract=TEST_CONTRACT,execution_policy={**self.policy,'prematch_trigger_status':'any'});legacy.mark_history_complete('a')
        self.assertEqual([s['strategy_id'] for s in legacy.feed(prematch(0,'早'))],['pre'])

    def test_prematch_cross_market_age_uses_its_own_limit(self):
        from lab.standard_features import StandardFeatureStream
        stream=StandardFeatureStream(contract=TEST_CONTRACT,cross_stale_minutes=5,cross_stale_minutes_prematch=1440)
        stream.feed(prematch(0,'即',market=0,ts=0,line=10));current=stream.feed(prematch(1,'即',ts=100))
        self.assertEqual((current[0]['cross_line'],current[0]['cross_age']),(10,100))
        strict=StandardFeatureStream(contract=TEST_CONTRACT,cross_stale_minutes=5)
        strict.feed(prematch(0,'即',market=0,ts=0,line=10));self.assertEqual(strict.feed(prematch(1,'即',ts=100))[0]['cross_line'],MISSING)
        live=StandardFeatureStream(contract=TEST_CONTRACT,cross_stale_minutes=5,cross_stale_minutes_prematch=1440)
        live.feed({**event(0,ts=0,line=10),'market':0});self.assertEqual(live.feed(event(1,ts=100))[0]['cross_line'],MISSING)

    def test_direction_availability_counts_trigger_window_matches_per_segment(self):
        labels={'x':{'eligible':True,'segment':'S1'},'y':{'eligible':True,'segment':'S2'},'z':{'eligible':False,'segment':'S2'}}
        rows=[{**prematch(0,'早'),'sid':'x'},{**prematch(1,'即'),'sid':'y'},{**prematch(2,'即'),'sid':'z'},{**event(3),'sid':'x'}]
        self.assertEqual(direction_availability(rows,labels,['PRE_GIVE','LIVE_GIVE','PRE_OVER'],self.policy),{'PRE_GIVE':{'S2':1},'LIVE_GIVE':{'S1':1},'PRE_OVER':{}})
        self.assertEqual(direction_availability(rows,labels,['PRE_GIVE'],{})['PRE_GIVE'],{'S1':1,'S2':1})


class CompactGrammar(unittest.TestCase):
    def test_compact_atoms_are_coarse_bounded_and_two_condition(self):
        from lab.standard_spec import compact_atoms,max_conditions_for,make_standard_plan,COMPACT_MODULES
        spans={'absline':(0,10),'water':(60,130),'otherwater':(60,130),'line_init':(-4,4),'water_init':(-30,30),'path2_keep':(11,44),'path1_keep':(1,4),'cross_line':(4,16)}
        values={'path2_keep':{11,12,21,44},'path1_keep':{1,2,3,4}}
        atoms=list(compact_atoms('LIVE_GIVE',100,spans.get,lambda f:values.get(f) if f in spans else None))
        water=[a for a in atoms if a['feature'] in ('water','otherwater')]
        self.assertTrue(water);self.assertFalse([a for a in water if a['op']=='eq']);self.assertTrue(all(a['value']%5==0 for a in water))
        self.assertLess(len(atoms),300);self.assertEqual(len(atoms),len({str(sorted(a.items())) for a in atoms}))
        self.assertEqual({a['value'] for a in atoms if a['feature']=='path2_keep'},{11,12,21,44})
        self.assertEqual({(a['op'],a['value']) for a in atoms if a['feature']=='line_init'},{('eq',0),('le',-1),('ge',1),('le',-2),('ge',2),('le',-4),('ge',4)})
        self.assertEqual((max_conditions_for(DEFAULT),max_conditions_for({'search_grammar':'v3_full'})),(2,3))
        plan=make_standard_plan(10,list(range(10)),[],[],[],[],[],max_conditions=2)
        self.assertNotIn('M03',{b['module'] for b in plan['blocks']});self.assertIn('M03',COMPACT_MODULES)

    def test_identically_zero_features_never_enter_the_v3_dictionary(self):
        from lab.standard_atoms import build_standard_catalog
        class Columns(dict):
            def domain(self,name):return (0,0) if name.startswith('line_') else (90,95)
        columns=Columns({'line_same':np.zeros(2,np.int64),'line_return_keep':np.zeros(2,np.int64),'water':np.array([90,95])})
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            stack.enter_context(patch('lab.standard_atoms.EXTRA_FIELDS',{'line_same','line_return_keep','line_return_reset'}))
            for name in ('path_atoms','required_templates'):stack.enter_context(patch('lab.standard_atoms.'+name,return_value=[]))
            atoms,_=build_standard_catalog(Path(td),columns,'PRE_GIVE',100,{**DEFAULT,'search_grammar':'v3_full'})
            features={a['feature'] for a in atoms};atoms.close()
        self.assertIn('water',features);self.assertFalse(features&{'line_same','line_return_keep','line_return_reset'})


class MaterialNeighbourhoods(unittest.TestCase):
    def test_perturbations_have_a_real_difference(self):
        self.assertEqual([x['value'] for x in neighbors({'feature':'water','op':'ge','value':90},100)],[85,95])
        self.assertEqual([x['value'] for x in neighbors({'feature':'minute','op':'ge','value':30},100)],[25,35])
        self.assertEqual([(x['value'],x['upper']) for x in neighbors({'feature':'minute','op':'range','value':0,'upper':16},100)],[(5,21)])
        self.assertEqual([x['value'] for x in neighbors({'feature':'line','op':'ge','value':2},100)],[1,3])
        spans={x.get('span') for x in neighbors({'feature':'path2_keep','op':'eq','value':12,'span':10},100)}
        self.assertTrue({5,15}<=spans)


class ParallelWorkers(unittest.TestCase):
    def test_slots_follow_the_workspace_limit_and_maintenance_needs_all(self):
        with tempfile.TemporaryDirectory() as td:
            with WorkerSlot(td,2) as first,WorkerSlot(td,2) as second:
                self.assertEqual({first.index,second.index},{0,1})
                with self.assertRaises(WorkspaceBusy):WorkerSlot(td,2).__enter__()
                with self.assertRaises(WorkspaceBusy):AllWorkerSlots(td).__enter__()
            with AllWorkerSlots(td):
                with self.assertRaises(WorkspaceBusy):WorkerSlot(td,8).__enter__()

    def test_claim_respects_the_parallel_limit(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jobs=[store.create(f'league{i}',DEFAULT,{'files':[]}) for i in range(3)]
            self.assertEqual(store.parallel_limit(),1)
            self.assertTrue(store.claim(jobs[0],os.getpid()));self.assertFalse(store.claim(jobs[1],os.getpid()))
            store.set_parallel_limit(2);self.assertTrue(store.claim(jobs[1],os.getpid()));self.assertFalse(store.claim(jobs[2],os.getpid()))
            with self.assertRaises(ValueError):store.set_parallel_limit(9)


class EngineFingerprintScope(unittest.TestCase):
    def test_non_semantic_modules_do_not_change_the_engine_hash_but_computation_modules_do(self):
        real=common.sha;base=common.source_fingerprint()
        for name,changes in (('reporting.py',False),('server.py',False),('packaging.py',False),('selection.py',True),('research_standard.py',True)):
            with self.subTest(name=name),patch.object(common,'sha',side_effect=lambda p,n=name:'changed' if Path(p).name==n else real(p)):
                self.assertEqual(common.source_fingerprint()!=base,changes)
        self.assertIn('server.py',NON_SEMANTIC_MODULES);self.assertNotIn('selection.py',NON_SEMANTIC_MODULES)


if __name__=='__main__':unittest.main()

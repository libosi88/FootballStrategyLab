import copy
import tempfile
import unittest
from pathlib import Path
from lab.common import DEFAULT,check_config
from lab.history_policy import (segment_evidence,segment_policy_reasons,portfolio_history_reasons,
                                portfolio_availability,qualify_results,compare_roster_risk)
from lab.research_standard import assign_segments,direction_availability,research_gate_reasons
from lab.selection import metrics,portfolio_metrics,dispatch
from lab.portfolio_search import PortfolioSearch
from lab.portfolio_replay import MatchReplayScorer
from lab.complementarity import study_pairs
from rationality_fixtures import losing_portfolio,complementary_portfolio


class HistoricalRationality(unittest.TestCase):
    def test_both_singletons_pass_but_losing_joint_period_is_refused(self):
        cfg=check_config({});events,labels,rules=losing_portfolio(cfg)
        availability=direction_availability(events,labels,['LIVE_GIVE'],cfg)['LIVE_GIVE']
        for r in rules:
            trades=r['_trades'][4];m=metrics(trades,100,[2022,2023,2024],availability)
            self.assertEqual(research_gate_reasons(trades,m,[],cfg,100,availability)[0],[])
        full=portfolio_metrics(dispatch(rules,4,4,priority_mode='frozen_rule_priority')[0],100,[2022,2023,2024])
        full['segment_availability']=availability
        self.assertAlmostEqual(full['net'],71.4)
        self.assertTrue(portfolio_history_reasons(full,cfg))
        scorer=MatchReplayScorer(rules,4,4,segment_availability=availability)
        fast=scorer(rules)['historical']
        for key in ('net_i','drawdown_match_i','segment_net_i','segment_match_counts','history_days'):
            self.assertEqual(fast[key],full[key],key)
        with tempfile.TemporaryDirectory() as td:
            solver=PortfolioSearch(rules,scorer,cfg,100,Path(td),'counterexample','2')
            self.assertFalse(solver.score(('r0','r1'))['feasible'])
            result=solver.run()
            self.assertTrue(result['chosen']);self.assertTrue(result['score']['feasible'])
            self.assertNotEqual(set(result['chosen']),{'r0','r1'})

    def test_portfolio_missing_segment_evidence_fails_closed(self):
        self.assertTrue(portfolio_history_reasons({'net_i':10000},DEFAULT))
        self.assertEqual(portfolio_history_reasons({},DEFAULT,empty=True),[])

    def test_minor_negative_period_is_blocked_on_both_sides_of_old_threshold(self):
        m={'n':180,'denominator':200,'segment_counts':{'A':80,'B':80,'C':20},
           'segment_net_i':{'A':7400,'B':7400,'C':-4000}}
        for size in (35,36):
            evidence=segment_evidence(m,DEFAULT,{'A':100,'B':100,'C':size})
            self.assertTrue(segment_policy_reasons(evidence,DEFAULT))
            row=next(x for x in evidence['segments'] if x['segment']=='C')
            self.assertEqual(row['evidence_status'],'LOSING_SEGMENT')

    def test_small_positive_is_disclosed_without_inventing_strong_evidence(self):
        m={'n':81,'denominator':200,'segment_counts':{'A':40,'B':40,'C':1},
           'segment_net_i':{'A':2000,'B':2000,'C':190}}
        evidence=segment_evidence(m,DEFAULT,{'A':100,'B':100,'C':1})
        self.assertEqual(segment_policy_reasons(evidence,DEFAULT),[])
        self.assertFalse(evidence['all_segments_sufficient_and_positive'])
        self.assertTrue(evidence['has_small_sample_segments'])

    def test_concentration_is_disclosed_and_optional_gate_is_frozen(self):
        m={'n':150,'denominator':200,'segment_counts':{'A':50,'B':50,'C':50},
           'segment_net_i':{'A':20,'B':6000,'C':20}}
        evidence=segment_evidence(m,DEFAULT)
        self.assertGreater(evidence['best_segment_positive_profit_share'],.99)
        self.assertEqual(segment_policy_reasons(evidence,DEFAULT),[])
        self.assertTrue(segment_policy_reasons(evidence,{**DEFAULT,'max_best_segment_profit_share':'.9'}))

    def test_calendar_segments_do_not_move_when_future_data_is_appended(self):
        labels={'a':{'date':'2023-06-01','eligible':True},'b':{'date':'2024-05-01','eligible':True}}
        assign_segments(labels,config=DEFAULT);old={k:v['segment'] for k,v in labels.items()}
        labels['c']={'date':'2025-09-01','eligible':True};assign_segments(labels,config=DEFAULT)
        self.assertEqual({k:labels[k]['segment'] for k in old},old)

    def test_fixed_anchor_and_policy_inputs_are_validated(self):
        with self.assertRaises(ValueError):check_config({'segment_basis':'fixed_365'})
        cfg=check_config({'segment_basis':'fixed_365','segment_anchor_date':'2024-12-31'})
        labels={'a':{'date':'2023-01-01','eligible':True}}
        assign_segments(labels,config=cfg);self.assertEqual(labels['a']['segment_basis'],'fixed_365')
        for change in ({'major_segment_share':0},{'portfolio_segment_checks':'true'},
                       {'missing_result_status':'guess'},{'complementarity_pair_budget':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):check_config(change)

    def test_missing_completion_evidence_requires_explicit_trial_or_contract(self):
        base={'a':{'eligible':True,'result_status':'','final':[2,1]},
              'b':{'eligible':False,'conflict':True,'result_status':'完','final':[2,1]}}
        labels=copy.deepcopy(base);report=qualify_results(labels,DEFAULT)
        self.assertFalse(labels['a']['eligible']);self.assertEqual(report['label_only_trial_included'],0)
        labels=copy.deepcopy(base);report=qualify_results(labels,{**DEFAULT,'missing_result_status':'trial'})
        self.assertTrue(labels['a']['eligible']);self.assertFalse(labels['b']['eligible'])
        self.assertEqual(report['label_only_trial_included'],1)
        with self.assertRaises(ValueError):check_config({'missing_result_status':'archive_contract'})
        cfg=check_config({'missing_result_status':'archive_contract','archive_result_contract':'Synthetic archive contract v1'})
        labels=copy.deepcopy(base);report=qualify_results(labels,cfg)
        self.assertEqual(labels['a']['result_evidence_status'],'DECLARED_ARCHIVE_CONTRACT')
        self.assertFalse(report['independent_results_verified'])

    def test_stricter_ratio_is_not_assumed_lower_actual_risk(self):
        main={'n':40,'net':20,'drawdown_match':5,'drawdown_day':5,'streak':3,'max_match_stake':2,'worst_match_net':-2}
        backup={**main,'net':40,'drawdown_match':10}
        self.assertEqual(compare_roster_risk(main,backup)['status'],'RISK_TRADEOFF_OR_INSUFFICIENT_EVIDENCE')

    def test_separate_complementary_pair_is_found_and_never_opens_execution(self):
        cfg=check_config({'directions':['LIVE_GIVE','LIVE_OVER'],'complementarity_research':True})
        events,labels,rules=complementary_portfolio(cfg)
        avail=direction_availability(events,labels,cfg['directions'],cfg)
        for r in rules:
            trades=r['_trades'][4];m=metrics(trades,100,[2022,2023,2024],avail[r['direction']])
            _,r['tags']=research_gate_reasons(trades,m,[],cfg,100,avail[r['direction']])
            self.assertEqual(r['tags'],['HIGH_RISK_ALTERNATIVE'])
        original=copy.deepcopy(rules)
        with tempfile.TemporaryDirectory() as td:
            result=study_pairs(Path(td),rules,events,labels,cfg,100)
            self.assertEqual(result['status'],'PAIR_STUDY_COMPLETE')
            self.assertEqual(result['qualified_pairs'],1);self.assertAlmostEqual(result['best']['metrics']['net'],51)
            self.assertFalse(result['primary_roster_changed']);self.assertFalse(result['execution_handoff_verified'])
            self.assertFalse(result['live_enabled'])
            again=study_pairs(Path(td),rules,events,labels,cfg,100)
            self.assertEqual(again['best'],result['best'])
        self.assertEqual(rules,original)


if __name__=='__main__':unittest.main()

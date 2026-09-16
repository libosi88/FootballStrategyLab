"""New historical-policy and crash-recovery regressions; synthetic data only."""
import copy,itertools,json,subprocess,sys,tempfile,unittest
from collections import Counter
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT,ROOT,check_config,digest,timestamp
from lab.contracts import bind_rule
from lab.features import SignalEngine
from lab.history_policy import segment_evidence,segment_policy_reasons,portfolio_history_reasons,qualify_results,compare_roster_risk
from lab.paper_execution import PaperDispatcher
from lab.paper_runner import StreamingPaperRunner
from lab.portfolio_replay import MatchReplayScorer
from lab.portfolio_search import PortfolioSearch
from lab.research_standard import assign_segments,research_gate_reasons,direction_availability
from lab.selection import Quotes,metrics,dispatch,portfolio_metrics,PREGOAL_REJECTION_POLICY
from test_core import event,TEST_CONTRACT
from test_paper_execution import packet

def stream_fixture():
    p=packet(['LIVE_GIVE'])
    p['rules']=[bind_rule({'id':'r0','direction':'LIVE_GIVE','priority':0,
        'conditions':[{'feature':'water_prev_keep','op':'le','value':-10}]},p['contract'])]
    p['execution_policy'].update(feature_version='v3',cross_stale_minutes=5,
        warmup='complete_archive_history_required_for_history_based_features')
    p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
    return p,[event(i,ts=i+1,w0=w) for i,w in enumerate((95,110,100,100))]

def portfolio_fixture(complement=False):
    directions=['LIVE_GIVE','LIVE_OVER' if complement else 'LIVE_GIVE'];minutes=[5,5 if complement else 20]
    if complement:
        first=[{0:True,1:False}]*20+[{0:False,1:True}]*20+[{0:True,1:True}]*10
        schedules={y:first for y in (2022,2023,2024)}
    else:
        first=[{0:False,1:True}]*10+[{0:True}]*12+[{1:False}]*6
        later=[x for _ in range(10) for x in ({0:True,1:False},{1:True})]+[{1:True}]*8+[{0:True}]*12
        schedules={2022:first,2023:later,2024:later}
    events=[];labels={}
    for year,schedule in schedules.items():
        for day_offset,outcomes in enumerate(schedule):
            day=(date(year,1,1)+timedelta(days=day_offset)).isoformat();sid=f'{year}-{day_offset:03d}';kickoff=timestamp(day+' 12:00')
            labels[sid]={'eligible':True,'final':[2,1],'year':year,'date':day,'kickoff':kickoff,'result_status':'完','league':TEST_CONTRACT['league']}
            for i,won in sorted(outcomes.items()):
                total=directions[i].endswith('OVER');line=(10 if won else 14) if total else (2 if won else 6)
                q=event(len(events),line=line,w0=95,w1=85,ts=kickoff+minutes[i])
                q.update(sid=sid,mid=len(labels)-1,market=0 if total else 1,minute=minutes[i],minute_raw=str(minutes[i]));events.append(q)
    assign_segments(labels,config=DEFAULT)
    rules=[bind_rule({'id':f'r{i}','direction':d,'priority':i,'conditions':[{'feature':'minute','op':'eq','value':minutes[i]}]},TEST_CONTRACT) for i,d in enumerate(directions)]
    policy={'feature_version':'v3','cross_stale_minutes':5,'cross_stale_minutes_prematch':1440,'prematch_trigger_status':'any','priority':'frozen_rule_priority','match_cap':4,'stale_minutes':5,'quote_mapping':'minute_close_latest_v3','pregoal_rejection':PREGOAL_REJECTION_POLICY}
    engine=SignalEngine(rules,contract=TEST_CONTRACT,execution_policy=policy)
    for sid in labels:engine.mark_history_complete(sid)
    signals=[]
    for q in events:signals.extend(engine.feed(q))
    quotes=Quotes(events,labels,100,5,minute_close=True)
    for r in rules:
        ts=[quotes.trade(s['eid'],s['side'],reject_pregoal=True) for s in signals if s['strategy_id']==r['id']]
        r['_trades']=[[t for t in ts if t is not None] for _ in range(5)];r['_execution_priority']=r['priority']
    return events,labels,rules,signals,policy,quotes,dict(Counter(l['segment'] for l in labels.values()))

class RationalitySegments(unittest.TestCase):
    def test_defaults_preserve_full_history_profit_pool_and_disabled_execution(self):
        c=check_config({})
        self.assertEqual((c['research_objective'],c['holdout_months'],c['min_profit']),('historical',0,'10'))
        self.assertTrue(c['portfolio_segment_checks']);self.assertEqual(c['segment_basis'],'calendar_year')
        self.assertFalse(c['live_enabled']);self.assertFalse(c['second_slot_enabled'])

    def test_losing_small_segment_not_hidden_at_fifteen_percent_boundary(self):
        m={'n':180,'denominator':200,'segment_counts':{'A':80,'B':80,'C':20},'segment_net_i':{'A':7400,'B':7400,'C':-4000}}
        for last in (35,36):
            e=segment_evidence(m,DEFAULT,{'A':100,'B':100,'C':last})
            self.assertTrue(segment_policy_reasons(e,DEFAULT));self.assertEqual(e['segments'][-1]['evidence_status'],'LOSING_SEGMENT')

    def test_small_positive_segment_is_not_claimed_sufficient(self):
        e=segment_evidence({'n':2,'denominator':200,'segment_counts':{'A':2},'segment_net_i':{'A':380}},DEFAULT,{'A':2})
        self.assertEqual(e['segments'][0]['evidence_status'],'INSUFFICIENT_SEGMENT_SAMPLE');self.assertFalse(e['all_segments_sufficient_and_positive'])

    def test_concentration_cap_is_explicit(self):
        e=segment_evidence({'n':90,'denominator':200,'segment_counts':{'A':30,'B':30,'C':30},'segment_net_i':{'A':20,'B':6000,'C':20}},DEFAULT)
        self.assertGreater(e['best_segment_positive_profit_share'],.99)
        self.assertTrue(segment_policy_reasons(e,{**DEFAULT,'max_best_segment_profit_share':'.8'}))

    def test_calendar_year_does_not_shift_with_new_data(self):
        labels={'a':{'date':'2023-12-31','eligible':True}};assign_segments(labels,config=DEFAULT);before=labels['a']['segment']
        labels['b']={'date':'2024-06-20','eligible':True};assign_segments(labels,config=DEFAULT)
        self.assertEqual(labels['a']['segment'],before);self.assertEqual(before,'2023-01-01~2023-12-31')

    def test_fixed_anchor_requires_a_valid_explicit_date(self):
        with self.assertRaises(ValueError):check_config({'segment_basis':'fixed_365'})
        c=check_config({'segment_basis':'fixed_365','segment_anchor_date':'2024-12-31'})
        labels={'a':{'date':'2023-12-31','eligible':True}};assign_segments(labels,config=c);before=labels['a']['segment']
        labels['b']={'date':'2025-02-01','eligible':True};assign_segments(labels,config=c);self.assertEqual(labels['a']['segment'],before)

    def test_losing_joint_year_fails_despite_qualified_singletons(self):
        events,labels,rules,_,_,_,available=portfolio_fixture();avail=direction_availability(events,labels,[r['direction'] for r in rules],DEFAULT)
        for r in rules:
            reasons,_=research_gate_reasons(r['_trades'][4],metrics(r['_trades'][4],100,[2022,2023,2024]),[],DEFAULT,100,avail[r['direction']]);self.assertEqual(reasons,[])
        def score(rs):
            m=portfolio_metrics(dispatch(rs,4,4,priority_mode='frozen_rule_priority')[0],100,[2022,2023,2024]);m['segment_availability']=available
            return {'raw':m,'stress':[],'historical':m}
        joint=score(rules)['historical'];self.assertGreater(joint['net'],70);self.assertLess(min(joint['segment_net_i'].values()),0);self.assertTrue(portfolio_history_reasons(joint,DEFAULT))
        with tempfile.TemporaryDirectory() as td:result=PortfolioSearch(rules,score,DEFAULT,100,td,'test','2').run()
        self.assertTrue(result['chosen']);self.assertNotEqual(set(result['chosen']),{'r0','r1'});self.assertTrue(result['score']['feasible'])

    def test_economic_acceptance_rechecks_joint_orders(self):
        from lab.execution_economics import verify_economics
        _,labels,rules,signals,policy,quotes,_=portfolio_fixture()
        m=portfolio_metrics(dispatch(rules,4,4,priority_mode='frozen_rule_priority')[0],100,[2022,2023,2024])
        p={'rules':rules,'scope':'FSL_STANDARD_V3_2','water_scale':100,'execution_policy':policy}
        r=verify_economics(p,{r['id']:[s for s in signals if s['strategy_id']==r['id']] for r in rules},quotes,labels,DEFAULT,{'scenarios':{str(i):{'metrics':m} for i in range(5)}})
        self.assertEqual(r['status'],'FAIL');self.assertEqual(r['portfolio_segment_verification']['status'],'FAIL')

    def test_incremental_and_full_replay_segment_evidence_match(self):
        _,_,rules,_,_,_,available=portfolio_fixture();scorer=MatchReplayScorer(rules,4,4,segment_availability=available,scale=100)
        subsets=[list(c) for k in range(3) for c in itertools.combinations(rules,k)]
        for base in subsets:
            scorer.rebase([r['id'] for r in base])
            for subset in subsets:
                full=portfolio_metrics(dispatch(subset,4,4,priority_mode='frozen_rule_priority')[0],100,[2022,2023,2024]);fast=scorer(subset)['historical']
                for key in ('net_i','drawdown_match_i','matches','history_days','segment_net_i','segment_match_counts'):self.assertEqual(fast[key],full[key],key)

class RationalityRecovery(unittest.TestCase):
    def test_batched_checkpoint_failure_keeps_first_signal(self):
        p,qs=stream_fixture()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with StreamingPaperRunner(path,p,checkpoint_every=20) as r:
                r.declare_new_match('a');r.feed(qs[0]);r.save(True);r.feed(qs[1]);self.assertEqual(len(r.feed(qs[2])['signals']),1)
                with patch.object(r.dispatcher,'save_runner_state',side_effect=OSError('synthetic checkpoint failure')):
                    with self.assertRaises(OSError):r.save(True)
            with StreamingPaperRunner(path,p,checkpoint_every=20) as r:
                self.assertEqual(r.watermarks['a'],3);self.assertTrue(r.pending)
                for q in qs[1:]:r.feed(q)
                r.flush(5,'a');self.assertEqual(r.dispatcher.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

    def test_atomic_observation_rolls_back_state_and_clocks(self):
        p,_=stream_fixture()
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            before={'watermarks':{},'test':'before'};d.save_runner_state('coordinator',before);advance=d._advance_clock
            def fail_second(sid,minute):
                advance(sid,minute)
                if sid=='b':raise OSError('synthetic transaction failure')
            with patch.object(d,'_advance_clock',side_effect=fail_second):
                with self.assertRaises(OSError):d.save_observation({'watermarks':{'a':2,'b':3}},{'a':2,'b':3})
            self.assertEqual(d.load_runner_state('coordinator'),before);self.assertEqual(d.observed_clocks(),{})

    def test_match_end_is_durable_with_batched_checkpoints(self):
        p,qs=stream_fixture()
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'paper.db',p,checkpoint_every=20) as r:
            r.declare_new_match('a');r.feed(qs[0]);r.finish_match('a')
            self.assertIn('a',r.dispatcher.load_runner_state('coordinator')['engine']['closed_matches'])

    def test_real_process_exit_preserves_batched_signal(self):
        p,qs=stream_fixture()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);payload=root/'fixture.json';path=root/'paper.db';payload.write_text(json.dumps({'packet':p,'quotes':qs}),encoding='utf-8')
            code="import json,os,sys\nfrom pathlib import Path\nfrom lab.paper_runner import StreamingPaperRunner\nv=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\nr=StreamingPaperRunner(sys.argv[2],v['packet'],checkpoint_every=20)\nr.declare_new_match('a')\nr.feed(v['quotes'][0]);r.save(True)\nr.feed(v['quotes'][1]);r.feed(v['quotes'][2])\nos._exit(23)\n"
            done=subprocess.run([sys.executable,'-B','-c',code,str(payload),str(path)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(done.returncode,23,done.stderr.decode(errors='replace'))
            with StreamingPaperRunner(path,p,checkpoint_every=20) as r:
                for q in qs[1:]:r.feed(q)
                r.flush(5,'a');self.assertEqual(r.dispatcher.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

class RationalityScopeAndEvidence(unittest.TestCase):
    def test_historical_migration_template_preserves_company_resources_and_risk_cap(self):
        from lab.migration import _rebuild_config,REBUILD_BUDGETS
        preset=json.loads((ROOT/'config/rebuild_historical.json').read_text(encoding='utf-8'))
        old=check_config({'company':'平博','max_feature_cache_mb':64,'match_cap':2,
            'selection_checkpoint_every':17,'standard_node_budget':8192})
        _,new=_rebuild_config(old,None,preset)
        self.assertEqual((new['company'],new['max_feature_cache_mb'],new['match_cap'],new['selection_checkpoint_every']),('平博',64,2,17))
        for name in REBUILD_BUDGETS:self.assertEqual(new[name],0,name)
        self.assertTrue(new['portfolio_segment_checks'])

    def test_balanced_thresholds_and_three_step_paths(self):
        from lab.standard_spec import balanced_atoms,max_conditions_for
        atoms=list(balanced_atoms('LIVE_GIVE',100,lambda f:(-30,150),lambda f:{1,2,3,4,12,21}));core=[a for a,c in atoms if c]
        for v in (-15,-20):self.assertIn({'feature':'samewater','op':'le','value':v},core)
        self.assertIn({'feature':'line_init','op':'ge','value':3},core)
        self.assertTrue(any(a['feature']=='path3_reset' and a.get('pulse') for a,c in atoms if not c))
        self.assertEqual(max_conditions_for({'search_grammar':'balanced_v1'}),3);self.assertFalse(any('span' in a for a,c in atoms))

    def test_excluded_modules_are_not_reported_as_searched(self):
        from lab.common import atomic_json
        from lab.workflow_gates import module_coverage
        for grammar,excluded in [('compact_v1',{'M03','M04','M05'}),('balanced_v1',{'M04'})]:
            with tempfile.TemporaryDirectory() as td:
                atomic_json(Path(td)/'config.json',{**DEFAULT,'search_grammar':grammar})
                r=module_coverage(td,{'directions':{},'units':{'water':100},'standard_search_complete':False})
                self.assertEqual(set(r['excluded_modules']),excluded);self.assertTrue(all(r['modules'][m]['status']=='NOT_APPLICABLE' for m in excluded))

    def test_missing_completion_is_isolated_or_disclosed(self):
        original={'x':{'eligible':True,'final':[2,1],'result_status':''}};labels=copy.deepcopy(original);r=qualify_results(labels,DEFAULT)
        self.assertFalse(labels['x']['eligible']);self.assertEqual(r['label_only_trial_included'],0)
        labels=copy.deepcopy(original);r=qualify_results(labels,{**DEFAULT,'missing_result_status':'trial'})
        self.assertTrue(labels['x']['eligible']);self.assertEqual(r['label_only_trial_included'],1);self.assertTrue(original['x']['eligible'])

    def test_archive_contract_cannot_be_assumed(self):
        with self.assertRaises(ValueError):check_config({'missing_result_status':'archive_contract'})
        c=check_config({'missing_result_status':'archive_contract','archive_result_contract':'synthetic documented source'})
        labels={'x':{'eligible':True,'result_status':''}};r=qualify_results(labels,c)
        self.assertEqual(labels['x']['result_evidence_status'],'DECLARED_ARCHIVE_CONTRACT');self.assertFalse(r['independent_results_verified'])

    def test_runtime_patch_preserves_research_identity(self):
        from lab import common
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir()
            for name in ('paper_runner.py','history_policy.py'):(root/'lab'/name).write_text('# original',encoding='utf-8')
            with patch.object(common,'ROOT',root):
                research=common.source_fingerprint();execution=common.execution_fingerprint()
                (root/'lab/paper_runner.py').write_text('# repaired',encoding='utf-8')
                self.assertEqual(common.source_fingerprint(),research);self.assertNotEqual(common.execution_fingerprint(),execution)
                (root/'lab/history_policy.py').write_text('# changed policy',encoding='utf-8');self.assertNotEqual(common.source_fingerprint(),research)

    def test_runtime_mismatch_is_rejected_before_ledger_use(self):
        p,_=stream_fixture();p['execution_policy']['execution_code_hash']='wrong-runtime';p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):PaperDispatcher(Path(td)/'paper.db',p)
            self.assertFalse((Path(td)/'paper.db').exists())

    def test_stronger_ratio_is_not_automatically_lower_risk(self):
        m={'n':10,'net':10,'drawdown_match':2,'drawdown_day':2,'streak':2,'max_match_stake':1,'worst_match_net':-1}
        self.assertEqual(compare_roster_risk(m,{**m,'net':30,'drawdown_match':5})['status'],'RISK_TRADEOFF_OR_INSUFFICIENT_EVIDENCE')

    def test_complementary_research_is_separate_and_idempotent(self):
        from lab.complementarity import study_pairs
        events,labels,rules,_,_,_,_=portfolio_fixture(complement=True);available=direction_availability(events,labels,[r['direction'] for r in rules],DEFAULT)
        for r in rules:
            _,tags=research_gate_reasons(r['_trades'][4],metrics(r['_trades'][4],100,[2022,2023,2024]),[],DEFAULT,100,available[r['direction']])
            self.assertEqual(tags,['HIGH_RISK_ALTERNATIVE']);r['tags']=tags
        original=copy.deepcopy(rules)
        with tempfile.TemporaryDirectory() as td:
            r=study_pairs(td,rules,events,labels,DEFAULT,100);again=study_pairs(td,rules,events,labels,DEFAULT,100)
        self.assertEqual(r['status'],'PAIR_STUDY_COMPLETE');self.assertEqual(r['qualified_pairs'],1)
        self.assertEqual(r['best'],again['best']);self.assertEqual(rules,original)
        self.assertFalse(r['primary_roster_changed']);self.assertFalse(r['execution_handoff_verified'])

if __name__=='__main__':unittest.main()

"""Regressions for the 0.4.9 repairs: pre-goal rejection, fixed standard thresholds, numeric
minutes, categorical robustness, stream robustness, scale and accounting fixes."""
import json,tempfile,threading,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from lab.common import DEFAULT,atomic_json,check_config,write_jsonl,csv_write
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.selection import Quotes,neighbors
from lab.rules import matches,mask
from lab.standard_review import diagnostic_variants,needs_quote_loss_check,pregoal_disclosure,scope_years,direction_years
from lab.workflow_gates import completion_state
from lab.store import Store
from test_core import event


def goal_rows(close_ts=11,score_after=(1,0),phase=1):
    rows=[]
    def add(ts,valid=True,score=(0,0)):
        e=event(len(rows),line=10,w0=95,ts=ts,valid=valid,score0=score);e.update(sid='g',mid=0,market=0,phase=phase,status='滚' if phase else '即');rows.append(e)
    add(10);add(close_ts,valid=False,score=score_after);add(close_ts+1,score=score_after)
    return rows,{'g':{'eligible':True,'final':[3,0],'year':2024,'date':'2024-01-01','kickoff':0}}


class PregoalRejection(unittest.TestCase):
    def test_close_and_score_change_within_one_minute_is_rejected_without_replacement(self):
        rows,labels=goal_rows();q=Quotes(rows,labels,100,5,minute_close=True)
        self.assertIsNotNone(q.trade(0,0));self.assertIsNone(q.trade(0,0,reject_pregoal=True))
        self.assertEqual(q.trades([0],[0],reject_pregoal=True),[])
        base=q.trades([0],[0]);report=pregoal_disclosure(base,[],100,[2024])
        self.assertEqual(report['rejected_n'],1);self.assertEqual(report['rejected_net'],base[0]['pnl']/200);self.assertFalse(report['replacement_order'])

    def test_closure_without_score_change_later_closure_or_prematch_is_kept(self):
        for rows,labels in (goal_rows(score_after=(0,0)),goal_rows(close_ts=12),goal_rows(phase=0)):
            with self.subTest(rows=[(r['ts'],r['valid'],r['score'],r['phase']) for r in rows]):
                self.assertIsNotNone(Quotes(rows,labels,100,5,minute_close=True).trade(0,0,reject_pregoal=True))


class StandardThresholds(unittest.TestCase):
    def test_standard_rejects_each_relaxed_threshold_but_legacy_profiles_may_change_them(self):
        for key,value in (('min_profit','9'),('min_matches',39),('min_segment_share','0.1'),('min_return_drawdown_ratio','1'),('stress_min_profit','0'),('portfolio_min_return_drawdown_ratio','1'),('conservative_portfolio_min_return_drawdown_ratio','5')):
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,key):check_config({'research_objective':'validation',key:value})
        for key in ('min_year_matches','max_streak','max_drawdown','portfolio_max_drawdown','conservative_portfolio_max_drawdown'):
            with self.subTest(retired=key),self.assertRaisesRegex(ValueError,'已废弃'):check_config({key:1})
        self.assertEqual(check_config({'profile':'routine','min_matches':10})['min_matches'],10)

    def test_completion_gate_rejects_relaxed_standard_configuration(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'execution_economics':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)}}
        holdout={'status':'CONFIRMED','main':{'verdict':'CONFIRMED'}}
        self.assertEqual(completion_state(coverage,summary,trigger,{'status':'PASS'},True,DEFAULT,holdout=holdout)['state'],'STANDARD_HANDOFF_COMPLETE')
        relaxed=completion_state(coverage,summary,trigger,{'status':'PASS'},True,{**DEFAULT,'min_matches':10},holdout=holdout)
        self.assertEqual(relaxed['state'],'PARTIAL_RESULT');self.assertFalse(relaxed['gates']['standard_research_thresholds'])


class NumericMinutes(unittest.TestCase):
    def test_halftime_only_matches_its_explicit_equality(self):
        for a in ({'feature':'minute','op':'le','value':10},{'feature':'minute','op':'ge','value':-5},{'feature':'minute','op':'range','value':-3,'upper':5}):
            with self.subTest(a=a):
                self.assertFalse(matches(a,{'minute':-2}));self.assertTrue(matches(a,{'minute':0}))
                self.assertEqual(mask(a,{'minute':np.array([-2,0])}).tolist(),[False,True])
        self.assertTrue(matches({'feature':'minute','op':'eq','value':-2},{'minute':-2}))
        from lab.standard_masks import MaskStore
        self.assertIsNone(object.__new__(MaskStore)._scalar_words({'feature':'minute','op':'le','value':10}))

    def test_minute_neighbours_and_boundary_cases_stay_numeric(self):
        self.assertEqual([x['value'] for x in neighbors({'feature':'minute','op':'ge','value':0},100)],[5])
        self.assertEqual([(x['value'],x['upper']) for x in neighbors({'feature':'minute','op':'range','value':0,'upper':16},100)],[(5,21)])
        self.assertEqual(neighbors({'feature':'minute','op':'eq','value':-2},100),[])
        from lab.handoff_assets import scalar_cases
        cases={c['feature_value']:c['expected'] for c in scalar_cases({'feature':'minute','op':'le','value':10})}
        self.assertIs(cases[-2],False);self.assertIs(cases[10],True)
        for value,expected in cases.items():self.assertIs(matches({'feature':'minute','op':'le','value':10},{'minute':value}),expected)


class RobustnessChecks(unittest.TestCase):
    def test_categorical_condition_stays_fixed_and_gets_quote_loss_check(self):
        rule=[{'feature':'score_code','op':'eq','value':101},{'feature':'water','op':'ge','value':105}]
        self.assertTrue(needs_quote_loss_check(rule,100));self.assertFalse(needs_quote_loss_check(rule[1:],100))
        self.assertNotIn('joint',[mode for mode,_,_ in diagnostic_variants(rule,100)])
        joint=[cs for mode,_,cs in diagnostic_variants([*rule,{'feature':'line','op':'ge','value':4}],100) if mode=='joint']
        self.assertTrue(joint);self.assertTrue(all({'feature':'score_code','op':'eq','value':101} in cs for cs in joint))

    def test_yearly_coverage_uses_years_present_in_each_market_phase(self):
        labels={'a':{'eligible':True,'year':2024},'b':{'eligible':True,'year':2025}}
        pre={**event(0),'sid':'a','phase':0};live={**event(1),'sid':'b'}
        years=scope_years([pre,live],labels)
        self.assertEqual(direction_years(years,'PRE_GIVE'),[2024]);self.assertEqual(direction_years(years,'LIVE_GIVE'),[2025]);self.assertEqual(direction_years(years,'LIVE_OVER'),[])

    def test_unknown_quality_reason_excludes_by_default(self):
        from lab.data import inspect,load_inputs
        from test_v031 import quote,write_csv
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'selected';data.mkdir();write_csv(data/'完整指数_甲_2024.csv',[quote('new'),quote('note')])
            write_csv(root/'_build/handoff_current/exclude_sids.csv',[{'sId':'new','reason':'brand_new_upstream_problem','scope':'all','source':'x'},{'sId':'note','reason':'remaining_suspicious','scope':'all','source':'x'}],['sId','reason','scope','source'])
            _,labels,_,audit=load_inputs(inspect([str(data)],root/'work'),'真实甲组','皇冠')
            self.assertFalse(labels['new']['eligible']);self.assertTrue(labels['note']['eligible']);self.assertEqual(audit['counts']['quality_unknown_reason_excluded'],1)


class StreamAndSnapshots(unittest.TestCase):
    def test_bad_messages_are_rejected_and_the_stream_continues(self):
        from lab.standard_cli import paper_stream
        from test_paper_runner import standard_packet
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rules=root/'rules.json';atomic_json(rules,standard_packet())
            good={**event(0,line=8,ts=1),'market':0}
            lines=['not json',json.dumps({'type':'unknown'}),json.dumps({'type':'match_start','sid':'a'}),json.dumps({'type':'quote','event':good}),json.dumps({'type':'quote','event':{**good,'water':[70,85]}})]
            source=root/'in.jsonl';source.write_text('\n'.join(lines)+'\n',encoding='utf-8');output=root/'out.jsonl'
            result=paper_stream(rules,root/'ledger.db',str(source),str(output))
            rows=[json.loads(x) for x in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(result['rejected_messages'],3)
            self.assertEqual([r.get('status') for r in rows],['MESSAGE_REJECTED','MESSAGE_REJECTED','HISTORY_DECLARED_COMPLETE','PAPER_ONLY','MESSAGE_REJECTED'])

    def test_closed_match_leaves_only_a_tombstone_in_snapshots(self):
        from lab.features import SignalEngine
        from test_paper_runner import standard_packet
        p=standard_packet();engine=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy'])
        engine.mark_history_complete('a');engine.feed({**event(0,line=8,ts=1),'market':0});engine.close_match('a')
        snap=engine.snapshot()
        self.assertEqual((snap['history_ready'],snap['seen_scopes'],snap['fired'],snap['closed_matches']),([],[],[],['a']))
        with self.assertRaises(ValueError):engine.feed({**event(1,line=8,ts=2),'market':0})


class ScaleAndOperations(unittest.TestCase):
    def test_deep_preview_pages_match_sequential_rows(self):
        from lab.server import preview_page
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rows=[{'id':i,'text':'line\nbreak "quoted"' if i%7==0 else 'plain'} for i in range(1005)]
            csv_write(root/'t.csv',rows);write_jsonl(root/'t.jsonl.gz',rows)
            page,more=preview_page(root/'t.csv',1000);self.assertEqual([int(r['id']) for r in page],list(range(1000,1005)));self.assertFalse(more)
            self.assertEqual(preview_page(root/'t.csv',7,1)[0][0]['text'],'line\nbreak "quoted"')
            page,more=preview_page(root/'t.jsonl.gz',400);self.assertEqual(page[0]['id'],400);self.assertEqual(len(page),200);self.assertTrue(more)

    def test_job_listing_skips_manifests_and_job_keeps_only_its_catalog_row(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td)
            manifest={'files':[{'path':'a.csv','kind':'quotes','targets':[['A','皇冠']]},{'path':'b.csv','kind':'quotes','targets':[['B','皇冠']]}],'leagues':[{'league':'A','company':'皇冠'},{'league':'B','company':'皇冠'}]}
            jid=store.create('A',DEFAULT,manifest)
            self.assertNotIn('manifest',store.jobs()[0]);saved=store.get(jid)['manifest']
            self.assertEqual(saved['leagues'],[{'league':'A','company':'皇冠'}]);self.assertEqual(saved['excluded_files']['count'],1)

    def test_worker_blocked_by_workspace_lock_stays_queued(self):
        from lab.server import launch_next
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]})
            with patch('lab.server.subprocess.Popen',return_value=SimpleNamespace(poll=lambda:75)):launch_next(store,threading.Event())
            self.assertEqual(store.get(jid)['status'],'QUEUED')

    def test_partition_job_without_training_subset_never_reads_full_inputs(self):
        from lab.pipeline import run
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('A',{**DEFAULT,'research_partition':{'kind':'walk_forward_training','cutoff':'2024-12-31'}},{'files':[]})
            with patch('lab.pipeline.load_inputs',side_effect=AssertionError('full inputs were read')):
                with self.assertRaisesRegex(ValueError,'训练子集'):run(td,jid)
            self.assertEqual(store.get(jid)['status'],'ERROR')

    def test_final_decision_and_gap_notes_come_from_verified_facts(self):
        from lab.reporting import final_decision_markdown,gap_notes_markdown
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'results').mkdir()
            atomic_json(root/'config.json',DEFAULT);atomic_json(root/'data_audit.json',{'league':'合成','company':'皇冠'})
            atomic_json(root/'labels.json',{'a':{'eligible':True,'date':'2024-01-01'},'b':{'eligible':True,'date':'2025-02-01'}})
            atomic_json(root/'results/rules.json',{'rules':[{'direction':'LIVE_OVER'},{'direction':'PRE_GIVE'}]})
            prospective={'state':'PARTIAL_RESULT','gates':{'full_standard_search':False,'trigger':True}}
            text=final_decision_markdown(root,{'directions':[{'方向':'滚球大球','全局保留':1,'低风险备选':1,'资格数':2}]},{'remaining':5},{'status':'PASS','missing_signals':0,'extra_or_changed_signals':0,'order_differences':{'0':0}},prospective,{'status':'PASS'})
            for expected in ('主名单 2 条（赛前 1，滚球 1）','2024-01-01 至 2025-02-01','完整标准搜索','实盘批准 0 条'):self.assertIn(expected,text)
            self.assertIn('完整标准搜索',gap_notes_markdown(prospective,{'remaining':5}))

    def test_audit_package_lists_but_does_not_archive_rebuildable_direction_caches(self):
        import test_packaging_publication as publication
        fixture=publication.PackagingPublication();fixture.setUp()
        try:
            folder=fixture.root/'mining/LIVE_OVER';(folder/'review_columns').mkdir(parents=True)
            (folder/'masks.sqlite3').write_bytes(b'cache');(folder/'review_columns/col.npy').write_bytes(b'cache');(folder/'state.json').write_text('{}')
            packages,_=fixture.run_package()
            with zipfile.ZipFile(fixture.root/'packages'/packages['audit']) as z:names=z.namelist();policy=json.loads(z.read('audit_log_policy.json'))
            self.assertIn('mining/LIVE_OVER/state.json',names);self.assertNotIn('mining/LIVE_OVER/masks.sqlite3',names)
            self.assertEqual({x['path'] for x in policy['rebuildable_caches_not_archived']},{'mining/LIVE_OVER/masks.sqlite3','mining/LIVE_OVER/review_columns/col.npy'})
            with zipfile.ZipFile(fixture.root/'packages'/packages['developer']) as z:self.assertIn('results/最终决定.md',z.namelist())
        finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()

"""Delivery audit regressions: actual execution, corruption and boundary failures."""
import copy,csv,json,os,tempfile,unittest,zipfile,sqlite3
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
import numpy as np
from lab.common import DEFAULT,MISSING,atomic_json,read_json,sha,digest,write_jsonl,timestamp
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.data import inspect,load_inputs
from lab.features import SignalEngine,validate_rules
from lab.handoff_assets import write_assets,verify_scalar_cases
from lab.selection import Quotes,metrics
from lab.standard_review import select_standard
from lab.standard_atoms import AtomCatalog,W,C
from lab.standard_columns import StandardColumns
from lab.standard_masks import MaskStore
from lab.standard_search import run_class_search
from lab.mining import build_direction
from lab.workflow_gates import completion_state
from lab.workflow_journal import StageJournal
from lab.pool_diagnostics import all_pool_diagnostics
from lab.diagnostics import export_selected_diagnostics
from lab.store import Store
from lab.release import build_snapshot,release_fingerprint
from test_core import event,TEST_CONTRACT
from test_v031 import quote,write_csv
from test_paper_runner import standard_packet
import test_pool_diagnostics as pool_test
import test_packaging_publication as publication

def economic_fixture():
    rows=[];labels={}
    for mid in range(90):
        sid=f'economic-{mid:03d}';year=2024+mid//30;start=mid*10
        labels[sid]={'eligible':True,'final':[3,0] if mid%2==0 else [0,0], 'year':year,'date':f'{year}-01-01','kickoff':start}
        for offset,water in ((0,160),(0,20),(1,160),(2,160)):
            e=event(len(rows),line=10,w0=water,ts=start+offset);e.update(sid=sid,mid=mid,market=0);rows.append(e)
    return rows,labels

class DeliveryFixes(unittest.TestCase):
    def test_frozen_configuration_cannot_be_relaxed_for_old_roster_reverification(self):
        from lab.verify import verify_frozen_config
        packet={'execution_policy':{'quote_mapping':'minute_close_latest_v3','research_config_hash':digest(DEFAULT)}}
        verify_frozen_config(packet,DEFAULT)
        with self.assertRaisesRegex(ValueError,'冻结研究配置'):verify_frozen_config(packet,{**DEFAULT,'max_drawdown':'100'})
        with self.assertRaisesRegex(ValueError,'冻结研究配置'):verify_frozen_config({'execution_policy':{'quote_mapping':'minute_close_latest_v3'}},DEFAULT)

    def test_finalize_only_permits_summary_metadata_drift_not_contract_or_code(self):
        from validation.finalize_review_status import executable_manifest_matches
        original={'lab/data.py':'code','tests/test_core.py':'test','docs/机器规则与数据契约.md':'contract','本版实测与未完成项.md':'pending'}
        self.assertTrue(executable_manifest_matches({'source_manifest':original},{**original,'本版实测与未完成项.md':'actual results'}))
        for name in ('lab/data.py','tests/test_core.py','docs/机器规则与数据契约.md'):
            self.assertFalse(executable_manifest_matches({'source_manifest':original},{**original,name:'changed'}))

    def test_finalize_missing_or_old_proof_does_not_publish_status(self):
        import validation.finalize_review_status as publisher
        with tempfile.TemporaryDirectory() as td,patch.object(publisher,'ROOT',Path(td)):
            with self.assertRaisesRegex(ValueError,'隔离回归'):publisher.finalize(Path(td)/'evidence')
            atomic_json(Path(td)/'evidence/frozen_regression.json',{'status':'PASS','version':'0.4.7'})
            with self.assertRaisesRegex(ValueError,'隔离回归'):publisher.finalize(Path(td)/'evidence')
            self.assertFalse((Path(td)/'validation/本版实际验收状态.json').exists())

    def test_mechanical_pass_cannot_make_standard_verify_command_succeed(self):
        from lab.verify import verification_passed
        for economics in ('FAIL','NOT_VERIFIED'):
            self.assertFalse(verification_passed({'status':'PASS','execution_economics_required':True,'execution_economics':{'status':economics}}))
        self.assertTrue(verification_passed({'status':'PASS','execution_economics_required':True,'execution_economics':{'status':'PASS'}}))

    def test_economic_gate_independently_recomputes_bad_actual_quote_qualification(self):
        from lab.execution_economics import verify_economics
        from lab.selection import portfolio_metrics
        rows,labels=economic_fixture();quotes=Quotes(rows,labels,100,5,minute_close=True)
        signals={'one':[{'eid':i,'side':0} for i in range(0,len(rows),4)]}
        scenarios={}
        for i in range(4):
            trades=quotes.trades(range(0,len(rows),4),[0]*90,max(0,i-1),0 if i==0 else 5)
            scenarios[str(i)]={'metrics':portfolio_metrics(trades,100,[2024,2025,2026])}
        packet={'rules':[{'id':'one'}],'water_scale':100,'execution_policy':{'quote_mapping':'minute_close_latest_v3'}}
        report=verify_economics(packet,signals,quotes,labels,DEFAULT,{'scenarios':scenarios})
        self.assertEqual(report['status'],'FAIL');self.assertFalse(report['portfolio_within_frozen_risk'])
        self.assertEqual(report['rules'][0]['execution_metrics'][0]['net'],-36)

    def test_minute_close_preserves_legacy_instant_and_maps_latest_at_zero_delay(self):
        rows,labels=economic_fixture();ids=list(range(0,len(rows),4));sides=[0]*90
        instant=Quotes(rows,labels,100,5);actual=Quotes(rows,labels,100,5,minute_close=True)
        self.assertEqual(sum(t['pnl'] for t in instant.trades(ids,sides))/200,27)
        self.assertEqual(sum(t['pnl'] for t in actual.trades(ids,sides))/200,-36)

    def test_profitable_first_stage_pool_survives_but_bad_actual_execution_cannot_qualify(self):
        rows,labels=economic_fixture();cfg={**DEFAULT,'directions':['LIVE_OVER'],'bootstrap_repetitions':0}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'mining/LIVE_OVER';folder.mkdir(parents=True)
            atomic_json(root/'config.json',cfg);atomic_json(root/'labels.json',labels);write_jsonl(root/'events.jsonl.gz',rows)
            base=build_direction(rows,labels,100,'LIVE_OVER',cfg);np.savez(folder/'arrays.npz',**base)
            with AtomCatalog(folder/'atoms.sqlite3',True) as atoms:
                for v in (149,150,151):atoms.add({'feature':'water','op':'ge','value':v},W|C,100)
                atoms.commit()
                with StandardColumns(base,rows,'LIVE_OVER') as columns,MaskStore(folder,columns,atoms,cfg,'delivery-fixture') as masks:
                    masks.prepare(lambda **kw:None,lambda:False)
                    state=run_class_search(folder,atoms,masks,{**cfg,'_water_scale':100},lambda **kw:None,lambda:False,'delivery-fixture')
            self.assertGreater(state['candidates'],0)
            summary=select_standard(root,rows,labels,100,cfg,lambda **kw:None,lambda:False)
            self.assertEqual(summary['selected'],0);self.assertEqual(summary['backup_selected'],0)
            self.assertEqual(read_json(folder/'state.json')['candidates'],state['candidates'])
            with closing(sqlite3.connect(root/'results/standard_review.sqlite3')) as conn:
                family=json.loads(conn.execute('SELECT body FROM families LIMIT 1').fetchone()[0])
                self.assertEqual(family['execution_metrics']['net'],-36)
                self.assertEqual(family['status'],'BASIC_FAILED')

    def test_economic_failure_is_a_separate_mandatory_completion_gate(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)},'execution_economics':{'status':'FAIL'}}
        result=completion_state(coverage,summary,trigger,{'status':'PASS'},True)
        self.assertEqual(result['state'],'PARTIAL_RESULT');self.assertFalse(result['gates']['execution_economics'])

    def test_latest_closure_at_zero_delay_cannot_fall_back_to_trigger_quote(self):
        rows=[{**event(0,line=10,ts=10),'market':0},{**event(1,line=10,ts=10,valid=False),'market':0}]
        labels={'a':{'eligible':True,'final':[3,0],'year':2024,'date':'2024-01-01','kickoff':10}}
        self.assertIsNone(Quotes(rows,labels,100,5,minute_close=True).trade(0,0))

    def test_minute_close_same_phase_ignores_later_other_phase(self):
        pre={**event(0,line=10,w0=160,ts=10),'market':0,'phase':0,'status':'即'}
        live={**event(1,line=10,w0=20,ts=10),'market':0,'phase':1,'status':'滚'}
        labels={'a':{'eligible':True,'final':[3,0],'year':2024,'date':'2024-01-01','kickoff':10}}
        trade=Quotes([pre,live],labels,100,5,minute_close=True).trade(0,0)
        self.assertIsNotNone(trade);self.assertEqual(trade['eid'],0);self.assertEqual(trade['phase'],0);self.assertEqual(trade['water'],160)

    def test_stream_oracle_skips_when_no_same_phase_quote(self):
        from lab.paper_verification import verify_stream_orders
        from test_paper_runner import standard_packet
        packet=standard_packet()
        signals=[{'strategy_id':packet['rules'][0]['id'],'sid':'a','ts':5,'side':0,'line':8,'score':[0,0]}]
        report=verify_stream_orders(packet,signals,[])
        self.assertEqual(report['status'],'PASS');self.assertEqual(report['orders'],0)

    def test_mutation_after_loader_precheck_is_rejected_before_prepared_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';write_csv(source,[quote()]);man=inspect([str(source)],root/'cache')
            def progress(**kw):write_csv(source,[quote(),quote('new')])
            with self.assertRaisesRegex(ValueError,'读取内容|原始输入内容改变'):load_inputs(man,'真实甲组','皇冠',progress)

    def test_changed_then_restored_input_still_fails_actual_read_byte_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';rows=[quote(str(i)) for i in range(5000)]
            write_csv(source,rows);original=source.read_bytes();man=inspect([str(source)],root/'cache');changed=[{**r,'上水/大球':'0.85'} for r in rows]
            started=False;restored=False
            def progress(**kw):
                nonlocal started
                write_csv(source,changed);started=True
            def checkpoint():
                nonlocal restored
                if started and not restored:source.write_bytes(original);restored=True
                return False
            with self.assertRaisesRegex(ValueError,'实际读取内容'):load_inputs(man,'真实甲组','皇冠',progress,should_pause=checkpoint)
            self.assertTrue(restored);self.assertEqual(source.read_bytes(),original)

    def test_packaging_rejects_changed_raw_input_before_any_publication(self):
        fixture=publication.PackagingPublication();fixture.setUp()
        try:
            rec=read_json(fixture.root/'input_manifest.json')['files'][0];Path(rec['path']).write_text('changed')
            with self.assertRaisesRegex(ValueError,'原始输入内容改变'):fixture.run_package()
            fixture.assert_unpublished()
        finally:fixture.doCleanups()

    def test_index_date_conflict_is_preserved_and_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r=quote();write_csv(root/'quotes.csv',[r])
            write_csv(root/'index.csv',[{'sId':r['sId'],'联赛':r['联赛'],'全场比分':r['全场比分'],'状态':'完','日期':'2023-12-31'}],['sId','联赛','全场比分','状态','日期'])
            _,labels,_,audit=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertFalse(labels['one']['eligible']);self.assertIn('date',labels['one']['index_quote_conflicts'])
            self.assertEqual(audit['label_conflicts_by_field']['date'],1)

    def test_illegal_index_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()])
            write_csv(root/'index.csv',[{'sId':'one','联赛':'真实甲组','全场比分':'2-1','状态':'完','日期':'2024-99-99'}],['sId','联赛','全场比分','状态','日期'])
            with self.assertRaisesRegex(ValueError,'日期必须'):load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')

    def test_absent_optional_index_time_does_not_erase_known_quote_time(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()])
            write_csv(root/'index.csv',[{'sId':'one','联赛':'真实甲组','全场比分':'2-1','状态':'完'}],['sId','联赛','全场比分','状态'])
            _,labels,_,_=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertTrue(labels['one']['eligible']);self.assertNotEqual(labels['one']['kickoff'],MISSING)

    def test_blank_index_result_does_not_erase_complete_quote_result(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()])
            write_csv(root/'index.csv',[{'sId':'one','联赛':'真实甲组','全场比分':'','状态':'完'}],['sId','联赛','全场比分','状态'])
            _,labels,_,_=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertTrue(labels['one']['eligible']);self.assertEqual(labels['one']['final'],[2,1])

    def test_live_total_below_known_goals_is_quarantined(self):
        from lab.historical_pricing import minute_close_payoffs
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r=quote();r.update({'盘口类型':'大小球','盘口数值':'0.5','当时比分':'1-0','全场比分':'="1-0"'})
            write_csv(root/'quotes.csv',[r]);events,labels,scale,audit=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertTrue(events[0]['quality_blocked']);self.assertFalse(events[0]['valid'])
            self.assertEqual(audit['counts']['live_total_line_below_known_goals'],1)
            self.assertIsNone(Quotes(events,labels,scale,5,minute_close=True).trade(0,0,reject_pregoal=True))
            self.assertEqual(int(minute_close_payoffs(events,labels,scale)[0,0]),0)

    def test_score_change_with_unchanged_quote_is_blocked_until_price_refreshes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rows=[]
            for minute,score_text,line,water in ((9,'0-0','2.5','0.95'),(10,'1-0','2.5','0.95'),(11,'1-0','3.25','0.92')):
                r=quote();r.update({'盘口类型':'大小球','比赛分钟':str(minute),'当时比分':score_text,'盘口数值':line,'上水/大球':water,
                                    '变化时间':f'2024-03-01 20:{minute:02d}','全场比分':'="2-1"'});rows.append(r)
            write_csv(root/'quotes.csv',rows);events,labels,scale,audit=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertEqual([e['valid'] for e in events],[True,False,True])
            self.assertEqual([e['quality_blocked'] for e in events],[False,True,False])
            self.assertEqual(audit['counts']['live_score_change_quote_unchanged'],1)
            self.assertIsNone(Quotes(events,labels,scale,5,minute_close=True).trade(events[1]['eid'],0,reject_pregoal=True))

    def test_consistent_duplicate_index_with_missing_optional_fields_is_not_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()])
            common={'sId':'one','联赛':'真实甲组','全场比分':'2-1','状态':'完'}
            write_csv(root/'index1.csv',[{**common,'日期':'2024-03-01'}],['sId','联赛','全场比分','状态','日期'])
            write_csv(root/'index2.csv',[common],['sId','联赛','全场比分','状态'])
            _,labels,_,_=load_inputs(inspect([str(root)],root/'cache'),'真实甲组','皇冠')
            self.assertTrue(labels['one']['eligible']);self.assertEqual(labels['one']['date'],'2024-03-01')

    def test_inspect_rejects_illegal_index_date(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()])
            write_csv(root/'index.csv',[{'sId':'one','联赛':'真实甲组','全场比分':'2-1','状态':'完','日期':'2024-99-99'}],['sId','联赛','全场比分','状态','日期'])
            with self.assertRaisesRegex(ValueError,'日期必须'):inspect([str(root)],root/'cache')

    def test_projection_length_tokens_and_composite_mismatch_are_rejected(self):
        for a in ({'feature':'path3_keep','op':'eq','value':1,'sequence':'subsequence'},
                  {'feature':'path2_keep','op':'eq','value':19},
                  {'feature':'path1_keep','op':'eq','value':1,'pattern':[1]},
                  {'feature':'path1_keep','op':'eq','value':13,'event_model':'composite','pattern':[13]},
                  {'feature':'water','op':'ge','value':90,'pulse':False},
                  {'feature':'path3_keep','op':'eq','value':0,'event_model':'composite','pattern':[13]}):
            with self.subTest(a=a),self.assertRaises(ValueError):validate_rules([{'id':'bad','direction':'LIVE_GIVE','conditions':[a]}])

    def test_current_roster_scalar_and_path_boundaries_are_bound_and_complete(self):
        packet=standard_packet();packet['water_scale']=100;packet['rules'][0]['conditions']=[{'feature':'water','op':'ge','value':90},{'feature':'path2_keep','op':'eq','value':13,'sequence':'subsequence','span':5,'min_line_step':2,'min_water_step':5}]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);res=root/'results';res.mkdir();atomic_json(res/'rules.json',packet);write_assets(root)
            result=verify_scalar_cases(res);self.assertGreater(result['scalar_boundaries'],0);self.assertGreater(result['path_boundaries'],0)
            atomic_json(res/'boundary_cases.json',{'rules':[]})
            with self.assertRaisesRegex(ValueError,'覆盖'):verify_scalar_cases(res)

    def test_stale_boundary_condition_cannot_claim_pass(self):
        packet=standard_packet();packet['water_scale']=100;packet['rules'][0]['conditions']=[{'feature':'water','op':'ge','value':90}]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);res=root/'results';res.mkdir();atomic_json(res/'rules.json',packet);write_assets(root)
            packet['rules'][0]['conditions'][0]['value']=95;atomic_json(res/'rules.json',packet)
            with self.assertRaisesRegex(ValueError,'绑定'):verify_scalar_cases(res)

    def test_missing_and_same_size_corrupt_pair_csv_fail_closed(self):
        population,events,labels=pool_test.PoolDiagnostics().fixture()
        for remove in (True,False):
            with self.subTest(remove=remove),tempfile.TemporaryDirectory() as td:
                root=Path(td);all_pool_diagnostics(root,population,events,labels,DEFAULT,lambda **kw:None,lambda:False)
                p=root/'全池重叠与共同亏损.csv'
                if remove:p.unlink()
                else:p.write_bytes(b'\0'*p.stat().st_size)
                with self.assertRaises(ValueError):all_pool_diagnostics(root,population,events,labels,DEFAULT,lambda **kw:None,lambda:False)

    def test_same_shape_corrupt_matrix_is_not_reused(self):
        population,events,labels=pool_test.PoolDiagnostics().fixture()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);all_pool_diagnostics(root,population,events,labels,DEFAULT,lambda **kw:None,lambda:False)
            path=root/'pnl.npy';matrix=np.load(path);matrix[0,0]+=1;np.save(path,matrix)
            with self.assertRaisesRegex(ValueError,'矩阵内容改变'):all_pool_diagnostics(root,population,events,labels,DEFAULT,lambda **kw:None,lambda:False)

    def test_source_observability_change_invalidates_risk_matrix_binding(self):
        population,events,labels=pool_test.PoolDiagnostics().fixture()
        with tempfile.TemporaryDirectory() as td:
            all_pool_diagnostics(td,population,events,labels,DEFAULT,lambda **kw:None,lambda:False)
            with self.assertRaisesRegex(ValueError,'范围改变'):all_pool_diagnostics(td,population,events[1:],labels,DEFAULT,lambda **kw:None,lambda:False)

    def test_missing_market_observation_does_not_manufacture_negative_correlation(self):
        a={'id':'a','direction':'LIVE_GIVE','_trades':[[{'sid':'a','market':1,'phase':1,'eid':0,'side':0,'line':2,'water':95,'ts':1,'pnl':190}]]}
        b={'id':'b','direction':'LIVE_OVER','_trades':[[{'sid':'b','market':0,'phase':1,'eid':1,'side':0,'line':10,'water':95,'ts':2,'pnl':190}]]}
        events=[{**event(0),'sid':'a'},{**event(1),'sid':'b','market':0}]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);export_selected_diagnostics(root,[a],[b],100,[2024],events)
            with (root/'主备名单重叠与共同亏损.csv').open(encoding='utf-8-sig',newline='') as f:row=next(csv.DictReader(f))
            self.assertEqual(row['活跃并集收益相关'],'');self.assertEqual(row['共同可观察比赛'],'0')

    def test_recover_stale_snapshot_cannot_overwrite_committed_done(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('race',DEFAULT,{'files':[]},True);store.update(jid,status='RUNNING',pid=12345)
            with store.conn() as conn:conn.execute('INSERT OR REPLACE INTO worker_identity VALUES(?,?,?)',(jid,12345,'birth'))
            def exited(pid):store.update(jid,status='DONE',pid=0);raise OSError('exited')
            with patch('lab.store.process_birth',side_effect=exited):store.recover()
            self.assertEqual(store.get(jid)['status'],'DONE')

    def test_snapshot_and_runtime_fingerprints_use_identical_relative_order(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'source';(root/'lab').mkdir(parents=True);(root/'lab/common.py').write_text("VERSION='test'\n")
            (root/'README_先读.md').write_text('read');(root/'app.py').write_text('')
            target=Path(td)/'source.zip';result=build_snapshot(target,root)
            self.assertEqual(result['release_fingerprint'],release_fingerprint(root))

    def test_unknown_business_configuration_cannot_silently_use_default(self):
        from lab.common import check_config
        with self.assertRaisesRegex(ValueError,'未知配置'):check_config({'max_drawdwon':'1'})
        self.assertEqual(check_config({'research_partition':{'kind':'training'}})['research_partition']['kind'],'training')

    def test_pause_closes_active_stage_and_records_resumption_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            journal=StageJournal(td,'binding');journal.start('S1');journal.pause('user')
            row=read_json(journal.path)['stages']['S1'];self.assertEqual(row['status'],'PARTIAL');self.assertIsNotNone(row['finished']);self.assertEqual(row['pause_events'][0]['reason'],'user')
            journal.start('S1');self.assertEqual(journal.data['stages']['S1']['status'],'RUNNING')

    def test_minus_002_quality_column_does_not_read_pregoal_trades(self):
        from lab.standard_review import priced_stress_trades
        major=[[{'quality':1}],[{'quality':0}],[{'quality':0}],[{'quality':0}],[{'quality':9}]]
        trades=priced_stress_trades(major,[{'quality':2}])
        self.assertEqual([sum(t['quality'] for t in row) for row in trades],[1,0,0,0,2,9])

    def test_catalog_version_invalidates_pre_date_check_cache(self):
        from lab.catalog import CATALOG_VERSION
        self.assertEqual(CATALOG_VERSION,'FSL_csv_catalog_v7')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote()]);man=inspect([str(root)],root/'cache')
            self.assertEqual(man['catalog_version'],CATALOG_VERSION)

    def test_worker_unclaimed_queued_job_exits_75(self):
        from lab import cli
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('t',DEFAULT,{'files':[]})
            with patch.object(cli.sys,'argv',['app.py','worker','--workspace',str(store.root),'--job',jid]),patch('lab.pipeline.run',return_value=None):
                self.assertEqual(cli.main(),75)
            self.assertEqual(store.get(jid)['status'],'QUEUED')

    def test_paper_scenario_4_uses_pregoal_rejection_not_delay(self):
        from lab.paper_verification import verify_paper_orders
        from test_repair_049 import goal_rows
        rows,labels=goal_rows();quotes=Quotes(rows,labels,100,5,minute_close=True)
        packet=standard_packet();packet['water_scale']=100
        rule=packet['rules'][0];e=rows[0]
        signals=[{'strategy_id':rule['id'],'direction':rule['direction'],'sid':e['sid'],'eid':e['eid'],'event_key':e['event_key'],'ts':e['ts'],'side':0,'line':e['line'],'water':e['water'][0],'score':e['score']}]
        empty=verify_paper_orders(packet,signals,rows,quotes,4,[])
        filled=verify_paper_orders(packet,signals,rows,quotes,0,[])
        self.assertEqual(empty['status'],'PASS');self.assertEqual(empty['orders'],0)
        self.assertEqual(filled['status'],'FAIL');self.assertGreater(filled['orders'],0)

    def test_failed_pregoal_paper_gate_blocks_persistent_paper(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        paper={str(i):{'status':'PASS'} for i in range(4)};paper['4']={'status':'FAIL'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':paper,'execution_economics':{'status':'PASS'}}
        self.assertFalse(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['gates']['persistent_paper'])

    def test_standard_scope_without_pregoal_policy_is_rejected(self):
        from lab.verify import verify_frozen_config
        from lab.execution_economics import verify_economics
        with self.assertRaisesRegex(ValueError,'进球前拒单'):
            verify_frozen_config({'scope':'FSL_STANDARD_V3_2','execution_policy':{'quote_mapping':'minute_close_latest_v3','research_config_hash':digest(DEFAULT)}},DEFAULT)
        packet={'scope':'FSL_STANDARD_V3_2','rules':[{'id':'one'}],'water_scale':100,'execution_policy':{'quote_mapping':'minute_close_latest_v3'}}
        self.assertEqual(verify_economics(packet,{'one':[]},None,{},DEFAULT,{'scenarios':{}})['status'],'FAIL')

    def test_economics_requires_handoff_s4_when_pregoal_policy(self):
        from lab.execution_economics import verify_economics
        from lab.selection import portfolio_metrics,dispatch,PREGOAL_REJECTION_POLICY
        rows,labels=economic_fixture();quotes=Quotes(rows,labels,100,5,minute_close=True)
        signals={'one':[{'eid':i,'side':0} for i in range(0,len(rows),4)]}
        scenarios={}
        for i in range(4):
            trades=quotes.trades(range(0,len(rows),4),[0]*90,max(0,i-1),0 if i==0 else 5)
            scenarios[str(i)]={'metrics':portfolio_metrics(trades,100,[2024,2025,2026])}
        packet={'rules':[{'id':'one','direction':'LIVE_OVER'}],'water_scale':100,
                'execution_policy':{'quote_mapping':'minute_close_latest_v3','pregoal_rejection':PREGOAL_REJECTION_POLICY,'match_cap':4,'priority':'strategy_id_lexical'}}
        missing=verify_economics(packet,signals,quotes,labels,DEFAULT,{'scenarios':scenarios})
        self.assertEqual(missing['status'],'FAIL');self.assertFalse(missing['portfolio_within_frozen_risk'])
        kept=quotes.trades(range(0,len(rows),4),[0]*90,reject_pregoal=True)
        orders,_=dispatch([{'id':'one','direction':'LIVE_OVER','_trades':[kept]}],0,4)
        scenarios['4']={'metrics':portfolio_metrics(orders,100,[2024,2025,2026])}
        locked=verify_economics(packet,signals,quotes,labels,DEFAULT,{'scenarios':scenarios})
        self.assertEqual(locked['priced_scenario_count'],5);self.assertIsNotNone(locked['pregoal_rejection_portfolio'])

    def test_paper_s4_pass_requires_priced_stream_s4(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        paper={str(i):{'status':'PASS'} for i in range(5)}
        stream={'status':'PASS','scenarios':{str(i):{'status':'PASS'} for i in range(4)}}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'streaming_paper_execution':stream,'persistent_paper_execution':paper,'execution_economics':{'status':'PASS'}}
        self.assertFalse(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['gates']['streaming_paper'])
        stream['scenarios']['4']={'status':'PASS'}
        self.assertTrue(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['gates']['streaming_paper'])

if __name__=='__main__':unittest.main()

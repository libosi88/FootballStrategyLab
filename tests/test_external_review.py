"""Behavioral regressions for independently checked external review findings."""
import csv,tempfile,unittest,copy,json,zipfile,os,threading,subprocess,sys,re,time
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from pathlib import Path
from lab.common import DEFAULT,atomic_json,read_json,csv_write,sha
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.selection import build_alternative_comparisons,portfolio_metrics
from lab.portfolio_search import PortfolioSearch
from lab.data import inspect,load_inputs
from lab.store import Store
from lab.walk_forward import run_walk_forward
from lab.research_validation import adapt_packet_scale,evaluate_frozen
from test_v031 import quote,write_csv
from test_paper_runner import standard_packet

class ExternalReview(unittest.TestCase):
    def test_alternatives_rank_full_pool_then_score_only_final_four(self):
        def candidate(name,net,n=100):
            return {'id':name,'direction':'LIVE_GIVE','signature':name,'conditions':[],
                    'metrics':{'n':n,'year_counts':{'2024':n},'streak':2,'drawdown_i':200,'drawdown':1},
                    'stress':[{'net_i':net}]*3,'_trades':[[{'sid':name}]]}
        current=candidate('current',2000);pool=[candidate(f'alt{i}',3000+i) for i in range(20)]
        pool.append(candidate('oversized',100000,1000));calls=[]
        def scorer(rules):
            calls.append([r['id'] for r in rules]);m={'net_i':5000,'drawdown_match_i':100}
            return {'raw':m,'stress':[m]*3}
        rows=build_alternative_comparisons([('默认',[current])],pool,DEFAULT,100,scorer)
        self.assertEqual(len(rows),4);self.assertEqual(len(calls),4)
        best=next(r for r in rows if r['比较指标']=='最差压力净胜')
        self.assertEqual(best['替代ID'],'alt19');self.assertFalse(any('oversized' in r for r in calls))

    def test_raw_loss_cannot_be_hidden_by_profitable_delay_scenarios(self):
        pool=[{'id':'bad','conditions':[],'_trades':[[]]*4}]
        def scorer(rules):
            base={'net_i':-200 if rules else 0,'drawdown_match_i':0,'matches':80 if rules else 0}
            return {'raw':base,'stress':[{**base,'net_i':4000 if rules else 0}]*3}
        with tempfile.TemporaryDirectory() as td:
            search=PortfolioSearch(pool,scorer,DEFAULT,100,td,'test','10')
            self.assertEqual(search.score(['bad'])['min_profit_i'],-200)
            result=search.run();self.assertEqual(result['chosen'],[]);self.assertEqual(result['singleton_evaluations'],1)

    def test_daily_curve_does_not_replace_intraday_match_risk(self):
        orders=[{'sid':str(i),'sort_time':100+i,'ts':100+i,'eid':i,'pnl':p,'year':2024,'water':100,'stake':1} for i,p in enumerate((-200,200))]
        result=portfolio_metrics(orders,100,[2024])
        self.assertEqual(result['drawdown_match_i'],200);self.assertEqual(result['drawdown_day_i'],0)
        self.assertEqual(result['drawdown_risk_basis'],'match_order')

    def test_prematch_nonzero_score_is_preserved_and_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';r=quote('bad');r.update({'状态':'早','阶段':'赛前','当时比分':'1-0'})
            write_csv(source,[r]);before=sha(source);man=inspect([str(source)],root/'cache')
            events,labels,_,audit=load_inputs(man,'真实甲组','皇冠')
            self.assertEqual(events[0]['score'],[1,0]);self.assertFalse(labels['bad']['eligible'])
            self.assertEqual(audit['counts']['prematch_nonzero_score'],1);self.assertEqual(sha(source),before)

    def test_missing_final_is_not_counted_as_score_exceeding_final(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';r=quote('missing');r['全场比分']='';write_csv(source,[r])
            _,_,_,audit=load_inputs(inspect([str(source)],root/'cache'),'真实甲组','皇冠')
            self.assertEqual(audit['counts'].get('score_exceeds_unverified_final',0),0)

    def test_quality_scope_typo_fails_at_scan_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);quality=root/'exclude_sids.csv';source=root/'quotes.csv';write_csv(source,[quote('one')])
            write_csv(quality,[{'sId':'one','scope':'all_crowm','reason':'pending_score_verify','source':'source'}],['sId','scope','reason','source'])
            with self.assertRaisesRegex(ValueError,'未知质量'):inspect([str(root)],root/'cache')
            with self.assertRaisesRegex(ValueError,'未知质量'):load_inputs({'files':[{'kind':'quality','path':str(quality),'sha256':sha(quality)}]},'真实甲组','皇冠')

    def test_discovered_quality_is_not_duplicated_and_has_complete_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_csv(root/'quotes.csv',[quote('one')]);q=root/'_build/handoff_current/exclude_sids.csv'
            write_csv(q,[{'sId':'one','scope':'all','reason':'pending_score_verify','source':'source'}],['sId','scope','reason','source'])
            man=inspect([str(root)],root/'cache');self.assertEqual(len(man['quality']['files']),1)
            record=next(r for r in man['files'] if r['kind']=='quality');self.assertTrue({'columns','size_bytes','joined_results'}<=record.keys())

    def test_month_filename_does_not_become_a_league_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'完整指数_2024-01.csv';write_csv(source,[quote('one')]);man=inspect([str(source)],root/'cache')
            self.assertEqual(man['leagues'][0]['file_groups'],['真实甲组'])

    def test_numeric_negative_strings_roundtrip_but_formulas_are_escaped(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'out.csv';csv_write(path,[{'integer':'-1','decimal':'-0.005','formula':'-SUM(A1:A2)','equals':'=2+3'}])
            with path.open(encoding='utf-8-sig',newline='') as f:row=next(csv.DictReader(f))
            self.assertEqual((row['integer'],row['decimal']),('-1','-0.005'));self.assertTrue(row['formula'].startswith("'"));self.assertTrue(row['equals'].startswith("'"))

    def test_non_grid_money_thresholds_have_exact_integer_comparisons(self):
        from lab.common import money_threshold,MISSING
        self.assertEqual(money_threshold('5.001',200),1000)
        self.assertLessEqual(1000,money_threshold('5.001',200));self.assertGreater(1001,money_threshold('5.001',200))
        self.assertEqual(money_threshold('10.000001',200),2000)
        with self.assertRaises(ValueError):money_threshold('1e100',200)
        with self.assertRaises(ValueError):money_threshold('NaN',200)

    def test_real_date_conflicts_remain_blocked_with_specific_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';a=quote('same');b={**a,'日期':'2024-03-02'};write_csv(source,[a,b])
            _,labels,_,audit=load_inputs(inspect([str(source)],root/'cache'),'真实甲组','皇冠')
            self.assertFalse(labels['same']['eligible']);self.assertEqual(audit['label_conflicts_by_field']['date'],1)

    def test_rescaled_historical_integer_metrics_keep_their_value(self):
        packet=standard_packet();packet['water_scale']=100
        packet['rules'][0]['metrics']={'net_i':1500,'drawdown_i':200,'denominator':200,'net':7.5,'n':80}
        result=adapt_packet_scale(packet,1000);m=result['rules'][0]['metrics']
        self.assertEqual((m['net_i'],m['drawdown_i'],m['denominator'],m['n']),(15000,2000,2000,80));self.assertEqual(m['net'],7.5)

    def test_evaluation_rejects_policy_override(self):
        packet=standard_packet();packet['water_scale']=100
        with self.assertRaisesRegex(ValueError,'政策冲突'):evaluate_frozen(packet,[],{},100,{**DEFAULT,'match_cap':6})

    def test_walk_forward_detects_label_change_before_creating_any_fold(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]},True);root=store.root/jid
            atomic_json(root/'labels.json',{'a':{'eligible':True,'date':'2024-01-01'}});atomic_json(root/'prepared_manifest.json',{'labels.json':sha(root/'labels.json')})
            atomic_json(root/'labels.json',{'a':{'eligible':True,'date':'2025-01-01'}})
            with self.assertRaisesRegex(ValueError,'标签与预处理'):run_walk_forward(td,jid,[{'cutoff':'2024-12-31','test_end':'2025-12-31'}])
            self.assertFalse((root/'walk_forward').exists())

    def test_the_four_reported_test_modules_are_discovered(self):
        import importlib
        counts={name:unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(name)).countTestCases() for name in ('test_core','test_v03','test_sparse','test_release')}
        # Discovery must find them; exact counts are not frozen, so adding a test does not break this.
        for name,minimum in {'test_core':17,'test_v03':22,'test_sparse':3,'test_release':6}.items():self.assertGreaterEqual(counts[name],minimum,name)

    def test_legal_quarter_lines_have_only_five_outcomes(self):
        from lab.common import settlement
        for scale,water in ((100,95),(1000,955)):
            allowed={2*water,water,0,-scale,-2*scale}
            for line in range(-48,49):
                for margin in range(-15,16):
                    for side in (0,1):self.assertIn(settlement(line,water,margin,side,scale),allowed)

    def test_global_and_mask_upper_bounds_agree_at_profit_threshold(self):
        from lab.standard_masks import mask_economics
        from lab.standard_atoms import AtomCatalog,W,C
        from lab.global_bound import prove_global_bound
        words=np.array([3,3],np.uint64);ends=np.array([1,2],np.int64);pnl=np.zeros(128,np.int64)
        pnl[0]=-200;pnl[1]=1500;pnl[65]=500
        self.assertEqual(mask_economics(words,ends,pnl)[2],2000)
        with tempfile.TemporaryDirectory() as td,AtomCatalog(Path(td)/'atoms.sqlite3',True) as atoms:
            atoms.add({'feature':'water','op':'ge','value':90},W|C,100);atoms.commit()
            base={'mid':np.array([0,0,1,1]),'pnl':np.array([-200,1500,0,500])}
            result=prove_global_bound(td,base,atoms,DEFAULT,100,'test',{})
            self.assertEqual(result['global_upper_i'],2000);self.assertEqual(result['candidates'],0)
            base['pnl'][1]+=1;self.assertIsNone(prove_global_bound(td,base,atoms,DEFAULT,100,'test',{}))

    def test_nonprofitable_review_rows_still_respond_to_pause(self):
        from lab.selection import select
        from lab.mining import Paused
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'mining/LIVE_OVER';folder.mkdir(parents=True)
            atomic_json(folder/'dictionary.json',{'atoms':[{'feature':'water','op':'ge','value':90,'label':'water'}]})
            atomic_json(folder/'state.json',{'parts':[{'file':'part.npz'}]})
            np.savez(folder/'arrays.npz',eid=np.array([0]));np.savez(folder/'part.npz',conditions=np.full((300,3),-1),metrics=np.tile([0,-1],(300,1)))
            calls=[0]
            def pause():calls[0]+=1;return calls[0]>=3
            with patch('lab.selection.finish_selection',side_effect=AssertionError('pause was skipped')):
                with self.assertRaises(Paused):select(root,[],{},100,{**DEFAULT,'profile':'smoke','directions':['LIVE_OVER']},lambda **kw:None,pause)

    def test_zip_cache_cannot_mix_leftover_csv(self):
        from lab.data import input_paths
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);archive=root/'input.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('one.csv','a,b\n1,2\n')
            first=input_paths([str(archive)],root/'cache');self.assertEqual(len(first),1)
            (first[0].parent/'injected.csv').write_text('not in original archive')
            again=input_paths([str(archive)],root/'cache');self.assertEqual([p.name for p in again],['one.csv'])
            self.assertEqual(again[0].parent.name,sha(archive));self.assertTrue(list((root/'cache/unpacked').glob('*.invalid_*')))

    def test_audit_import_does_not_need_old_drive_and_stays_paused(self):
        from lab.migration import import_audit_bundle
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'quotes.csv';write_csv(source,[quote('one')]);h=sha(source);archive=root/'audit.zip'
            manifest={'files':[{'path':r'Z:\nonexistent\quotes.csv','sha256':h,'kind':'quotes'}]}
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('input_manifest.json',json.dumps(manifest));z.writestr('config.json',json.dumps(DEFAULT));z.writestr('data_audit.json',json.dumps({'league':'真实甲组','company':'皇冠'}));z.write(source,'original_inputs/'+h[:12]+'_quotes.csv')
            result=import_audit_bundle(archive,root/'work');self.assertEqual(result['status'],'PAUSED');self.assertFalse(result['automatic_research_started'])
            repeat=import_audit_bundle(archive,root/'work');self.assertEqual(repeat['job'],result['job'])
            self.assertEqual(len(Store(root/'work').jobs()),1)

    def test_source_snapshot_excludes_machine_state(self):
        from lab.release import build_snapshot
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'project';(root/'lab').mkdir(parents=True);(root/'lab/common.py').write_text("VERSION='test'\n")
            for name in ('.venv/secret.txt','workspace/private.csv','runtime/python.exe','validation/old.json','lab/__pycache__/old.pyc'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('excluded')
            target=Path(td)/'source.zip';build_snapshot(target,root)
            with zipfile.ZipFile(target) as z:self.assertEqual(set(z.namelist()),{'lab/common.py','release_manifest.json'})
            with self.assertRaises(FileExistsError):build_snapshot(target,root)

    def test_empty_roster_cannot_hide_corrupted_nonempty_golden_signals(self):
        from lab.common import write_jsonl,digest
        from lab.verify import verify
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);res=root/'results';res.mkdir();packet=standard_packet();packet['rules']=[];packet['water_scale']=100
            packet['roster_hash']=digest({k:packet[k] for k in ('rules','contract','execution_policy')})
            atomic_json(res/'rules.json',packet);atomic_json(root/'config.json',DEFAULT);atomic_json(root/'labels.json',{});write_jsonl(root/'events.jsonl.gz',[])
            write_jsonl(res/'golden_signals.jsonl.gz',[{'strategy_id':'unexpected','direction':'LIVE_OVER','sid':'a','eid':0,'event_key':'source:2','ts':1,'side':0,'line':8,'water':95,'score':[0,0]}])
            for scenario in range(4):write_jsonl(res/f'orders_s{scenario}.jsonl.gz',[])
            # Supply the assets the production pipeline writes before verification;
            # the negative case here is the corrupted golden signal, not missing assets.
            from lab.handoff_assets import write_assets
            write_assets(root)
            result=verify(root);self.assertEqual(result['status'],'FAIL');self.assertEqual(result['prefix_causality'],'NOT_APPLICABLE_EMPTY_INPUT')

    def test_output_size_sampler_counts_growth_and_can_pause(self):
        from lab.resources import sample_output_size
        from lab.mining import Paused
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'live.sqlite3';p.write_bytes(b'a'*10)
            self.assertEqual(sample_output_size(root)['generated_bytes'],10);p.write_bytes(b'a'*35)
            self.assertEqual(sample_output_size(root)['generated_bytes'],35)
            with self.assertRaises(Paused):sample_output_size(root,lambda:True)

    def test_two_launchers_do_not_start_two_computations(self):
        from concurrent.futures import ThreadPoolExecutor
        from lab.server import launch_next
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]});calls=[]
            def start(*args,**kwargs):calls.append(args);store.claim(jid,os.getpid());return SimpleNamespace(poll=lambda:None)
            with patch('lab.server.subprocess.Popen',side_effect=start),ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:launch_next(Store(td),threading.Event()),range(2)))
            self.assertEqual(len(calls),1);self.assertEqual(len(list((store.root/jid).glob('worker_*.log'))),1)

    def test_early_worker_exit_is_removed_from_queue(self):
        from lab.server import launch_next
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]})
            with patch('lab.server.subprocess.Popen',return_value=SimpleNamespace(poll=lambda:1)):launch_next(store,threading.Event())
            self.assertEqual(store.get(jid)['status'],'ERROR')

    def test_zero_exit_while_queued_stays_queued(self):
        from lab.server import launch_next
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]})
            with patch('lab.server.subprocess.Popen',return_value=SimpleNamespace(poll=lambda:0)):launch_next(store,threading.Event())
            self.assertEqual(store.get(jid)['status'],'QUEUED')

class HttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lab.common import ROOT
        cls.temp=tempfile.TemporaryDirectory();cls.workspace=Path(cls.temp.name)/'workspace'
        cls.jid=Store(cls.workspace).create('HTTP fixture',DEFAULT,{'files':[]},True)
        env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'}
        cls.process=subprocess.Popen([sys.executable,'-B','app.py','serve','--no-browser','--port','0','--workspace',str(cls.workspace)],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
        line=cls.process.stdout.readline();match=re.search(r'http://127\.0\.0\.1:\d+',line)
        if not match:raise RuntimeError('Test interface failed to start: '+line)
        cls.base=match.group(0)
        from urllib.request import urlopen
        with urlopen(cls.base) as response:html=response.read().decode()
        cls.token=re.search(r'window.LOCAL_TOKEN="([^"]+)"',html).group(1)

    @classmethod
    def request(cls,path,body=None,auth=True,headers=None):
        from urllib.request import urlopen,Request
        values={'Content-Type':'application/json',**({'X-Local-Token':cls.token} if auth else {}),**(headers or {})}
        with urlopen(Request(cls.base+path,data=json.dumps(body).encode() if body is not None else None,headers=values),timeout=5) as response:return response.read()

    @classmethod
    def tearDownClass(cls):
        cls.request('/api/shutdown',{});cls.process.wait(timeout=10);cls.process.stdout.close();cls.temp.cleanup()

    def test_public_identity_has_no_machine_paths(self):
        from lab.release import workspace_identity
        value=json.loads(self.request('/api/identity',auth=False))
        self.assertNotIn('workspace',value);self.assertNotIn('source_root',value);self.assertEqual(value['workspace_id'],workspace_identity(self.workspace))

    def test_download_ticket_is_single_use_and_master_token_not_in_url(self):
        from urllib.error import HTTPError
        result=json.loads(self.request('/api/download-ticket',{'job':self.jid,'path':'config.json'}))
        self.assertNotIn(self.token,result['url']);self.assertGreater(result['expires_at'],time.time())
        self.assertIn('profile',json.loads(self.request(result['url'],auth=False)))
        with self.assertRaises(HTTPError) as error:self.request(result['url'],auth=False)
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError):self.request(f'/download/{self.jid}/config.json?token={self.token}',auth=False)

    def test_identity_origin_and_download_path_boundaries(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError):self.request('/api/identity',auth=False,headers={'Origin':'https://untrusted.example'})
        with self.assertRaises(HTTPError):self.request('/api/download-ticket',{'job':self.jid,'path':'../../outside.txt'})

if __name__=='__main__':unittest.main()

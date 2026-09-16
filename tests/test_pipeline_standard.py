"""Explicit slow S0-S7 acceptance; excluded from nested engine self-tests."""
import os,tempfile,unittest,sqlite3,subprocess
from datetime import datetime,timedelta
from pathlib import Path
from contextlib import nullcontext,closing
from lab.common import DEFAULT,read_json,sha,atomic_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.data import inspect
from lab.store import Store
from lab.pipeline import run
from test_v031 import quote,write_csv

def nonempty_rows():
    league='合成90场16分支有限验收（非真实比赛）';rows=[]
    for year in (2022,2023,2024):
        for match in range(30):
            kickoff=datetime(year,8,1,19,30)+timedelta(days=match)
            common=quote(f'synthetic-{year}-{match}',league=league)
            common.update({'日期':kickoff.strftime('%Y-%m-%d'),'开球时间':kickoff.strftime('%Y-%m-%d %H:%M'),
                '全场比分':'3-0' if match%5<3 else '0-0','半场比分':'0-0','当时比分':'0-0','让球val':'','大小val':''})
            def add(market,phase,minute,water,other,line):
                rows.append({**common,'盘口类型':market,'阶段':'赛前' if phase==0 else '滚球','比赛分钟':'0' if phase==0 else str(minute),
                    '盘口数值':line,'上水/大球':water,'下水/小球':other,'变化时间':(kickoff+timedelta(minutes=minute)).strftime('%Y-%m-%d %H:%M'),'状态':'即' if phase==0 else '滚'})
            add('大小球',0,-12,'0.80','0.90','2.5');add('让球',0,-11,'0.90','0.90','0.5')
            # A second profitable direction (home -0.5 at 1.10 wins 60% of matches) gives the portfolio search
            # more than one qualified group, so ADD, REMOVE and REPLACE moves are all exercised.
            for minute in (9,12,15):add('让球',1,minute,'1.10','0.80','0.5')
            for minute,water in enumerate(('0.70','1.30','1.35','1.40','1.45','1.45','1.45'),10):add('大小球',1,minute,water,'0.85','2.5')
    return league,rows

@unittest.skipUnless(os.environ.get('FSL_RUN_NONEMPTY_PIPELINE')=='1','Explicit synthetic nonempty full-16 bounded pipeline acceptance')
class NonemptyStandardPipeline(unittest.TestCase):
    def test_full16_bounded_nonempty_chain_and_actual_fresh_zip(self):
        from lab.data import safe_extract
        from lab.packaging import content_manifest
        from lab.common import read_jsonl
        named=os.environ.get('FSL_VALIDATION_ROOT')
        with (nullcontext(named) if named else tempfile.TemporaryDirectory()) as td:
            root=Path(td);league,rows=nonempty_rows();source=root/'input/合成90场三年.csv';write_csv(source,rows);original=sha(source)
            cfg={**DEFAULT,'standard_node_budget':10,'acceptance_scope':'SYNTHETIC_FULL16_NONEMPTY_TEN_NODES_PER_DIRECTION'}
            store=Store(root/'workspace');jid=store.create(league,cfg,inspect([str(source)],store.root/'import_cache'))
            job=store.root/jid;print('NONEMPTY_FULL16_JOB='+str(job),flush=True);result=run(store.root,jid)
            self.assertIsNotNone(result);self.assertEqual(result['state'],'PARTIAL_RESULT')
            self.assertEqual(len(result['coverage']['directions']),16);self.assertGreater(result['coverage']['remaining'],0)
            self.assertFalse(result['coverage']['standard_search_complete']);self.assertFalse(result['gates']['full_standard_search'])
            self.assertGreater(result['selected'],0);self.assertGreater(result['backup_selected'],0)
            self.assertTrue(all(v for k,v in result['gates'].items() if k!='full_standard_search'))
            # The latest 12 months (the 2024 matches) never entered discovery; the frozen roster is confirmed there once.
            self.assertEqual(result['holdout']['plan']['status'],'SPLIT');self.assertEqual(result['holdout']['plan']['holdout_matches'],30)
            self.assertEqual(result['holdout']['main']['verdict'],'CONFIRMED');self.assertEqual(result['recommendation']['status'],'OBSERVATION_ONLY')
            self.assertTrue(result['standard_review']['complete']);self.assertEqual(sha(source),original)
            with closing(sqlite3.connect(job/'results/standard_review.sqlite3')) as connection:
                counts={name:connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('families','rules','neighbors','stress')}
            self.assertGreater(counts['neighbors'],0);rosters={}
            for name,folder in (('main',job/'results'),('backup',job/'results/lower_risk')):
                packet=read_json(folder/'rules.json');report=read_json(folder/'trigger_verification.json')
                self.assertEqual(report['status'],'PASS');self.assertEqual(report['execution_economics']['status'],'PASS')
                self.assertGreater(report['golden_signals'],0);self.assertEqual(packet['execution_policy']['quote_mapping'],'minute_close_latest_v3')
                self.assertTrue(all(report['streaming_paper_execution']['scenarios'][str(i)]['status']=='PASS' for i in range(5)))
                self.assertEqual(packet['execution_policy'].get('pregoal_rejection'),'explicit_live_close_and_score_change_within_one_minute_v2')
                self.assertTrue((folder/'orders_s4.jsonl.gz').is_file());self.assertTrue((folder/'minute_close_orders_s4.jsonl.gz').is_file())
                rosters[name]={'rules':len(packet['rules']),'signals':report['golden_signals'],'execution_economics':report['execution_economics']}
            actions=set()
            for path in (job/'selection_checkpoints').rglob('*.jsonl'):actions.update(r['action'] for r in read_jsonl(path))
            self.assertTrue({'ADD','REMOVE','REPLACE'}<=actions)
            packages=read_json(job/'packaging_verification.json');self.assertTrue(packages['fresh_zip_extract'])
            self.assertTrue(all(c['exit_code']==0 for c in packages['commands']))
            with tempfile.TemporaryDirectory(prefix='independent_final_A_',dir=root) as fresh:
                fresh=Path(fresh);safe_extract(job/'packages'/result['packages']['developer'],fresh)
                self.assertEqual(content_manifest(fresh),read_json(fresh/'manifest.json'))
                env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
                log=root/'independent_final_A_replay.log'
                with log.open('w',encoding='utf-8') as output:
                    process=subprocess.run([os.sys.executable,'-B','verify_handoff.py'],cwd=fresh,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=1800)
                self.assertEqual(process.returncode,0);self.assertEqual(read_json(fresh/'results/trigger_verification.json')['execution_economics']['status'],'PASS')
            atomic_json(root/'acceptance.json',{'status':'PASS','jobdir':str(job),'synthetic':True,'matches':90,'rows':len(rows),'directions':16,
                'search_node_budget_per_direction':10,'input_unchanged':sha(source)==original,'coverage':result['coverage'],'gates':result['gates'],
                'review_counts':counts,'rosters':rosters,'compared_actions':sorted(actions),'packages':result['packages'],'actual_final_A_extract_and_replay':True,
                'independent_A_log_sha256':sha(log),'scope':'Nonempty full-16 bounded production chain only; not full standard search, real league, capacity, rolling or future validation'})


@unittest.skipUnless(os.environ.get('FSL_RUN_STANDARD_PIPELINE')=='1','Explicit full standard pipeline acceptance')
class FullStandardPipeline(unittest.TestCase):
    def test_all_sixteen_branches_from_joined_csv_to_verified_packages(self):
        named=os.environ.get('FSL_VALIDATION_ROOT')
        with (nullcontext(named) if named else tempfile.TemporaryDirectory()) as td:
            root=Path(td);data=root/'input';rows=[];league='合成全标准验收（非真实比赛）'
            for year in (2024,2025,2026):
                for market in ('让球','大小球'):
                    for phase in ('赛前','滚球'):
                        for step in range(3):
                            row=quote(str(year),league=league)
                            row.update({'日期':f'{year}-03-01','开球时间':f'{year}-03-01 19:30','盘口类型':market,'阶段':phase,
                                '比赛分钟':str(10+step) if phase=='滚球' else '', '当时比分':'0-0',
                                '盘口数值':('0','0.25','-0.25')[step] if market=='让球' else ('2.5','2.75','3')[step],
                                '上水/大球':('0.90','0.95','0.90')[step],'变化时间':f'{year}-03-01 '+('20' if phase=='滚球' else '19')+f':{step:02d}',
                                '状态':'滚' if phase=='滚球' else '即'})
                            rows.append(row)
            source=data/'完整指数_合成全标准_三年.csv';write_csv(source,rows);original_hash=sha(source)
            store=Store(root/'workspace');manifest=inspect([str(data)],store.root/'import_cache')
            jid=store.create(league,DEFAULT,manifest);print('FULL_STANDARD_JOB='+str(store.root/jid),flush=True)
            result=run(store.root,jid)
            self.assertEqual(result['state'],'PARTIAL_RESULT');self.assertTrue(result['empty_roster_chain_verified']);self.assertEqual(result['coverage']['remaining'],0)
            self.assertEqual(len(result['coverage']['directions']),16);self.assertEqual(result['selected'],0)
            self.assertGreater(result['coverage']['planned_expressions'],0);self.assertEqual(read_json(store.root/jid/'search_spec.json')['grammar'],'compact_v1')
            self.assertEqual(result['holdout']['status'],'EMPTY_ROSTER');self.assertEqual(result['recommendation']['status'],'OBSERVATION_ONLY')
            self.assertEqual(result['verification']['status'],'EMPTY_ROSTER')
            self.assertTrue(all(r['status']=='PASS' for r in read_json(store.root/jid/'workflow_stages.json')['stages'].values()))
            self.assertEqual(sha(source),original_hash)
            for key in ('developer','audit'):self.assertEqual(sha(store.root/jid/'packages'/result['packages'][key]),result['packages'][key+'_sha256'])
            self.assertEqual(len(read_json(store.root/'league_registry.json')['experiments']),1)
            atomic_json(root/'acceptance.json',{'status':'PASS','job':jid,'scope':'empty-result chain only on explicitly synthetic tiny archive; not STANDARD_HANDOFF_COMPLETE and not nonempty downstream acceptance',
                'input_unchanged':True,'coverage':result['coverage'],'workflow':read_json(store.root/jid/'workflow_status.json'),'packages':result['packages']})


if __name__=='__main__':unittest.main()

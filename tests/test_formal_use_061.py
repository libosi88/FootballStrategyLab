"""Formal-use fixes: historical search limits, blank scores, roster totals, scanning, scheduling and fleet identity."""
import json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT,VALIDATION_DEFAULT,check_config,MISSING,sha,read_json
from lab.historical_pricing import pregoal_rejected_quotes
from lab.selection import Quotes
from lab.portfolio_search import PortfolioSearch
from lab.reporting import roster_lines
from lab.store import Store
from test_core import event

def ladder(n):
    # Every added rule adds real joint profit, so the best roster holds all n rules.
    rules=[{'id':f'r{i:02d}','conditions':[],'_trades':[]} for i in range(n)]
    def score(rs):
        # Synthetic scoring contract includes the evidence required by the new gate.
        m={'net_i':1000*len(rs),'drawdown_match_i':10,'matches':50,
           'segment_net_i':{'A':500*len(rs),'B':500*len(rs)},
           'segment_match_counts':{'A':25,'B':25},'history_days':800,
           'segment_availability':{'A':25,'B':25},'denominator':200}
        return {'raw':m,'stress':[m],'historical':m}
    return rules,score

class FormalUse061(unittest.TestCase):
    def test_historical_search_has_no_move_cap(self):
        self.assertEqual(DEFAULT['select_budget'],0);self.assertEqual(VALIDATION_DEFAULT['select_budget'],12)
        self.assertEqual(check_config({'select_budget':0})['select_budget'],0)
        with self.assertRaises(ValueError):check_config({'select_budget':101})
        rules,score=ladder(15)
        with tempfile.TemporaryDirectory() as td:
            capped=PortfolioSearch(rules,score,{**DEFAULT,'select_budget':12},100,Path(td)/'capped','capped','2').run()
            free=PortfolioSearch(rules,score,DEFAULT,100,Path(td)/'free','free','2').run()
        self.assertEqual(capped['status'],'PARTIAL_SELECTION_BUDGET')
        self.assertEqual(free['status'],'LOCAL_SEARCH_COMPLETE');self.assertEqual(len(free['chosen']),15)

    def test_blank_live_score_is_compared_with_the_last_known_score(self):
        labels={'a':{'eligible':True,'date':'2024-01-01','year':2024,'kickoff':0,'final':[1,0]}}
        rows=[{**event(0,line=8,ts=9,score0=(1,0)),'market':0},
              {**event(1,line=8,ts=10,score0=(MISSING,MISSING)),'market':0},
              {**event(2,line=8,ts=11,valid=False,score0=(MISSING,MISSING)),'market':0},
              {**event(3,line=8,ts=11,score0=(1,0)),'market':0}]
        # A closure followed by the same known score is not a goal, even when the fill row had a blank score.
        self.assertEqual(pregoal_rejected_quotes(rows),set())
        self.assertFalse(Quotes(rows,labels,100,5,minute_close=True).pregoal_rejected(1,10))
        goal=[*rows[:3],{**rows[3],'score':[2,0]}]
        self.assertEqual(pregoal_rejected_quotes(goal),{1})
        self.assertTrue(Quotes(goal,labels,100,5,minute_close=True).pregoal_rejected(1,10))
        never=[{**event(0,line=8,ts=10,score0=(MISSING,MISSING)),'market':0},
               {**event(1,line=8,ts=11,valid=False,score0=(MISSING,MISSING)),'market':0},
               {**event(2,line=8,ts=11,score0=(1,0)),'market':0}]
        # Without any known score there is no evidence of a goal.
        self.assertEqual(pregoal_rejected_quotes(never),set())
        self.assertFalse(Quotes(never,labels,100,5,minute_close=True).pregoal_rejected(0,10))

    def test_final_decision_reports_joint_roster_totals(self):
        comparisons=[{'名单':'默认','整场上限':4,'情景':4,'政策':'整场最多4单位','n':120,'net':38.4,'roi':0.32,'drawdown_match':4.0},
                     {'名单':'默认','整场上限':4,'情景':0,'政策':'整场最多4单位','n':130,'net':50.0,'roi':0.38,'drawdown_match':3.0},
                     {'名单':'较低风险','整场上限':4,'情景':4,'政策':'整场最多4单位','n':60,'net':19.2,'roi':0.32,'drawdown_match':2.0}]
        lines=roster_lines({'comparisons':comparisons},{'match_cap':4})
        self.assertEqual(len(lines),2)
        self.assertIn('120 笔',lines[0]);self.assertIn('38.400',lines[0]);self.assertIn('较稳定备选',lines[1])

    def test_stale_engine_jobs_leave_the_queue_instead_of_failing(self):
        from lab.server import launch_next
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('test',DEFAULT,{'files':[]})
            with store.conn() as c:
                config=json.loads(c.execute('SELECT config FROM jobs WHERE id=?',(jid,)).fetchone()['config']);config['engine_hash']='old'
                c.execute('UPDATE jobs SET config=? WHERE id=?',(json.dumps(config),jid))
            with patch('lab.server.subprocess.Popen') as popen:self.assertIsNone(launch_next(store,threading.Event()))
            popen.assert_not_called()
            job=store.get(jid);self.assertEqual(job['status'],'PAUSED');self.assertIn('SOURCE_REVISION_MISMATCH',job['error'])
            self.assertEqual(store.resume_all(),[])

    def test_resume_all_queues_current_paused_and_interrupted_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);paused=store.create('a',DEFAULT,{'files':[]},start_paused=True);interrupted=store.create('b',DEFAULT,{'files':[]})
            store.update(interrupted,status='INTERRUPTED')
            self.assertEqual(sorted(store.resume_all()),sorted([paused,interrupted]))
            self.assertTrue(all(store.get(j)['status']=='QUEUED' for j in (paused,interrupted)))

    def test_scan_skips_underscore_folders_and_refuses_duplicate_league_copies(self):
        from lab.data import inspect
        from test_pipeline_standard import nonempty_rows
        from test_v031 import write_csv
        league,rows=nonempty_rows()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'数据'
            write_csv(root/'_build'/'backup'/'完整指数_备份.csv',rows)
            write_csv(root/'甲'/'完整指数_合成_2024.csv',rows)
            first=sorted({str(r['sId']) for r in rows})[:20]
            write_csv(root/'乙'/'完整指数_合成_2024.csv',[r for r in rows if str(r['sId']) in first])
            manifest=inspect([str(root)],Path(td)/'cache')
            self.assertFalse(any('_build' in r['path'] for r in manifest['files']))
            row=next(r for r in manifest['leagues'] if r['league']==league)
            self.assertEqual(row['source_conflict']['duplicate_matches'],20)
            with self.assertRaisesRegex(ValueError,'重复比赛'):Store(Path(td)/'work').create(league,DEFAULT,manifest)
            only=inspect([str(root/'甲')],Path(td)/'cache')
            self.assertNotIn('source_conflict',next(r for r in only['leagues'] if r['league']==league))

    def test_rebuild_stale_keeps_the_old_validation_policy_and_skips_real_failures(self):
        from lab.migration import rebuild_stale_jobs
        from lab.data import inspect
        from test_pipeline_standard import nonempty_rows
        from test_v031 import write_csv
        league,rows=nonempty_rows()
        with tempfile.TemporaryDirectory() as td:
            write_csv(Path(td)/'data'/'完整指数_合成_2024.csv',rows)
            store=Store(Path(td)/'work');manifest=inspect([str(Path(td)/'data')],Path(td)/'cache')
            old=store.create(league,VALIDATION_DEFAULT,manifest,start_paused=True)
            failed=store.create(league,{**VALIDATION_DEFAULT,'match_cap':3},manifest,start_paused=True)
            with store.conn() as c:
                for jid in (old,failed):
                    config=json.loads(c.execute('SELECT config FROM jobs WHERE id=?',(jid,)).fetchone()['config'])
                    # As created by 0.5: an older engine and no research objective.
                    config['engine_hash']='old';config.pop('research_objective')
                    c.execute('UPDATE jobs SET config=? WHERE id=?',(json.dumps(config),jid))
            store.update(failed,status='ERROR',error='输入文件损坏')
            result=rebuild_stale_jobs(store.root,queue=True)
            self.assertEqual([r['source_job'] for r in result['rebuilt']],[old]);self.assertEqual(result['failed'],[])
            new=store.get(result['rebuilt'][0]['job'])
            self.assertEqual((new['config']['research_objective'],new['status']),('validation','QUEUED'))
            self.assertEqual(rebuild_stale_jobs(store.root)['rebuilt'][0]['job'],new['id'])

    def test_fleet_identity_survives_regenerated_plans_and_later_queueing(self):
        from lab.fleet import make_plans,import_plan
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'data';data.mkdir();records=[];leagues=[]
            for i,count in enumerate((100,80)):
                league=f'L{i}';p=data/f'{league}.csv';p.write_text('fixture'+str(i),encoding='utf-8')
                records.append({'kind':'quotes','path':str(p),'sha256':sha(p),'targets':[[league,'皇冠']]})
                leagues.append({'league':league,'company':'皇冠','rows':count})
            index=data/'L1_index.csv';index.write_text('index',encoding='utf-8')
            records.append({'kind':'index','path':str(index),'sha256':sha(index),'leagues':['L1']})
            with patch('lab.fleet.inspect',return_value={'files':records,'leagues':leagues}):
                two=make_plans([str(data)],root/'plan2',2,None)
                make_plans([str(data)],root/'plan1',1,None)
                with self.assertRaisesRegex(ValueError,'公司'):make_plans([str(data)],root/'plan3',1,'平博',{'company':'皇冠'})
            node_l0=next(p['file'] for p in two['plans'] if p['leagues']==['L0'])
            # Each node plan lists exactly the files that machine must copy.
            self.assertEqual(read_json(node_l0)['required_files'],['L0.csv'])
            self.assertEqual(sorted(read_json(root/'plan1'/'node_001.json')['required_files']),['L0.csv','L1.csv','L1_index.csv'])
            paused=import_plan(node_l0,data,root/'work')
            everything=import_plan(root/'plan1'/'node_001.json',data,root/'work',queue=True)
            store=Store(root/'work')
            # A regenerated plan reuses the paused job and queues it instead of creating a duplicate.
            self.assertIn(paused['jobs'][0],everything['jobs']);self.assertEqual(len(store.jobs()),2)
            self.assertTrue(all(j['status']=='QUEUED' for j in store.jobs()))
            # Index files of leagues assigned elsewhere need not exist on this machine.
            index.unlink()
            self.assertEqual(len(import_plan(node_l0,data,root/'other')['jobs']),1)

if __name__=='__main__':unittest.main()

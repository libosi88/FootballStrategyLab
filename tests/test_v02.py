import unittest, tempfile, json, copy, itertools, os, subprocess, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from lab.common import *
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.contracts import *
from lab.features import SignalEngine, FeatureStream
from lab.rules import dictionary, matches, mask
from lab.search_plan import *
from lab.mining import build_direction,pack_masks,evaluate,first_indices,check_resources,ResourcePaused,Paused
from lab.portfolio_search import PortfolioSearch,move_count,move_at,best_in_band
from lab.selection import dispatch,portfolio_metrics
from lab.store import Store
from lab.locking import WorkspaceLock,WorkspaceBusy
from test_core import event, TEST_CONTRACT


def rule(value=90):
    return bind_rule({'id':'r','direction':'LIVE_GIVE','conditions':[{'feature':'water','op':'ge','value':value}],
                      'priority':0,'version':VERSION},TEST_CONTRACT)

class Contracts(unittest.TestCase):
    def test_requires_explicit_contract(self):
        with self.assertRaises(ValueError):SignalEngine([{'id':'r','direction':'LIVE_GIVE','conditions':[]}])
    def test_rejects_changed_rule_content(self):
        r=rule();r['conditions'][0]['value']=80
        with self.assertRaises(ValueError):SignalEngine([r])
    def test_rejects_different_rules_on_restore(self):
        eng=SignalEngine([rule()]);eng.feed(event(0))
        new=rule(100)
        with self.assertRaises(ValueError):SignalEngine([new],eng.snapshot())
    def test_rejects_different_scope(self):
        eng=SignalEngine([rule()])
        for k,val in [('league','其他联赛'),('company','平博'),('provider','other'),('market_period','HALF_TIME'),('semantics','unknown')]:
            with self.subTest(k=k):
                e=event(0);e[k]=val
                with self.assertRaises(ValueError):eng.feed(e)
                self.assertEqual(eng.snapshot()['features'],{})
    def test_rejects_wrong_units(self):
        e=event(0);e['water_scale']=1000;e['water']=[500,950]
        with self.assertRaises(ValueError):SignalEngine([rule(95)]).feed(e)
        e=event(0);e['line_scale']=100
        with self.assertRaises(ValueError):SignalEngine([rule()]).feed(e)
    def test_caller_mutation_does_not_change_rule(self):
        r=rule(100);eng=SignalEngine([r]);r['conditions'][0]['value']=10
        self.assertEqual(eng.feed(event(0,w0=95)),[])
    def test_snapshot_not_alias_and_tamper_rejected(self):
        eng=SignalEngine([rule()]);eng.feed(event(0));s=eng.snapshot();s['fired']=[]
        self.assertTrue(eng.snapshot()['fired'])
        with self.assertRaises(ValueError):SignalEngine([rule()],s)
    def test_execution_policy_bound(self):
        p={'match_cap':4};eng=SignalEngine([rule()],execution_policy=p)
        with self.assertRaises(ValueError):SignalEngine([rule()],eng.snapshot(),execution_policy={'match_cap':2})
    def test_mixed_rule_scopes_rejected(self):
        r=rule();r2=bind_rule({'id':'other','direction':'LIVE_GIVE','conditions':[]},make_contract('OTHER'))
        with self.assertRaises(ValueError):SignalEngine([r,r2])
    def test_unknown_event_fields_rejected(self):
        e=event(0);e['mystery']=0
        with self.assertRaises(ValueError):SignalEngine([rule()]).feed(e)
    def test_status_score_consistency(self):
        for changes in ({'valid':False},{'status':'早'},{'score':[MISSING,1]},{'line':0.25}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):FeatureStream().feed({**event(0),**changes})
    def test_empty_roster_still_checks_scope(self):
        e=event(0);eng=SignalEngine([],contract=TEST_CONTRACT);self.assertEqual(eng.feed(e),[])
        e=event(1);e['company']='other'
        with self.assertRaises(ValueError):eng.feed(e)
    def test_integer_threshold_bool_rejected(self):
        r=rule();r['conditions'][0]['value']=True;r.pop('content_hash')
        with self.assertRaises(ValueError):SignalEngine([r])
    def test_state_legacy_not_silently_accepted(self):
        with self.assertRaises(ValueError):SignalEngine([rule()],{'fired':['a|r'],'features':{}})

class SearchPlans(unittest.TestCase):
    def test_unrank_all_small(self):
        for n in range(1,12):
            for k in range(1,min(4,n)+1):
                for i,c in enumerate(itertools.combinations(range(n),k)):
                    self.assertEqual(unrank_combination(n,k,i),c)
    def test_seek_very_far_without_prefix(self):
        n=100000;k=3;last=choose(n,k)-1
        self.assertEqual(next(combinations_from(range(n),k,last)),(n-3,n-2,n-1))
        self.assertEqual(list(combinations_from(range(n),k,last+1)),[])
    def test_plan_full_reference_and_every_resume_point(self):
        atoms=[{'feature':'a','op':'ge','value':0},{'feature':'b','op':'ge','value':0},
               {'feature':'path3_keep','op':'eq','value':123},
               {'feature':'path3_keep','op':'eq','value':123,'span':30,'pulse':True},
               {'feature':'water','op':'ge','value':96}]
        spec=make_plan_spec(atoms,[0,1,2],[0,1,2],'expanded');plan=TaskPlan(spec)
        ref=[(),*((i,) for i in range(5)),*itertools.combinations([0,1,2],2),*itertools.combinations([0,1,2],3)]
        for anchor in (3,4):
            for k in (1,2):ref += [tuple(sorted((anchor,*c))) for c in itertools.combinations([0,1],k)]
        self.assertEqual(list(plan.iter_from()),ref);self.assertEqual(len(set(ref)),len(ref));self.assertEqual(plan.total,len(ref))
        for pos in range(len(ref)+1):self.assertEqual(list(plan.iter_from(pos)),ref[pos:])
    def test_fixed_dictionary_paths_and_fine_have_contexts(self):
        from lab.features import FEATURE_NAMES
        arr={f:np.array([0,1,2],dtype=np.int64) for f in FEATURE_NAMES}
        arr['water']=np.array([95,96,105]);arr['otherwater']=np.array([80,85,95])
        for profile in ('routine','expanded'):
            aa,pp,tt=dictionary(arr,'LIVE_OVER',100,profile);sp=make_plan_spec(aa,pp,tt,profile)
            c3=[i for i,a in enumerate(aa) if a['feature']=='path3_keep' and 'span' not in a]
            self.assertTrue(c3);self.assertTrue(all(i in tt for i in c3))
            self.assertGreater(sp['timed_atoms'],0)
            self.assertTrue(any(b['id']=='L04_TIMED_CONTEXT2' and b['size'] for b in sp['blocks']))
            if profile=='expanded':
                ix=next(i for i,a in enumerate(aa) if a['feature']=='water' and a['op']=='ge' and a['value']==96)
                block=next(b for b in sp['blocks'] if b['id']=='L05_FINE_CONTEXT2');self.assertIn(ix,block['anchors'])
    def test_plan_economics_matches_naive_all_events(self):
        arr={'eid':np.arange(168),'mid':np.repeat(np.arange(12),14),
             'pnl':np.tile(np.array([-200]*10+[190]*4,dtype=np.int64),12),
             'x':np.tile(np.arange(14),12),'water':np.tile(np.arange(90,104),12)}
        atoms=[{'feature':'x','op':'ge','value':v} for v in (0,5,10)]+[{'feature':'water','op':'ge','value':97}]
        plan=TaskPlan(make_plan_spec(atoms,[0,1,2],[0,1,2],'expanded'));cs=list(plan.iter_from())
        cc=np.full((len(cs),3),-1,dtype=np.int64)
        for j,c in enumerate(cs):cc[j,:len(c)]=c
        m,nz,o,end,p=pack_masks(arr,atoms);got=evaluate(cc,m,nz,o,end,p)
        reference=[]
        # Deliberately do not call first_indices/mask/matches here.
        for c in cs:
            n=net=0
            for mid in range(12):
                for i in range(mid*14,(mid+1)*14):
                    if all(int(arr[atoms[a]['feature']][i])>=atoms[a]['value'] for a in c):
                        n+=1;net+=int(arr['pnl'][i]);break
            reference.append([n,net])
        self.assertEqual(got.tolist(),reference)
        self.assertTrue(any(net>2000 for n,net in reference)) # profitable descendants are not pruned
        self.assertTrue(any(net>0 for n,net in reference))
    def test_coverage_counts_reconcile(self):
        a=[{'feature':'x','op':'ge','value':i} for i in range(5)]
        p=TaskPlan(make_plan_spec(a,list(range(5)),list(range(5)),'routine'))
        for pos in (0,1,4,p.total):self.assertEqual(sum(b['evaluated'] for b in p.coverage(pos)),pos)
    def test_resource_budget_rejects_instead_of_cutting(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ResourcePaused):check_resources(td,2*1024*1024,{**DEFAULT,'max_mask_mb':1})


def synthetic_rule(rid,values,offset=0,same_sids=False):
    tt=[]
    for i,pnl in enumerate(values):
        sid=str(i) if same_sids else rid+'-'+str(i)
        tt.append({'sid':sid,'mid':i,'eid':i+offset,'signal_eid':i+offset,'ts':i+offset,
                   'quote_ts':i+offset,'sort_time':i+offset,'line':10,'water':95,'score':[0,0],
                   'market':0,'phase':1,'side':0,'year':2024,'pnl':pnl,'stake':1,'quality':0})
    return {'id':rid,'direction':'LIVE_OVER','conditions':[{'feature':'water','op':'ge','value':90}],
            '_trades':[copy.deepcopy(tt) for _ in range(4)]}


def scorer(rs):
    mm=[portfolio_metrics(dispatch(rs,s,4)[0],100,[2024]) for s in range(4)]
    return {'raw':mm[0],'stress':mm[1:]}

class Portfolios(unittest.TestCase):
    def test_move_neighborhood_complete(self):
        pool=['a','b','c','d'];chosen=['a','b']
        moves=[move_at(pool,chosen,i) for i in range(move_count(pool,chosen))]
        self.assertEqual(len(moves),8)
        self.assertEqual([x[0] for x in moves].count('REPLACE'),4)
    def test_more_profit_but_risk_violation_not_selected(self):
        a=synthetic_rule('a',[190,-100,190]);b=synthetic_rule('b',[190,190,-200,-200,190,190,190],offset=10)
        with tempfile.TemporaryDirectory() as td:
            # a: net 280 against drawdown 100; b alone and a+b stay below a 2.5 return/drawdown ratio.
            r=PortfolioSearch([a,b],scorer,{**DEFAULT,'select_budget':8},100,td,'risk','2.5').run()
            self.assertEqual(r['chosen'],['a']);self.assertGreaterEqual(r['score']['min_profit_i'],2.5*r['score']['max_drawdown_i'])
    def test_lower_risk_near_profit_replacement(self):
        a=synthetic_rule('a',[190,-200,190,190,190],same_sids=True)
        b=synthetic_rule('b',[190,-100,190,90,180],offset=1,same_sids=True)
        with tempfile.TemporaryDirectory() as td:
            rr=PortfolioSearch([a,b],scorer,{**DEFAULT,'select_budget':8},100,td,'replace','2').run()
            self.assertEqual(rr['chosen'],['b'])
            actions=[x for p in rr['paths'] for x in p['actions']]
            self.assertTrue(any(x['from']==['a'] and x['to']==['b'] for x in actions))
    def test_pause_resume_same_result_and_committed_journal(self):
        pool=[synthetic_rule('a',[190,-100,190]),synthetic_rule('b',[190,-200,190],offset=10),synthetic_rule('c',[190,190,-100],offset=20)]
        cfg={**DEFAULT,'select_budget':5,'selection_checkpoint_every':1};counter=[0];progress={}
        def update(**row):progress.update(row)
        def pause():
            # This case exercises committed neighborhood journals; singleton
            # precheck pauses are covered separately and create no journal.
            if progress.get('selection_phase')!='NEIGHBORHOOD_SEARCH':return False
            counter[0]+=1;return counter[0]==4
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            with self.assertRaises(Paused):PortfolioSearch(pool,scorer,cfg,100,p/'paused','same','1',update=update,should_pause=pause).run()
            # Crash-like uncommitted suffix must not become committed history.
            journal=next((p/'paused').glob('*.jsonl'))
            with journal.open('ab') as f:f.write(b'{invalid uncommitted suffix}\n')
            one=PortfolioSearch(pool,scorer,cfg,100,p/'paused','same','1').run()
            two=PortfolioSearch(pool,scorer,cfg,100,p/'whole','same','1').run()
            self.assertEqual(one,two)
            for f in (p/'whole').glob('*.jsonl'):self.assertEqual(f.read_bytes(),(p/'paused'/f.name).read_bytes())
    def test_budget_stop_is_not_completion(self):
        pool=[synthetic_rule(str(i),[190,-100,190],offset=i*10) for i in range(5)]
        with tempfile.TemporaryDirectory() as td:
            r=PortfolioSearch(pool,scorer,{**DEFAULT,'selection_eval_budget':1},100,td,'budget','10').run()
            self.assertEqual(r['status'],'PARTIAL_SELECTION_BUDGET')
    def test_changed_policy_rejects_checkpoint(self):
        pool=[synthetic_rule('a',[190,-100,190])]
        with tempfile.TemporaryDirectory() as td:
            PortfolioSearch(pool,scorer,DEFAULT,100,td,'bound','10').run()
            with self.assertRaises(ValueError):PortfolioSearch(pool,scorer,DEFAULT,100,td,'bound','9').run()
    def test_tolerance_is_anchored_not_unlimited_drift(self):
        opts=[{'ids':['a'],'feasible':True,'min_profit_i':1800,'max_drawdown_i':0,'matches_floor':100,'condition_count':1},
              {'ids':['b'],'feasible':True,'min_profit_i':1920,'max_drawdown_i':200,'matches_floor':100,'condition_count':1}]
        self.assertEqual(best_in_band(opts,2000,100)['ids'],['b'])
    def test_empty_qualified_pool_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            r=PortfolioSearch([],scorer,DEFAULT,100,td,'empty','10').run();self.assertEqual(r['chosen'],[])

class WorkerProtection(unittest.TestCase):
    def test_claim_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);j=s.create('T',DEFAULT,{'files':[]})
            with ThreadPoolExecutor(2) as ex:
                got=list(ex.map(lambda _:Store(td).claim(j,os.getpid()),[0,1]))
            self.assertEqual(sum(got),1)
    def test_workspace_mutex(self):
        with tempfile.TemporaryDirectory() as td:
            with WorkspaceLock(td):
                with self.assertRaises(WorkspaceBusy):
                    with WorkspaceLock(td):pass
            with WorkspaceLock(td):pass
    def test_killed_worker_releases_mutex(self):
        with tempfile.TemporaryDirectory() as td:
            script="import time,sys,os;from lab.locking import WorkspaceLock\nwith WorkspaceLock(sys.argv[1]):\n print('LOCKED '+str(os.getpid()),flush=True)\n time.sleep(60)"
            proc=subprocess.Popen([sys.executable,'-c',script,td],cwd=ROOT,stdout=subprocess.PIPE,text=True)
            try:
                ready=proc.stdout.readline().strip().split()
                self.assertEqual(ready[0],'LOCKED');worker_pid=int(ready[1])
                with self.assertRaises(WorkspaceBusy):
                    with WorkspaceLock(td):pass
                # Windows venv python.exe can be a redirector. Kill the lock owner,
                # not just the launcher whose PID Popen returned.
                import signal
                os.kill(worker_pid,signal.SIGTERM);proc.wait(timeout=10)
                with WorkspaceLock(td):pass
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
                proc.stdout.close()

if __name__=='__main__':unittest.main()

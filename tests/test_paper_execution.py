import tempfile,unittest,copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from lab.common import digest,side_for
from lab.contracts import bind_rule
from lab.paper_execution import PaperDispatcher
from test_core import event,TEST_CONTRACT

def packet(directions,cap=4):
    rules=[bind_rule({'id':f'r{i}','direction':direction,'conditions':[],'priority':i},TEST_CONTRACT) for i,direction in enumerate(directions)]
    policy={'match_cap':cap,'stale_minutes':5,'priority':'frozen_rule_priority','live_enabled':False,'second_slot_enabled':False}
    p={'rules':rules,'contract':TEST_CONTRACT,'execution_policy':policy};p['roster_hash']=digest(p)
    return p

def offer(p,index,e):
    r=p['rules'][index];side=side_for(r['direction'],e['line'])
    s={'strategy_id':r['id'],'direction':r['direction'],'sid':e['sid'],'eid':e['eid'],'event_key':e['event_key'],'ts':e['ts'],'side':side,'line':e['line'],'water':e['water'][side],'score':e['score'][:]}
    return {'strategy_id':r['id'],'signal':s,'quote':e}

class PaperExecution(unittest.TestCase):
    def test_one_hundred_same_direction_signals_reserve_one(self):
        p=packet(['LIVE_OVER']*100);e={**event(0,line=8,ts=1),'market':0}
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            offers=[offer(p,i,e) for i in range(100)];r=d.submit_batch(offers)
            self.assertEqual(sum(x['status']=='RESERVED' for x in r),1);self.assertEqual(d.usage('a'),1)
            self.assertEqual(len(d.submit_batch(offers[::-1])),100);self.assertEqual(d.usage('a'),1)
    def test_unknown_partial_cancel_and_shadow_persist(self):
        p=packet(['LIVE_GIVE','LIVE_HOME']);e=event(0,line=2,ts=1)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with PaperDispatcher(path,p) as d:
                iid=d.submit_batch([offer(p,0,e)])[0]['intent_id'];d.receipt(iid,'u','SUBMITTED_UNKNOWN')
                self.assertEqual(d.usage('a'),1)
                d.receipt(iid,'p','PARTIAL','0.4');self.assertEqual(d.usage('a'),1)
                with self.assertRaises(ValueError):d.receipt(iid,'c','CANCELLED','0.4')
                self.assertEqual(d.usage('a'),1)
                d.receipt(iid,'c','CANCELLED','0.4',True);self.assertEqual(d.usage('a'),.4)
            with PaperDispatcher(path,p) as d:
                r=d.submit_batch([offer(p,1,event(1,line=2,ts=2))]);self.assertEqual(r[0]['status'],'REJECTED')
                self.assertEqual(len(d.shadows()),1);self.assertEqual(d.usage('a'),.4)
                with self.assertRaises(ValueError):d.receipt(iid,'new','PARTIAL','0.5')
    def test_receipt_dedup_and_binding(self):
        p=packet(['LIVE_GIVE']);e=event(0,line=2,ts=1)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with PaperDispatcher(path,p) as d:
                iid=d.submit_batch([offer(p,0,e)])[0]['intent_id'];d.receipt(iid,'fill','FILLED','1')
                d.receipt(iid,'fill','FILLED','1');self.assertEqual(d.usage('a'),1)
                with self.assertRaises(ValueError):d.receipt(iid,'fill','SUBMITTED_UNKNOWN','1')
                self.assertEqual(d.intent(iid)['status'],'FILLED')
                d.save_runner_state('engine',{'fired':['a|r0']});self.assertEqual(d.load_runner_state('engine'),{'fired':['a|r0']})
            changed=packet(['LIVE_RECEIVE'])
            with self.assertRaises(ValueError):PaperDispatcher(path,changed)
    def test_opposite_batch_and_future_label_rejection(self):
        p=packet(['LIVE_OVER','LIVE_UNDER']);e={**event(0,line=8,ts=1),'market':0}
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            r=d.submit_batch([offer(p,0,e),offer(p,1,e)]);self.assertTrue(all(x['status']=='REJECTED' for x in r));self.assertEqual(d.usage('a'),0)
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            bad=offer(p,0,e);bad['quote']={**e,'final':[3,2]}
            with self.assertRaises(ValueError):d.submit_batch([bad])
            self.assertEqual(d.usage('a'),0)
    def test_cancelled_unfilled_slot_can_be_used_by_another_rule(self):
        p=packet(['LIVE_GIVE','LIVE_HOME']);e=event(0,line=2,ts=1)
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            iid=d.submit_batch([offer(p,0,e)])[0]['intent_id'];d.receipt(iid,'cancel','CANCELLED','0',True)
            self.assertEqual(d.usage('a'),0)
            self.assertEqual(d.submit_batch([offer(p,1,event(1,line=2,ts=2))])[0]['status'],'RESERVED')
    def test_two_workers_retry_same_batch_without_double_reservation(self):
        p=packet(['LIVE_GIVE']);e=event(0,line=2,ts=1)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with PaperDispatcher(path,p):pass
            def work(_):
                with PaperDispatcher(path,p) as d:return d.submit_batch([offer(p,0,e)])[0]['intent_id']
            with ThreadPoolExecutor(2) as ex:ids=list(ex.map(work,range(2)))
            self.assertEqual(ids[0],ids[1])
            with PaperDispatcher(path,p) as d:self.assertEqual(d.usage('a'),1)

if __name__=='__main__':unittest.main()

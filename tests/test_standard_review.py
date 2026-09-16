import tempfile,unittest
from pathlib import Path
from lab.common import DEFAULT,atomic_json,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_mining import ordered_standard_events
from lab.mining import build_direction
from lab.standard_columns import StandardColumns
from lab.standard_atoms import AtomCatalog,W,C
from lab.standard_masks import MaskStore
from lab.standard_search import run_class_search,family_conditions,family_conditions_from
from lab.normalization import normalize_conditions
from lab.standard_review import select_standard
from lab.selection import dispatch,metrics
from lab.verify import verify
from test_core import event

class StandardReview(unittest.TestCase):
    def test_numeric_logic_normalization(self):
        conditions=[{'feature':'water','op':'ge','value':85},{'feature':'water','op':'ge','value':90},{'feature':'water','op':'le','value':99}]
        result=normalize_conditions(conditions,100)
        self.assertEqual([{k:v for k,v in a.items() if k!='label'} for a in result],[{'feature':'water','op':'range','value':90,'upper':100}])
        self.assertIsNone(normalize_conditions([{'feature':'line','op':'ge','value':3},{'feature':'line','op':'le','value':2}],100))
    def test_frozen_priority_is_independent_of_rule_names(self):
        t={'sid':'a','market':0,'phase':1,'eid':0,'side':0,'line':10,'water':95,'score':[0,0],'ts':1}
        r=[{'id':'a','priority':20,'direction':'LIVE_OVER','_trades':[[t]]},{'id':'z','priority':2,'direction':'LIVE_OVER','_trades':[[{**t,'eid':1,'water':90}]]}]
        self.assertEqual(dispatch(r,0,4,priority_mode='frozen_rule_priority')[0][0]['strategy_id'],'z')
        self.assertEqual(dispatch(r,0,4)[0][0]['strategy_id'],'a')
    def test_five_outcomes_and_streak_losses(self):
        trades=[{'pnl':v,'water':95,'stake':1,'sort_time':i,'sid':str(i),'ts':i,'eid':i,'year':2024} for i,v in enumerate((190,95,0,-100,-200))]
        m=metrics(trades,100,[2024]);self.assertEqual(m['outcome_counts'],{'win':1,'half_win':1,'push':1,'half_loss':1,'loss':1,'unclassified':0});self.assertEqual(m['max_streak_loss'],1.5)
    def test_fixed_team_alias_cannot_bypass_core_slot(self):
        t={'sid':'a','market':1,'phase':1,'eid':0,'side':0,'line':2,'water':95,'score':[0,0],'ts':1}
        r=[{'id':'give','priority':0,'direction':'LIVE_GIVE','_trades':[[t]]},{'id':'home','priority':1,'direction':'LIVE_HOME','_trades':[[{**t,'eid':1,'ts':2}]]}]
        orders,rejected=dispatch(r,0,4,True,'frozen_rule_priority')
        self.assertEqual(len(orders),1);self.assertEqual(rejected[0]['reason'],'同方向首单已占用')
    def test_review_and_streaming_trigger_use_same_normalized_rules(self):
        rows=[];labels={}
        for mid in range(90):
            sid=f'r{mid:03d}';year=2024+mid//30
            labels[sid]={'eligible':True,'final':[0,0] if mid%5==0 else [2,1],'year':year,'date':f'{year}-01-01','kickoff':mid*10}
            for step in range(3):
                e=event(len(rows),line=4,w0=90+step*5,ts=mid*10+step);e.update(sid=sid,mid=mid,market=0,minute=step)
                rows.append(e)
        rows=ordered_standard_events(rows);config={**DEFAULT,'profile':'standard','directions':['LIVE_OVER'],'select_budget':5}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);mine=root/'mining/LIVE_OVER';mine.mkdir(parents=True)
            atomic_json(root/'config.json',config);atomic_json(root/'labels.json',labels)
            from lab.common import write_jsonl
            write_jsonl(root/'events.jsonl.gz',rows)
            base=build_direction(rows,labels,100,'LIVE_OVER',config)
            import numpy as np
            np.savez_compressed(mine/'arrays.npz',**base)
            with AtomCatalog(mine/'atoms.sqlite3',True) as atoms:
                for v in (85,90,96):atoms.add({'feature':'water','op':'ge','value':v},W|C,100)
                atoms.commit()
                with StandardColumns(base,rows,'LIVE_OVER') as columns,MaskStore(mine,columns,atoms,config,'review_fixture') as masks:
                    masks.prepare(lambda **kw:None,lambda:False)
                    run_class_search(mine,atoms,masks,{**config,'_water_scale':100},lambda **kw:None,lambda:False,'review_fixture')
            result=select_standard(root,rows,labels,100,config,lambda **kw:None,lambda:False)
            self.assertTrue(result['standard_review']['complete']);self.assertGreater(result['selected'],0)
            # Mirror the production S6 order instead of relying on the verifier to
            # manufacture missing evidence and then verify its own replacement.
            from lab.handoff_assets import write_assets
            write_assets(root)
            report=verify(root);self.assertEqual(report['status'],'PASS');self.assertFalse(any(report['order_differences'].values()))
            packet=read_json(root/'results/rules.json');self.assertEqual(packet['execution_policy']['priority'],'frozen_rule_priority')
            self.assertEqual(packet['execution_policy']['pregoal_rejection'],'explicit_live_close_and_score_change_within_one_minute_v2')
            self.assertTrue((root/'results/orders_s4.jsonl.gz').is_file());self.assertTrue((root/'results/minute_close_orders_s4.jsonl.gz').is_file())
            self.assertEqual(report['streaming_paper_execution']['priced_scenario_count'],5)
            self.assertEqual(report['streaming_paper_execution']['scenarios']['4']['status'],'PASS')
            self.assertEqual(report['streaming_paper_execution']['scenarios']['4']['coordinator'],'not_replayed')
            again=select_standard(root,rows,labels,100,config,lambda **kw:None,lambda:False)
            self.assertEqual(again['selected'],result['selected'])

if __name__=='__main__':unittest.main()

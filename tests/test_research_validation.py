import tempfile,unittest
from pathlib import Path
from lab.common import DEFAULT,digest,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.research_validation import block_bootstrap,adapt_packet_scale
from lab.walk_forward import split_matches,prepare_training_job
from lab.data import inspect,load_inputs
from lab.store import Store
from lab.contracts import bind_rule
from test_core import TEST_CONTRACT
from test_v031 import quote,write_csv

class ResearchValidation(unittest.TestCase):
    def test_common_blocks_preserve_opposite_scenario_payoffs(self):
        labels={str(i):{'eligible':True,'date':f'2024-01-{i+1:02d}','kickoff':i*1440} for i in range(10)}
        first=[{'sid':str(i),'pnl':(i-4)*100,'stake':1} for i in range(10)];second=[{**t,'pnl':-t['pnl']} for t in first]
        a=block_bootstrap([first,second],labels,100,50,3,12);b=block_bootstrap([first,second],labels,100,50,3,12)
        self.assertEqual(a,b);self.assertFalse(a['selection_bias_adjusted'])
        self.assertAlmostEqual(a['scenarios'][0]['net_interval_95'][0],-a['scenarios'][1]['net_interval_95'][1])
    def test_scale_adaptation_is_exact_and_does_not_change_line_or_span(self):
        conditions=[{'feature':'water','op':'ge','value':95},{'feature':'line','op':'eq','value':1},{'feature':'path2_keep','op':'eq','value':13,'sequence':'subsequence','span':5,'min_water_step':5}]
        rule=bind_rule({'id':'r','direction':'LIVE_GIVE','conditions':conditions,'priority':0},TEST_CONTRACT)
        packet={'rules':[rule],'water_scale':100,'contract':TEST_CONTRACT,'execution_policy':{'priority':'frozen_rule_priority','live_enabled':False,'second_slot_enabled':False}}
        packet['roster_hash']=digest({k:packet[k] for k in ('rules','contract','execution_policy')})
        adapted=adapt_packet_scale(packet,1000);cs=adapted['rules'][0]['conditions']
        self.assertEqual(cs[0]['value'],950);self.assertEqual(cs[1]['value'],1);self.assertEqual(cs[2]['min_water_step'],50);self.assertEqual(cs[2]['span'],5)
        self.assertEqual(packet['rules'][0]['conditions'][0]['value'],95)
    def test_training_partition_does_not_use_future_numeric_precision(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'quotes.csv';train=quote('past');test={**quote('future'),'日期':'2025-01-01','开球时间':'2025-01-01 19:30','变化时间':'2025-01-01 20:20','上水/大球':'0.955'}
            write_csv(p,[train,test]);man=inspect([str(p)],root/'inspect');_,labels,scale,_=load_inputs(man,'真实甲组','皇冠')
            self.assertEqual(scale,1000)
            store=Store(root/'work');parent_id=store.create('真实甲组',DEFAULT,man,start_paused=True);parent=store.get(parent_id)
            jid,train_ids,test_ids=prepare_training_job(store,parent,labels,'2024-12-31','2025-12-31')
            self.assertEqual(train_ids,{'past'});self.assertEqual(test_ids,{'future'});self.assertEqual(store.get(jid)['status'],'PAUSED')
            audit=read_json(store.root/jid/'data_audit.json');self.assertEqual(audit['water_scale'],100)
            self.assertEqual(set(read_json(store.root/jid/'labels.json')),{'past'})
    def test_whole_match_split_is_disjoint(self):
        labels={'a':{'eligible':True,'date':'2024-12-31'},'b':{'eligible':True,'date':'2025-01-01'},'c':{'eligible':False,'date':'2025-01-02'}}
        self.assertEqual(split_matches(labels,'2024-12-31','2025-12-31'),({'a'},{'b'}))

if __name__=='__main__':unittest.main()

import unittest, tempfile
from pathlib import Path
from lab.common import DEFAULT, check_config
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.features import SignalEngine, FeatureStream
from lab.selection import dispatch
from lab.store import Store
from test_core import event

class Release(unittest.TestCase):
 def test_config_validation(self):
  for conf in ({'max_drawdown':'nan'},{'chunk_size':0},{'min_matches':1.5},{'live_enabled':True},{'second_slot_enabled':True}):
   with self.assertRaises((ValueError,TypeError)):check_config(conf)
 def test_unknown_rule_rejected(self):
  with self.assertRaises(ValueError):SignalEngine([{'id':'r','direction':'LIVE_OVER','conditions':[{'feature':'unknown','op':'eq','value':1}]}])
 def test_changed_duplicate_rejected(self):
  f=FeatureStream();e=event(0);f.feed(e)
  self.assertIsNone(f.feed(e))
  with self.assertRaises(ValueError):f.feed({**e,'water':[99,85]})
 def test_priority_is_fixed_not_list_order(self):
  t={'sid':'a','market':0,'phase':1,'eid':0,'side':0,'line':10,'water':95,'score':[0,0],'ts':1}
  r=[{'id':'z','direction':'LIVE_OVER','_trades':[[t]]},{'id':'a','direction':'LIVE_OVER','_trades':[[{**t,'eid':1,'water':90}]]}]
  one=dispatch(r,0,4)[0];two=dispatch(r[::-1],0,4)[0]
  self.assertEqual(one,two);self.assertEqual(one[0]['strategy_id'],'a')
 def test_queue_freezes_per_league_inputs(self):
  with tempfile.TemporaryDirectory() as td:
   m={'files':[{'path':'A.csv','kind':'quotes','targets':[['A','皇冠']]},{'path':'B.csv','kind':'quotes','targets':[['B','皇冠']]},{'path':'idx.csv','kind':'index','leagues':['A','B']}],'leagues':[]}
   st=Store(td);jid=st.create('A',DEFAULT,m)
   self.assertEqual([r['path'] for r in st.get(jid)['manifest']['files']],['A.csv','idx.csv'])
 def test_pause_queued_then_resume(self):
  with tempfile.TemporaryDirectory() as td:
   st=Store(td);jid=st.create('A',DEFAULT,{'files':[]})
   st.control(jid,'pause');self.assertEqual(st.get(jid)['status'],'PAUSED')
   st.control(jid,'resume');self.assertEqual(st.get(jid)['status'],'QUEUED')
if __name__=='__main__':unittest.main()

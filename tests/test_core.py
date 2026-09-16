import unittest,random,tempfile,json,zipfile
from pathlib import Path
from decimal import Decimal
import numpy as np
from lab.common import *
from lab.contracts import make_contract,bind_event
TEST_CONTRACT=make_contract("合成边界测试", "皇冠",100)
from lab.features import FeatureStream,SignalEngine,LABEL_FIELDS
from lab.rules import mask,matches,dictionary,tasks
from lab.mining import pack_masks,evaluate,first_indices
from lab.selection import metrics,dispatch,Quotes
from lab.data import safe_extract,inspect,load_inputs

def event(i,line=2,w0=95,w1=85,valid=True,ts=None,score0=(0,0)):
 return bind_event({'sid':'a','mid':0,'eid':i,'market':1,'phase':1,'ts':i if ts is None else ts,'row':i+2,'event_key':'file:'+str(i+2),'source':'file','minute':i,'minute_raw':str(i),'status':'滚','score':list(score0),'line':line,'water':[w0,w1],'valid':valid,'closed':not valid},TEST_CONTRACT)

def decimal_reference(line,water,margin,side,scale):
 L=Decimal(line)/4;w=Decimal(water)/scale
 lo=(L*2//1)/2 # correct floor for negative supplied below
 lo=Decimal(line//2)/2;hi=lo if line%2==0 else lo+Decimal('.5')
 values=[]
 for x in (lo,hi):
  d=(Decimal(margin)-x)*(1 if side==0 else -1)
  values.append(w/2 if d>0 else Decimal('-0.5') if d<0 else Decimal(0))
 return int(sum(values)*2*scale)

class Core(unittest.TestCase):
 def test_settlement_full_grid(self):
  for L in range(-17,27):
   for W in (1,70,95,105,180):
    for margin in range(-8,13):
     for side in (0,1):self.assertEqual(settlement(L,W,margin,side),decimal_reference(L,W,margin,side,100))
 def test_precision(self):
  self.assertEqual(scaled('0.94',100)-scaled('0.79',100),15)
  self.assertEqual(scaled('0',4),0);self.assertEqual(scaled('',4),MISSING)
  self.assertEqual(scaled('0.945',100),MISSING);self.assertEqual(scaled('0.945',1000),945)
 def test_role(self):
  self.assertEqual(side_for('PRE_GIVE',-1),1);self.assertEqual(side_for('LIVE_RECEIVE',-1),0)
  self.assertIsNone(side_for('PRE_GIVE',0));self.assertEqual(side_for('LIVE_PK_HOME',0),0)
 def test_history_first_vs_previous(self):
  s=FeatureStream();s.feed(event(0,w0=95));s.feed(event(1,w0=90));f=s.feed(event(2,w0=80))[0]
  self.assertEqual(f['samewater'],-15);self.assertEqual(f['water_prev_keep'],-10);self.assertEqual(f['path2_keep'],44)
 def test_closed_reset_keep(self):
  s=FeatureStream();s.feed(event(0));s.feed(event(1,w0=90));s.feed(event(2,valid=False));f=s.feed(event(3,w0=100))[0]
  self.assertEqual(f['water_prev_keep'],10);self.assertEqual(f['water_prev_reset'],MISSING);self.assertEqual(f['returnwater_reset'],0)
 def test_path_non_pk_history(self):
  s=FeatureStream();s.feed(event(0,line=-1));s.feed(event(1,line=1));f=s.feed(event(2,line=0))[0]
  self.assertEqual(f['path2_keep'],12);self.assertEqual(f['linepath2_keep'],12)
 def test_current_team_mapping(self):
  s=FeatureStream();s.feed(event(0,line=1,w0=90,w1=80));f=s.feed(event(1,line=-1,w0=100,w1=85))
  side=side_for('LIVE_GIVE',-1);self.assertEqual(f[side]['water_init'],5)
 def test_label_rejection(self):
  e=event(0);e['final']=[2,1]
  with self.assertRaises(ValueError):FeatureStream().feed(e)
 def test_resume_duplicate_prefix(self):
  r={'id':'r','direction':'LIVE_GIVE','conditions':[{'feature':'samewater','op':'le','value':-10}]}
  eng=SignalEngine([r],contract=TEST_CONTRACT);a=event(0);b=event(1,w0=80)
  self.assertEqual(eng.feed(a),[]);eng=SignalEngine([r],json.loads(canonical(eng.snapshot())),contract=TEST_CONTRACT)
  self.assertEqual(len(eng.feed(b)),1);self.assertEqual(eng.feed(b),[]);self.assertEqual(eng.feed(event(2,w0=70)),[])
 def test_stale_closed_latest(self):
  ev=[event(0,ts=0),event(1,valid=False,ts=1)]
  lab={'a':{'eligible':True,'final':[1,0],'year':2024,'kickoff':0,'date':'2024-01-01'}}
  q=Quotes(ev,lab,100,5);self.assertIsNone(q.trade(0,0,1,5))
 def test_first_conjunction_not_parent(self):
  arr={'eid':np.array([0,1,2,3]),'mid':np.array([0,0,1,1]),'pnl':np.array([-200,190,-200,190]),'a':np.ones(4,dtype=np.int64),'b':np.array([0,1,0,1])}
  atoms=[{'feature':'a','op':'eq','value':1},{'feature':'b','op':'eq','value':1}]
  self.assertEqual(list(first_indices([0],arr,atoms)),[0,2]);self.assertEqual(list(first_indices([0,1],arr,atoms)),[1,3])
  masks,nz,offs,ends,pp=pack_masks(arr,atoms);res=evaluate(np.array([[0,-1,-1],[0,1,-1]],dtype=np.int64),masks,nz,offs,ends,pp)
  self.assertEqual(res.tolist(),[[2,-400],[2,380]])
 def test_optimizer_random_all(self):
  rng=np.random.default_rng(11);arr={'eid':np.arange(215),'mid':np.repeat(np.arange(5),43),'pnl':rng.integers(-200,250,215),'x':rng.integers(0,8,215),'y':rng.integers(0,4,215)}
  atoms=[{'feature':'x','op':'ge','value':x} for x in range(8)]+[{'feature':'y','op':'eq','value':x} for x in range(4)]
  cs=list(tasks(atoms,list(range(12)),list(range(12))));cc=np.full((len(cs),3),-1,dtype=np.int64)
  for j,c in enumerate(cs):cc[j,:len(c)]=c
  m,n,o,e,p=pack_masks(arr,atoms);res=evaluate(cc,m,n,o,e,p)
  for c,r in zip(cs,res):
   ix=first_indices(c,arr,atoms);self.assertEqual([len(ix),int(arr['pnl'][ix].sum())],r.tolist())
 def test_metrics_losses_and_pushes(self):
  ts=[{'sid':str(i),'sort_time':i,'ts':i,'eid':i,'pnl':z,'year':2024} for i,z in enumerate([-200,0,-100,190])]
  m=metrics(ts,100,[2024]);self.assertEqual(m['streak'],2);self.assertEqual(m['streak_push_break'],1);self.assertEqual(m['drawdown'],1.5);self.assertEqual(m['n'],4)
 def test_dispatch_duplicates_cap(self):
  t={'sid':'a','market':0,'phase':1,'eid':0,'side':0,'line':10,'water':95,'score':[0,0],'ts':1}
  rs=[{'id':str(i),'direction':'LIVE_OVER','_trades':[[t]]} for i in range(100)]
  orders,reject=dispatch(rs,0,4,True);self.assertEqual(len(orders),1);self.assertEqual(len(reject),99)
 def test_dispatch_opposite(self):
  t={'sid':'a','market':0,'phase':1,'eid':0,'side':0,'line':10,'water':95,'score':[0,0],'ts':1}
  rs=[{'id':'a','direction':'LIVE_OVER','_trades':[[t]]},{'id':'b','direction':'LIVE_UNDER','_trades':[[{**t,'side':1}]]}]
  self.assertEqual(len(dispatch(rs,0,4)[0]),0)
 def test_zip_traversal(self):
  with tempfile.TemporaryDirectory() as td:
   z=Path(td)/'a.zip'
   with zipfile.ZipFile(z,'w') as f:f.writestr('../bad.txt','x')
   with self.assertRaises(ValueError):safe_extract(z,Path(td)/'out')
 def test_demo_adapter(self):
  with tempfile.TemporaryDirectory() as td:
   m=inspect([str(ROOT/'demo')],td);ev,lb,s,a=load_inputs(m,'合成演示联赛（非真实比赛）','皇冠')
   self.assertEqual(len(lb),135);self.assertEqual(len(ev),3240);self.assertEqual(s,100)
   self.assertTrue(all(not LABEL_FIELDS.intersection(e) for e in ev))
if __name__=='__main__':unittest.main()
